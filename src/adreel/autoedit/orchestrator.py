"""The automatic editor: raw clips + business context -> edit plan -> rendered video.

    normalise -> analyse (silences, scenes, proxies) -> transcribe (cached) ->
    plan (LLM with validate/repair/retry, heuristic fallback) -> voiceover (TTS, fit, duck) ->
    captions -> render -> plan.json + cost.json

Every paid call is recorded in a ``CostLog``. Transcripts are cached on disk by clip
content + provider + model, so re-planning or re-rendering never pays for ASR twice.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pydantic import ValidationError

from adreel.ai.base import (
    MediaInput,
    Planner,
    PlannerTurn,
    ProviderError,
    SpeechResult,
    SpeechSynthesizer,
    Transcriber,
    Transcript,
    TranscriptWord,
)
from adreel.ai.costs import CostLog
from adreel.ai.registry import build_planner, build_transcriber, build_tts
from adreel.analysis import (
    detect_scenes,
    detect_silences,
    extract_speech_audio,
    make_proxy,
    speech_stats,
)
from adreel.autoedit.assemble import assemble_plan
from adreel.autoedit.heuristic import heuristic_plan
from adreel.autoedit.prompt import ClipAnalysis, build_prompt, build_system, format_problems
from adreel.autoedit.schema import BusinessContext, PlannerOutput
from adreel.autoedit.transcript_lines import (
    TranscriptLine,
    build_transcript_lines,
    finalize_captions,
    lines_from_words,
    to_caption_lines,
)
from adreel.config import Settings, get_settings
from adreel.log import get_logger
from adreel.plan_checks import check_plan, planned_duration, repair_plan
from adreel.render.normalize import NormalizeParams, normalize_clip
from adreel.render.pipeline import (
    Quality,
    Renderer,
    RenderRequest,
    RenderResult,
    safe_progress,
)
from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.edit_plan import EditPlan, Voiceover, VoiceTier
from adreel.schemas.template import TemplateSpec

log = get_logger(__name__)

ProgressFn = Callable[[str, float], None]
VO_START_S = 0.3


class AutoEditError(RuntimeError):
    pass


@dataclass
class AutoEditRequest:
    template_id: str
    brand: BrandKit
    clips: dict[str, Path]
    context: BusinessContext
    output: Path
    voice: VoiceTier | None = None  # None = no voiceover
    quality: Quality = Quality.PREVIEW
    watermark: bool = False
    language: str | None = None  # default: brand language


@dataclass
class AutoEditResult:
    plan: EditPlan
    render: RenderResult
    costs: CostLog
    plan_path: Path
    cost_path: Path
    planner_used: str
    attempts: int
    warnings: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


class AutoEditor:
    def __init__(
        self,
        settings: Settings | None = None,
        renderer: Renderer | None = None,
        *,
        transcriber: Transcriber | None = None,
        planner: Planner | str | None = "auto",
        tts: Callable[[VoiceTier], SpeechSynthesizer] | None = None,
    ) -> None:
        self.s = settings or get_settings()
        self.renderer = renderer or Renderer(settings=self.s)
        self.ff = self.renderer.ff
        self._transcriber = transcriber
        self._planner_cfg = planner
        self._tts = tts or (lambda tier: build_tts(self.s, tier, self.ff))

    def close(self) -> None:
        self.renderer.close()

    def __enter__(self) -> AutoEditor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ main

    def run(self, req: AutoEditRequest, progress: ProgressFn | None = None) -> AutoEditResult:
        report = safe_progress(progress)
        tpl = self.renderer.templates.get(req.template_id)
        lang = req.language or req.brand.language
        self._check_context(tpl, req.context)
        costs = CostLog(self.s.prices)
        warnings: list[str] = []
        timings: dict[str, float] = {}
        work = self.s.work_dir.resolve()
        job = (
            work
            / "auto"
            / _key(
                sorted((k, str(v)) for k, v in req.clips.items()), req.template_id, str(req.output)
            )
        )
        job.mkdir(parents=True, exist_ok=True)

        # 1. normalise + analyse ---------------------------------------------
        t0 = time.monotonic()
        report("analyze", 0.0)
        params = NormalizeParams(
            fps=tpl.canvas.fps,
            target_lufs=tpl.audio.target_lufs,
            true_peak_db=tpl.audio.true_peak_db,
        )
        analyses: list[ClipAnalysis] = []
        norm_paths: dict[str, Path] = {}
        for i, cid in enumerate(sorted(req.clips)):
            n = normalize_clip(self.ff, cid, req.clips[cid], work / "norm", params)
            norm_paths[cid] = n.path
            silences = detect_silences(self.ff, n.path, n.info.duration)
            proxy = (
                make_proxy(self.ff, n.path, job / "proxy" / f"{cid}.mp4")
                if self._uses_llm()
                else n.path
            )
            scenes = detect_scenes(proxy) if self._uses_llm() else []
            analyses.append(
                ClipAnalysis(
                    cid, n.info.duration, n.info.width, n.info.height, proxy, silences, scenes
                )
            )
            report("analyze", 0.12 * (i + 1) / len(req.clips))
        timings["analyze"] = time.monotonic() - t0

        # 2. transcribe ----------------------------------------------------------
        t0 = time.monotonic()
        transcripts: dict[str, Transcript] = {}
        for i, a in enumerate(analyses):
            if not speech_stats(a.silences, a.duration).has_speech:
                log.info("transcribe.skip", clip=a.clip_id, reason="no speech detected")
                continue
            tr = self._transcribe(a.clip_id, norm_paths[a.clip_id], a.duration, job, lang, costs)
            if tr.spoken:
                transcripts[a.clip_id] = tr
            report("transcribe", 0.12 + 0.13 * (i + 1) / len(analyses))
        analyses = [
            ClipAnalysis(
                a.clip_id,
                a.duration,
                a.width,
                a.height,
                a.proxy,
                a.silences,
                a.scenes,
                transcripts.get(a.clip_id),
            )
            for a in analyses
        ]
        timings["transcribe"] = time.monotonic() - t0
        cs = tpl.captions
        lines = build_transcript_lines(transcripts, max_units=cs.max_units_per_line * cs.max_lines)
        words_by_clip: dict[str, tuple[TranscriptWord, ...]] = {
            k: v.spoken for k, v in transcripts.items()
        }
        durations = {a.clip_id: a.duration for a in analyses}

        # 3. plan ------------------------------------------------------------------
        t0 = time.monotonic()
        report("plan", 0.25)
        want_vo = req.voice is not None

        def assemble(out: PlannerOutput) -> EditPlan:
            return assemble_plan(
                out,
                template=tpl,
                language=lang,
                context=req.context,
                lines=lines,
                words_by_clip=words_by_clip,
                clip_durations=durations,
            )

        out, plan, planner_used, attempts = self._plan(
            req, tpl, lang, analyses, lines, want_vo, assemble, durations, costs, warnings
        )
        timings["plan"] = time.monotonic() - t0
        report("plan", 0.42)

        # 4. voiceover ------------------------------------------------------------
        t0 = time.monotonic()
        if req.voice is not None:
            plan = self._voiceover(req.voice, out, plan, tpl, lang, job, costs, warnings)
        plan = finalize_captions(plan, tpl.canvas.fps)
        timings["voiceover"] = time.monotonic() - t0
        report("voiceover", 0.5)

        # 5. render -----------------------------------------------------------------
        t0 = time.monotonic()
        plan_path = req.output.with_suffix(".plan.json")
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(plan.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
        result = self.renderer.render(
            RenderRequest(
                plan=plan,
                brand=req.brand,
                clips=req.clips,
                output=req.output,
                quality=req.quality,
                watermark=req.watermark,
            ),
            lambda stage, frac: report(f"render:{stage}", 0.5 + 0.5 * frac),
        )
        warnings += result.warnings
        timings["render"] = time.monotonic() - t0
        if result.plan != plan:  # the renderer repaired something: keep the file in sync
            plan_path.write_text(
                result.plan.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
            )
        cost_path = req.output.with_suffix(".cost.json")
        costs.write(cost_path)
        _append_meta(
            cost_path,
            {
                "planner": planner_used,
                "attempts": attempts,
                "video_seconds": round(result.duration, 2),
                "timings_s": {k: round(v, 2) for k, v in timings.items()},
            },
        )
        log.info(
            "autoedit.done",
            output=str(req.output),
            planner=planner_used,
            attempts=attempts,
            cost_usd=round(costs.total_usd, 4),
            seconds=round(result.duration, 2),
        )
        return AutoEditResult(
            result.plan,
            result,
            costs,
            plan_path,
            cost_path,
            planner_used,
            attempts,
            warnings,
            {k: round(v, 2) for k, v in timings.items()},
        )

    # ------------------------------------------------------------------ steps

    @staticmethod
    def _check_context(tpl: TemplateSpec, ctx: BusinessContext) -> None:
        # Facts the LLM must never invent have to come from the business.
        missing = [
            s
            for s in tpl.required_slots
            if s in ("product_name", "price") and getattr(ctx, s) in (None, "")
        ]
        if missing:
            raise AutoEditError(f"template {tpl.id} needs: {', '.join(missing)}")

    def _uses_llm(self) -> bool:
        return self._planner_cfg not in (None, "heuristic") and not (
            self._planner_cfg == "auto" and self.s.planner == "heuristic"
        )

    def _get_planner(self) -> Planner | None:
        if self._planner_cfg == "auto":
            return build_planner(self.s)
        if isinstance(self._planner_cfg, str):
            return None
        return self._planner_cfg

    def _transcribe(
        self, clip_id: str, norm: Path, duration: float, job: Path, lang: str, costs: CostLog
    ) -> Transcript:
        transcriber = self._transcriber or build_transcriber(self.s, self.ff)
        cache = (
            self.s.work_dir.resolve()
            / "transcripts"
            / f"{_key(norm.name, transcriber.name, transcriber.model, lang)}.json"
        )
        if cache.exists():
            log.info("transcribe.cached", clip=clip_id)
            return _load_transcript(cache)
        audio = extract_speech_audio(self.ff, norm, job / "audio" / f"{clip_id}.mp3")
        tr = transcriber.transcribe(audio, language=lang, duration_s=duration)
        costs.transcription(tr.provider, tr.model, tr.audio_seconds, detail=clip_id)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(asdict(tr), ensure_ascii=False), encoding="utf-8")
        log.info("transcribe.done", clip=clip_id, words=len(tr.spoken), language=tr.language)
        return tr

    def _plan(
        self,
        req: AutoEditRequest,
        tpl: TemplateSpec,
        lang: str,
        analyses: list[ClipAnalysis],
        lines: list[TranscriptLine],
        want_vo: bool,
        assemble: Callable[[PlannerOutput], EditPlan],
        durations: dict[str, float],
        costs: CostLog,
        warnings: list[str],
    ) -> tuple[PlannerOutput, EditPlan, str, int]:
        planner = self._get_planner() if self._uses_llm() else None
        if planner is not None:
            turn = PlannerTurn(
                prompt=build_prompt(
                    template=tpl,
                    brand=req.brand,
                    context=req.context,
                    clips=analyses,
                    lines=lines,
                    want_voiceover=want_vo,
                    language=lang,
                ),
                media=[MediaInput(a.clip_id, a.proxy, "video/mp4", a.describe()) for a in analyses],
                system=build_system(lang),
                response_schema=PlannerOutput.model_json_schema(),
            )
            try:
                for attempt in range(1, self.s.plan_attempts + 1):
                    reply = planner.complete(turn)
                    u = reply.usage
                    costs.llm(
                        planner.name,
                        reply.model,
                        input_tokens=u.input_tokens,
                        input_audio_tokens=u.input_audio_tokens,
                        output_tokens=u.output_tokens,
                        detail=f"attempt {attempt}",
                    )
                    problems, out, plan = self._evaluate(reply.text, assemble, tpl, durations)
                    if not problems:
                        assert out is not None and plan is not None
                        return out, plan, planner.name, attempt
                    log.warning("plan.invalid", attempt=attempt, problems=problems[:5])
                    turn.feedback.append((reply.text, format_problems(problems)))
                warnings.append(
                    f"planner output invalid after {self.s.plan_attempts} attempts; "
                    "used heuristic plan"
                )
            except ProviderError as exc:
                warnings.append(f"planner failed ({exc}); used heuristic plan")
                log.error("plan.provider_error", error=str(exc))
            finally:
                planner.close()
        out = heuristic_plan(tpl, req.context, analyses, want_voiceover=want_vo, language=lang)
        plan = assemble(out)
        attempts = len(turn.feedback) if planner is not None else 0
        return out, plan, "heuristic", attempts

    @staticmethod
    def _evaluate(
        text: str,
        assemble: Callable[[PlannerOutput], EditPlan],
        tpl: TemplateSpec,
        durations: dict[str, float],
    ) -> tuple[list[str], PlannerOutput | None, EditPlan | None]:
        try:
            out = PlannerOutput.model_validate_json(text)
        except ValidationError as exc:
            return [_fmt_error(e) for e in exc.errors()], None, None
        try:
            plan = assemble(out)
        except ValidationError as exc:
            return [_fmt_error(e) for e in exc.errors()], None, None
        plan, _fixes = repair_plan(plan, tpl, durations)
        issues = check_plan(plan, tpl, durations)
        problems = [i.message for i in issues if i.severity == "error"]
        total = planned_duration(plan, tpl)
        end_card = tpl.end_card.duration_s if tpl.end_card.enabled else 0.0
        enough_footage = sum(durations.values()) >= (tpl.duration.min_s - end_card) * 1.15
        if total < tpl.duration.min_s - 1.0 and enough_footage:  # only ask for what's possible
            problems.append(
                f"the video is only {total:.1f} s; template minimum is {tpl.duration.min_s:.0f} s "
                "(including the end card): add more segments"
            )
        return problems, out, plan

    def _voiceover(
        self,
        tier: VoiceTier,
        out: PlannerOutput,
        plan: EditPlan,
        tpl: TemplateSpec,
        lang: str,
        job: Path,
        costs: CostLog,
        warnings: list[str],
    ) -> EditPlan:
        from adreel.autoedit.heuristic import default_voiceover
        from adreel.render.timeline import build_timeline

        script = (out.voiceover_script or "").strip() or default_voiceover(
            BusinessContext(
                product_name=plan.slots.product_name,
                price=plan.slots.price,
                cta=plan.slots.cta_text,
            ),
            lang,
        )
        main = build_timeline(plan, tpl.canvas.fps).main_duration
        available = max(1.0, main - VO_START_S - 0.2)
        try:
            tts = self._tts(tier)
            speech = self._synth(tts, script, job / "vo", lang, 1.0, costs)
            if speech.duration > available:
                speed = min(tts.max_speed, speech.duration / available * 1.03)
                if speed > 1.02:
                    speech = self._synth(tts, script, job / "vo_fast", lang, speed, costs)
            if speech.duration > available + 0.3:
                warnings.append(
                    f"voiceover is {speech.duration:.1f} s but the video has {available:.1f} s; "
                    "the end is cut"
                )
        except ProviderError as exc:
            warnings.append(f"voiceover failed ({exc}); rendered without it")
            return plan
        vo = Voiceover(
            script=script[:1500], voice=tier, start=VO_START_S, audio_path=str(speech.audio_path)
        )
        cs = tpl.captions
        vo_lines = lines_from_words(
            speech.words, offset=VO_START_S, max_units=cs.max_units_per_line * cs.max_lines
        )
        vo_caps = [c for c in to_caption_lines(vo_lines) if c.end <= main + 0.5]
        return plan.model_copy(update={"voiceover": vo, "captions": vo_caps})

    @staticmethod
    def _synth(
        tts: SpeechSynthesizer, script: str, out: Path, lang: str, speed: float, costs: CostLog
    ) -> SpeechResult:
        r = tts.synthesize(script, out, language=lang, speed=speed)
        costs.tts(r.provider, r.voice, r.characters, detail=f"speed {speed:.2f}")
        log.info(
            "tts.done",
            provider=r.provider,
            seconds=round(r.duration, 2),
            speed=round(speed, 2),
            estimated_timings=r.timings_estimated,
        )
        return r


# --------------------------------------------------------------------------- helpers


def _key(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _fmt_error(e: object) -> str:
    err = e if isinstance(e, dict) else {}
    loc = ".".join(str(x) for x in err.get("loc", ()))
    return f"{loc}: {err.get('msg', e)}" if loc else str(err.get("msg", e))


def _load_transcript(path: Path) -> Transcript:
    d = json.loads(path.read_text(encoding="utf-8"))
    d["words"] = tuple(TranscriptWord(**w) for w in d["words"])
    return Transcript(**d)


def _append_meta(path: Path, meta: dict[str, object]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(meta)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

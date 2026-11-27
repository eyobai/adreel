"""Render orchestration: clips + edit plan + brand kit -> finished 9:16 MP4.

Stages (each cached on disk, keyed by its inputs):

1. normalise every referenced clip            work/norm/   (shared across renders)
2. check + repair the plan against reality
3. cut + reframe each segment to the canvas    work/seg/    (shared across renders)
4. end-card background segment
5. build the ASS overlay (captions, cards...)  job dir
6. compose: concat -> logo overlays -> ASS -> audio mix (VO ducking) -> encode

Because 1 and 3 are cached by input, the "fix captions" flow (new caption text, same cuts)
re-runs only 5–6, and "approve" (final quality) reuses the normalised clips.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from adreel.config import Settings, get_settings
from adreel.log import get_logger
from adreel.plan_checks import PlanError, check_plan, repair_plan
from adreel.render.captions import CaptionEvent
from adreel.render.ffmpeg import FFmpeg
from adreel.render.normalize import NormalizedClip, NormalizeParams, normalize_clip
from adreel.render.overlays import LogoPlacement, build_overlays
from adreel.render.reframe import (
    FaceTracker,
    FaceTrackingUnavailable,
    MediaPipeFaceTracker,
    decide_reframe,
)
from adreel.render.timeline import PlacedSegment, Timeline, build_timeline
from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.edit_plan import EditPlan, ReframeMode, SegmentRole
from adreel.schemas.template import TemplateSpec
from adreel.templates_registry import TemplateRegistry

log = get_logger(__name__)

ProgressFn = Callable[[str, float], None]  # (stage, overall fraction 0..1)


class Quality(StrEnum):
    PREVIEW = "preview"
    FINAL = "final"


@dataclass(frozen=True)
class QualityPreset:
    scale: float  # of the template canvas
    seg_crf: int
    seg_preset: str
    out_crf: int
    out_preset: str
    audio_bitrate: str
    maxrate: str | None = None


PRESETS: dict[Quality, QualityPreset] = {
    # Small and fast: sent over slow mobile connections, ~2–4 MB for 30 s.
    Quality.PREVIEW: QualityPreset(0.5, 20, "veryfast", 28, "veryfast", "96k", "1000k"),
    Quality.FINAL: QualityPreset(1.0, 15, "veryfast", 19, "medium", "160k"),
}


@dataclass
class RenderRequest:
    plan: EditPlan
    brand: BrandKit
    clips: dict[str, Path]
    output: Path
    quality: Quality = Quality.PREVIEW
    watermark: bool = False
    voiceover_audio: Path | None = None
    auto_repair: bool = True


@dataclass
class RenderResult:
    output: Path
    duration: float
    width: int
    height: int
    size_bytes: int
    plan: EditPlan  # after repair
    captions: list[CaptionEvent]
    warnings: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)


class RenderError(RuntimeError):
    pass


def _key(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:20]


class Renderer:
    def __init__(
        self,
        settings: Settings | None = None,
        templates: TemplateRegistry | None = None,
        ff: FFmpeg | None = None,
        face_tracker: FaceTracker | str | None = "auto",
    ) -> None:
        self.s = settings or get_settings()
        self.templates = templates or TemplateRegistry(self.s.templates_dirs)
        self.ff = ff or FFmpeg(
            self.s.ffmpeg_bin, self.s.ffprobe_bin, self.s.ffmpeg_timeout_s, self.s.ffmpeg_threads
        )
        self._tracker_cfg = face_tracker
        self._tracker: FaceTracker | None = None
        self._tracker_ready = False

    def close(self) -> None:
        closer = getattr(self._tracker, "close", None)
        if callable(closer):
            closer()
        self._tracker, self._tracker_ready = None, False

    def __enter__(self) -> Renderer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ public

    def render(self, req: RenderRequest, progress: ProgressFn | None = None) -> RenderResult:
        report = progress or (lambda stage, frac: None)
        timings: dict[str, float] = {}
        warnings: list[str] = []
        tpl = self.templates.get(req.plan.template_id)
        preset = PRESETS[req.quality]
        work = self.s.work_dir.resolve()
        out_w = int(tpl.canvas.width * preset.scale) // 2 * 2
        out_h = int(tpl.canvas.height * preset.scale) // 2 * 2
        fps = tpl.canvas.fps

        # 1. normalise -----------------------------------------------------
        t0 = time.monotonic()
        report("normalize", 0.0)
        needed = sorted({s.clip_id for s in req.plan.segments})
        missing = [c for c in needed if c not in req.clips]
        if missing:
            raise RenderError(f"no file given for clip(s): {missing}")
        params = NormalizeParams(
            fps=fps, target_lufs=tpl.audio.target_lufs, true_peak_db=tpl.audio.true_peak_db
        )
        norm: dict[str, NormalizedClip] = {}
        for i, cid in enumerate(needed):
            norm[cid] = normalize_clip(self.ff, cid, req.clips[cid], work / "norm", params)
            report("normalize", 0.3 * (i + 1) / len(needed))
        timings["normalize"] = time.monotonic() - t0

        # 2. check / repair ------------------------------------------------
        durations = {cid: n.info.duration for cid, n in norm.items()}
        for cid in req.plan.clip_ids - set(durations):
            if cid in req.clips:  # caption-only clip reference: just need its duration
                durations[cid] = self.ff.probe(req.clips[cid]).duration
        plan = req.plan
        issues = check_plan(plan, tpl, durations)
        if req.auto_repair and issues:
            plan, fixes = repair_plan(plan, tpl, durations)
            warnings += [f"repaired: {f}" for f in fixes]
            issues = check_plan(plan, tpl, durations)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            raise PlanError(errors)
        warnings += [str(i) for i in issues]

        # 3. segments ------------------------------------------------------
        t0 = time.monotonic()
        end_card_s = tpl.end_card.duration_s if tpl.end_card.enabled else 0.0
        tl = build_timeline(plan, fps, end_card_s)
        zooms = self._alternate_zooms(plan, tpl)
        seg_files: list[Path] = []
        for i, p in enumerate(tl.placed):
            seg_files.append(
                self._render_segment(
                    p, norm[p.segment.clip_id], zooms[i], out_w, out_h, fps, preset
                )
            )
            report("segments", 0.3 + 0.35 * (i + 1) / len(tl.placed))
        if tl.end_card_frames:
            seg_files.append(self._render_end_card(tpl, req.brand, tl, out_w, out_h, preset))
        timings["segments"] = time.monotonic() - t0

        # 4. overlays + compose -------------------------------------------
        t0 = time.monotonic()
        job_dir = (
            work
            / "jobs"
            / _key(
                plan.model_dump(mode="json"),
                req.brand.model_dump(mode="json"),
                req.quality,
                req.watermark,
                str(req.output),
            )
        )
        if job_dir.exists():
            shutil.rmtree(job_dir)
        job_dir.mkdir(parents=True)
        logo = req.brand.logo_path
        has_logo = bool(logo and logo.is_file())
        if logo and not has_logo:
            warnings.append(f"logo not found: {logo}")
        ov = build_overlays(
            plan,
            req.brand,
            tpl,
            tl,
            has_logo=has_logo,
            watermark_text=self.s.watermark_text if req.watermark else None,
        )
        warnings += ov.warnings
        (job_dir / "overlays.ass").write_text(ov.doc.render(), encoding="utf-8")
        os.symlink(self.s.fonts_dir.resolve(), job_dir / "fonts", target_is_directory=True)
        (job_dir / "concat.txt").write_text("".join(f"file '{f}'\n" for f in seg_files))

        vo_path = req.voiceover_audio or (
            Path(plan.voiceover.audio_path)
            if plan.voiceover and plan.voiceover.audio_path
            else None
        )
        if plan.voiceover and vo_path is None:
            warnings.append("voiceover script present but no audio file; rendered without VO")
        if vo_path is not None and not vo_path.is_file():
            raise RenderError(f"voiceover audio not found: {vo_path}")
        vo_start = plan.voiceover.start if plan.voiceover else 0.0

        req.output.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = req.output.with_name(req.output.stem + ".part" + req.output.suffix)
        args = self._compose_args(
            tpl,
            tl,
            preset,
            out_w,
            out_h,
            job_dir,
            logo if has_logo else None,
            ov.logos,
            vo_path,
            vo_start,
            tmp_out,
        )
        total = tl.total_duration
        # ffmpeg resolves the relative ASS/fonts/concat paths against its cwd, which avoids
        # filtergraph escaping of arbitrary absolute paths.
        self.ff.run(
            args,
            cwd=job_dir,
            progress=lambda sec: report("compose", 0.65 + 0.35 * min(1.0, sec / total)),
        )
        tmp_out.replace(req.output)
        timings["compose"] = time.monotonic() - t0
        report("done", 1.0)

        info = self.ff.probe(req.output)
        result = RenderResult(
            output=req.output,
            duration=info.duration,
            width=info.width,
            height=info.height,
            size_bytes=req.output.stat().st_size,
            plan=plan,
            captions=ov.captions,
            warnings=warnings,
            timings={k: round(v, 2) for k, v in timings.items()},
        )
        log.info(
            "render.done",
            output=str(req.output),
            duration=round(info.duration, 2),
            size_kb=result.size_bytes // 1024,
            quality=req.quality.value,
            timings=result.timings,
            warnings=len(warnings),
        )
        return result

    # ------------------------------------------------------------------ helpers

    def _get_tracker(self) -> FaceTracker | None:
        if self._tracker_ready:
            return self._tracker
        self._tracker_ready = True
        cfg = self._tracker_cfg
        if cfg == "auto":
            if not self.s.face_tracking:
                return None
            try:
                self._tracker = MediaPipeFaceTracker(self.s.face_model_path, self.s.ffmpeg_bin)
            except FaceTrackingUnavailable as exc:
                log.warning("face_tracking.unavailable", reason=str(exc))
        elif cfg is not None and not isinstance(cfg, str):
            self._tracker = cfg
        return self._tracker

    @staticmethod
    def _alternate_zooms(plan: EditPlan, tpl: TemplateSpec) -> list[float]:
        """Alternate punch-in across consecutive same-clip speaker cuts (hides jump cuts)."""
        alt = tpl.reframe.alternate_zoom
        zooms, parity = [], 0
        for i, s in enumerate(plan.segments):
            prev = plan.segments[i - 1] if i else None
            if (
                alt > 1.0
                and prev is not None
                and s.role is SegmentRole.SPEAKER
                and prev.clip_id == s.clip_id
                and prev.role is SegmentRole.SPEAKER
            ):
                parity ^= 1
            else:
                parity = 0
            zooms.append(alt if parity else 1.0)
        return zooms

    def _render_segment(
        self,
        p: PlacedSegment,
        clip: NormalizedClip,
        zoom: float,
        out_w: int,
        out_h: int,
        fps: int,
        preset: QualityPreset,
    ) -> Path:
        seg = p.segment
        tracking = self.s.face_tracking and seg.reframe.mode in (ReframeMode.AUTO, ReframeMode.FACE)
        key = _key(
            "seg-v1",
            clip.path.name,
            seg.start,
            p.frames,
            fps,
            seg.reframe.model_dump(mode="json"),
            zoom,
            seg.keep_audio,
            out_w,
            out_h,
            preset.seg_crf,
            preset.seg_preset,
            tracking,
        )
        dst = self.s.work_dir.resolve() / "seg" / f"seg_{key}.mkv"
        if dst.exists():
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        info = clip.info
        dur = p.duration
        samples = None
        if tracking and abs(info.aspect - 9 / 16) > 0.015:
            tracker = self._get_tracker()
            if tracker is not None:
                samples = tracker.sample(
                    clip.path, seg.start, seg.start + dur, info.width, info.height
                )
        decision = decide_reframe(seg.reframe, info.width, info.height, zoom=zoom, samples=samples)
        log.info("segment.reframe", segment=seg.id, kind=decision.kind, reason=decision.reason)
        vol = "" if seg.keep_audio else ",volume=0"
        fc = (
            decision.video_filter(out_w, out_h, "0:v", "vr")
            + ";[vr]tpad=stop_mode=clone:stop_duration=2[v];"
            + f"[0:a]asetpts=PTS-STARTPTS{vol},apad,atrim=0:{dur:.6f}[a]"
        )
        tmp = dst.with_suffix(".part.mkv")
        self.ff.run(
            [
                "-ss",
                f"{seg.start:.6f}",
                "-i",
                str(clip.path),
                "-filter_complex",
                fc,
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-frames:v",
                str(p.frames),
                "-r",
                str(fps),
                "-c:v",
                "libx264",
                "-preset",
                preset.seg_preset,
                "-crf",
                str(preset.seg_crf),
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                str(tmp),
            ]
        )
        tmp.replace(dst)
        return dst

    def _render_end_card(
        self,
        tpl: TemplateSpec,
        brand: BrandKit,
        tl: Timeline,
        out_w: int,
        out_h: int,
        preset: QualityPreset,
    ) -> Path:
        from adreel.render.overlays import Palette

        bg = Palette(brand)(tpl.end_card.background)
        fps = tpl.canvas.fps
        key = _key("end-v1", bg, out_w, out_h, fps, tl.end_card_frames, preset.seg_crf)
        dst = self.s.work_dir.resolve() / "seg" / f"end_{key}.mkv"
        if dst.exists():
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        dur = tl.end_card_duration
        tmp = dst.with_suffix(".part.mkv")
        self.ff.run(
            [
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x{bg.lstrip('#')}:s={out_w}x{out_h}:r={fps}:d={dur:.6f}",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-vf",
                "vignette=angle=PI/5,format=yuv420p,setsar=1",
                "-frames:v",
                str(tl.end_card_frames),
                "-af",
                f"atrim=0:{dur:.6f}",
                "-c:v",
                "libx264",
                "-preset",
                preset.seg_preset,
                "-crf",
                str(preset.seg_crf),
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                str(tmp),
            ]
        )
        tmp.replace(dst)
        return dst

    def _compose_args(
        self,
        tpl: TemplateSpec,
        tl: Timeline,
        preset: QualityPreset,
        out_w: int,
        out_h: int,
        job_dir: Path,
        logo: Path | None,
        logos: list[LogoPlacement],
        vo: Path | None,
        vo_start: float,
        out: Path,
    ) -> list[str]:
        total = tl.total_duration
        fps = tpl.canvas.fps
        k = out_w / tpl.canvas.width  # canvas px -> output px
        args = ["-f", "concat", "-safe", "0", "-i", "concat.txt"]
        chains: list[str] = []
        cur = "0:v"
        next_in = 1
        if logo is not None and logos:
            args += [
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-t",
                f"{total:.3f}",
                "-i",
                str(logo.resolve()),
            ]
            li = next_in
            next_in += 1
            labels = [f"lg{j}" for j in range(len(logos))]
            chains.append(
                f"[{li}:v]format=rgba,split={len(logos)}" + "".join(f"[{x}]" for x in labels)
            )
            for j, lp in enumerate(logos):
                bw, bh = max(2, round(lp.box_w * k)), max(2, round(lp.box_h * k))
                chains.append(
                    f"[{labels[j]}]scale={bw}:{bh}:force_original_aspect_ratio=decrease:flags=lanczos,"
                    f"colorchannelmixer=aa={lp.opacity:.3f}[{labels[j]}s]"
                )
                x, y = lp.overlay_xy(k)
                chains.append(
                    f"[{cur}][{labels[j]}s]overlay=x='{x}':y='{y}':"
                    f"enable='between(t,{lp.start:.3f},{lp.end:.3f})'[ov{j}]"
                )
                cur = f"ov{j}"
        chains.append(f"[{cur}]ass=filename=overlays.ass:fontsdir=fonts,format=yuv420p[vout]")

        aud = tpl.audio
        if vo is not None:
            args += ["-i", str(vo.resolve())]
            vi = next_in
            ms = round(vo_start * 1000)
            chains.append(
                # TTS output varies wildly in level: bring the VO to the template target first.
                f"[{vi}:a]loudnorm=I={aud.target_lufs}:TP={aud.true_peak_db}:LRA=11,"
                f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={aud.voiceover_gain_db}dB,adelay={ms}:all=1,apad,atrim=0:{total:.6f},"
                f"asplit=2[vosc][vomix]"
            )
            chains.append(
                f"[0:a]aformat=sample_fmts=fltp:channel_layouts=stereo[bed0];"
                f"[bed0][vosc]sidechaincompress=threshold={aud.duck_threshold}:ratio={aud.duck_ratio}:"
                f"attack=20:release=400[bed];"
                f"[bed][vomix]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.89:level=0[aout]"
            )
        else:
            chains.append("[0:a]alimiter=limit=0.89:level=0[aout]")

        args += [
            "-filter_complex",
            ";".join(chains),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-t",
            f"{total:.6f}",
            "-r",
            str(fps),
            "-c:v",
            "libx264",
            "-preset",
            preset.out_preset,
            "-crf",
            str(preset.out_crf),
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
        ]
        if preset.maxrate:
            args += [
                "-maxrate",
                preset.maxrate,
                "-bufsize",
                str(int(preset.maxrate[:-1]) * 2) + "k",
            ]
        args += [
            "-c:a",
            "aac",
            "-b:a",
            preset.audio_bitrate,
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out.resolve()),
        ]
        return args

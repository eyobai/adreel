"""Command line interface.

adreel make    -t product_promo -b brand.json -c c1=a.mp4 --product .. --price .. -o out.mp4
adreel transcribe clip.mp4
adreel render  --plan plan.json --brand brand.json --clip c1=a.mp4 --clip c2=b.mp4 -o out.mp4
adreel validate --plan plan.json --clip c1=a.mp4
adreel schema  [-o edit_plan.schema.json]
adreel templates
adreel samples  -o examples/media     # synthetic clips + logo for trying things out
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from adreel.config import get_settings
from adreel.log import configure_logging
from adreel.plan_checks import PlanError, check_plan, repair_plan
from adreel.render.ffmpeg import FFmpeg, FFmpegError
from adreel.render.pipeline import Quality, Renderer, RenderError, RenderRequest
from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.edit_plan import EditPlan, edit_plan_json_schema
from adreel.templates_registry import TemplateError, TemplateRegistry

app = typer.Typer(add_completion=False, no_args_is_help=True, help="AdReel: AI video editing")


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    s = get_settings()
    configure_logging("DEBUG" if verbose else s.log_level, s.log_json)


def _parse_clips(values: list[str]) -> dict[str, Path]:
    clips: dict[str, Path] = {}
    for v in values:
        cid, sep, path = v.partition("=")
        if not sep or not cid or not path:
            raise typer.BadParameter(f"--clip must look like c1=path/to/video.mp4, got {v!r}")
        p = Path(path).expanduser()
        if not p.is_file():
            raise typer.BadParameter(f"clip file not found: {p}")
        clips[cid] = p
    return clips


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"cannot read {path}: {exc}") from exc


def _load_plan(path: Path) -> EditPlan:
    try:
        return EditPlan.model_validate(_load_json(path))
    except ValidationError as exc:
        typer.secho(f"Edit plan {path} is invalid:\n{exc}", fg="red", err=True)
        raise typer.Exit(2) from exc


def _load_brand(path: Path) -> BrandKit:
    try:
        brand = BrandKit.model_validate(_load_json(path))
    except ValidationError as exc:
        typer.secho(f"Brand kit {path} is invalid:\n{exc}", fg="red", err=True)
        raise typer.Exit(2) from exc
    if brand.logo_path and not brand.logo_path.is_absolute():  # relative to the brand file
        brand = brand.model_copy(update={"logo_path": (path.parent / brand.logo_path).resolve()})
    return brand


PlanOpt = Annotated[Path, typer.Option("--plan", "-p", help="Edit plan JSON", exists=True)]
ClipOpt = Annotated[list[str], typer.Option("--clip", "-c", help="clip_id=path (repeatable)")]


@app.command()
def render(
    plan: PlanOpt,
    brand: Annotated[Path, typer.Option("--brand", "-b", help="Brand kit JSON", exists=True)],
    clip: ClipOpt,
    out: Annotated[Path, typer.Option("--out", "-o", help="Output .mp4")] = Path("out.mp4"),
    quality: Annotated[Quality, typer.Option("--quality", "-q")] = Quality.FINAL,
    watermark: Annotated[bool, typer.Option("--watermark/--no-watermark")] = False,
    voiceover: Annotated[
        Path | None, typer.Option(help="Pre-recorded VO audio (Phase 1 testing)")
    ] = None,
    no_repair: Annotated[
        bool, typer.Option("--no-repair", help="Fail instead of auto-fixing")
    ] = False,
    no_face: Annotated[bool, typer.Option("--no-face", help="Disable face tracking")] = False,
    work_dir: Annotated[Path | None, typer.Option(help="Cache/work directory")] = None,
) -> None:
    """Render clips + edit plan + brand kit into a 9:16 video."""
    s = get_settings()
    updates: dict[str, object] = {}
    if work_dir:
        updates["work_dir"] = work_dir
    if no_face:
        updates["face_tracking"] = False
    s = s.model_copy(update=updates)
    req = RenderRequest(
        plan=_load_plan(plan),
        brand=_load_brand(brand),
        clips=_parse_clips(clip),
        output=out,
        quality=quality,
        watermark=watermark,
        voiceover_audio=voiceover,
        auto_repair=not no_repair,
    )
    last = [-1]

    def progress(stage: str, frac: float) -> None:
        pct = int(frac * 100)
        if pct != last[0] and sys.stderr.isatty():
            typer.echo(f"\r{stage:<10} {pct:3d}%", err=True, nl=False)
            last[0] = pct

    try:
        with Renderer(settings=s) as renderer:
            res = renderer.render(req, progress)
    except PlanError as exc:
        typer.secho("\nPlan cannot be rendered:", fg="red", err=True)
        for i in exc.issues:
            typer.secho(f"  {i}", fg="red", err=True)
        raise typer.Exit(2) from exc
    except (RenderError, FFmpegError, TemplateError) as exc:
        typer.secho(f"\nRender failed: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc
    if sys.stderr.isatty():
        typer.echo("", err=True)
    for w in res.warnings:
        typer.secho(f"warning: {w}", fg="yellow", err=True)
    typer.secho(
        f"✓ {res.output}  {res.width}x{res.height}  {res.duration:.2f}s  "
        f"{res.size_bytes / 1e6:.1f} MB  captions={len(res.captions)}  timings={res.timings}",
        fg="green",
    )


@app.command()
def make(
    template: Annotated[str, typer.Option("--template", "-t", help="product_promo | talking_head")],
    brand: Annotated[Path, typer.Option("--brand", "-b", help="Brand kit JSON", exists=True)],
    clip: ClipOpt,
    out: Annotated[Path, typer.Option("--out", "-o", help="Output .mp4")] = Path("out.mp4"),
    product: Annotated[str | None, typer.Option(help="Product name")] = None,
    price: Annotated[float | None, typer.Option(help="Price in ETB")] = None,
    old_price: Annotated[
        float | None, typer.Option(help="Old price in ETB (shows a discount)")
    ] = None,
    highlight: Annotated[str | None, typer.Option(help="What to highlight")] = None,
    target: Annotated[str | None, typer.Option(help="Target customer")] = None,
    cta: Annotated[str | None, typer.Option(help="Call to action text")] = None,
    notes: Annotated[str | None, typer.Option(help="Anything else the editor should know")] = None,
    voice: Annotated[str, typer.Option(help="none | standard | premium")] = "none",
    quality: Annotated[Quality, typer.Option("--quality", "-q")] = Quality.PREVIEW,
    watermark: Annotated[bool, typer.Option("--watermark/--no-watermark")] = False,
    fake: Annotated[
        bool, typer.Option("--fake", help="No API calls: fake ASR/TTS, heuristic planner")
    ] = False,
    no_face: Annotated[bool, typer.Option("--no-face", help="Disable face tracking")] = False,
    work_dir: Annotated[Path | None, typer.Option(help="Cache/work directory")] = None,
) -> None:
    """Edit raw clips automatically: transcribe, plan with AI, voice over, render. Writes
    OUT, OUT.plan.json (editable, re-render with `adreel render`) and OUT.cost.json."""
    from adreel.ai.base import ProviderError
    from adreel.autoedit.orchestrator import AutoEditError, AutoEditor, AutoEditRequest
    from adreel.autoedit.schema import BusinessContext
    from adreel.schemas.edit_plan import VoiceTier

    if voice not in ("none", "standard", "premium"):
        raise typer.BadParameter("--voice must be none, standard or premium")
    updates: dict[str, object] = {}
    if work_dir:
        updates["work_dir"] = work_dir
    if no_face:
        updates["face_tracking"] = False
    if fake:
        updates |= {
            "transcriber": "fake",
            "planner": "heuristic",
            "tts_standard": "fake",
            "tts_premium": "fake",
        }
    s = get_settings().model_copy(update=updates)
    try:
        ctx = BusinessContext(
            product_name=product,
            price=price,
            old_price=old_price,
            highlight=highlight,
            target_customer=target,
            cta=cta,
            notes=notes,
        )
    except ValidationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    req = AutoEditRequest(
        template_id=template,
        brand=_load_brand(brand),
        clips=_parse_clips(clip),
        context=ctx,
        output=out,
        voice=None if voice == "none" else VoiceTier(voice),
        quality=quality,
        watermark=watermark,
    )
    last = [""]

    def progress(stage: str, frac: float) -> None:
        line = f"{stage:<18} {int(frac * 100):3d}%"
        if line != last[0] and sys.stderr.isatty():
            typer.echo(f"\r{line}", err=True, nl=False)
            last[0] = line

    try:
        with AutoEditor(settings=s, renderer=Renderer(settings=s)) as editor:
            res = editor.run(req, progress)
    except (AutoEditError, ProviderError, RenderError, FFmpegError, TemplateError) as exc:
        typer.secho(f"\nFailed: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc
    except PlanError as exc:
        typer.secho(f"\nPlan cannot be rendered: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc
    if sys.stderr.isatty():
        typer.echo("", err=True)
    for w in res.warnings:
        typer.secho(f"warning: {w}", fg="yellow", err=True)
    r = res.render
    c = res.costs
    typer.secho(
        f"✓ {r.output}  {r.width}x{r.height}  {r.duration:.2f}s  {r.size_bytes / 1e6:.1f} MB",
        fg="green",
    )
    typer.echo(f"  planner: {res.planner_used} (attempts: {res.attempts})   timings: {res.timings}")
    typer.echo(f"  plan: {res.plan_path}   captions: {len(res.plan.captions)}")
    typer.echo(
        f"  cost: ${c.total_usd:.4f} (≈ {c.total_usd * c.prices.usd_to_etb:.2f} ETB)  "
        f"units: {c.totals()}  → {res.cost_path}"
    )


@app.command()
def transcribe(
    media: Annotated[Path, typer.Argument(help="Audio or video file", exists=True)],
    language: Annotated[str, typer.Option("--language", "-l")] = "am",
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write the transcript JSON here")
    ] = None,
    fake: Annotated[bool, typer.Option("--fake")] = False,
) -> None:
    """Transcribe one file with word timestamps and print it as caption lines."""
    import dataclasses

    from adreel.ai.base import ProviderError
    from adreel.ai.registry import build_transcriber
    from adreel.analysis import extract_speech_audio
    from adreel.autoedit.transcript_lines import build_transcript_lines

    s = get_settings().model_copy(update={"transcriber": "fake"} if fake else {})
    ff = FFmpeg(s.ffmpeg_bin, s.ffprobe_bin)
    duration = ff.probe(media).duration
    audio = extract_speech_audio(
        ff, media, s.work_dir.resolve() / "transcribe" / f"{media.stem}.mp3"
    )
    try:
        tr = build_transcriber(s, ff).transcribe(audio, language=language, duration_s=duration)
    except ProviderError as exc:
        typer.secho(f"Failed: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc
    for ln in build_transcript_lines({"x": tr}):
        typer.echo(f"{ln.start:7.2f}–{ln.end:7.2f}  {ln.text}")
    typer.echo(
        f"({len(tr.spoken)} words, language={tr.language}, {tr.audio_seconds:.1f}s audio)", err=True
    )
    if out:
        out.write_text(
            json.dumps(dataclasses.asdict(tr), ensure_ascii=False, indent=2), encoding="utf-8"
        )


@app.command()
def validate(
    plan: PlanOpt,
    clip: Annotated[list[str] | None, typer.Option("--clip", "-c", help="clip_id=path")] = None,
    show_repair: Annotated[bool, typer.Option("--repair", help="Print the repaired plan")] = False,
) -> None:
    """Validate an edit plan (shape; plus durations/template checks when clips are given)."""
    p = _load_plan(plan)
    s = get_settings()
    try:
        tpl = TemplateRegistry(s.templates_dirs).get(p.template_id)
    except TemplateError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc
    typer.secho(f"✓ schema OK ({len(p.segments)} segments, {len(p.captions)} captions)", fg="green")
    if not clip:
        return
    ff = FFmpeg(s.ffmpeg_bin, s.ffprobe_bin)
    durations = {cid: ff.probe(path).duration for cid, path in _parse_clips(clip).items()}
    issues = check_plan(p, tpl, durations)
    for i in issues:
        typer.secho(f"  {i}", fg="red" if i.severity == "error" else "yellow")
    if show_repair and issues:
        fixed, fixes = repair_plan(p, tpl, durations)
        for f in fixes:
            typer.echo(f"  fix: {f}")
        typer.echo(fixed.model_dump_json(indent=2, exclude_defaults=True))
    if any(i.severity == "error" for i in issues):
        raise typer.Exit(2)
    if not issues:
        typer.secho("✓ plan fits the clips and template", fg="green")


@app.command()
def schema(out: Annotated[Path | None, typer.Option("--out", "-o")] = None) -> None:
    """Print (or write) the edit-plan JSON Schema."""
    text = json.dumps(edit_plan_json_schema(), indent=2, ensure_ascii=False)
    if out:
        out.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)


@app.command()
def templates() -> None:
    """List available templates."""
    for t in TemplateRegistry(get_settings().templates_dirs).all():
        d = t.duration
        typer.echo(
            f"{t.id:<16} {t.name.en} / {t.name.am}  ({d.min_s:.0f}–{d.max_s:.0f}s)  "
            f"slots={t.required_slots}"
        )


@app.command()
def samples(
    out: Annotated[Path, typer.Option("--out", "-o")] = Path("examples/media"),
) -> None:
    """Generate synthetic test clips (60 fps portrait, silent landscape, 25 fps), logo, VO."""
    from adreel.samples import generate_samples

    s = get_settings()
    for p in generate_samples(FFmpeg(s.ffmpeg_bin, s.ffprobe_bin), out, s.fonts_dir):
        typer.echo(f"  {p}")


if __name__ == "__main__":  # pragma: no cover
    app()

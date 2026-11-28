"""Free-text brief (facts, template choice, prompt) and automatic colour correction."""

from __future__ import annotations

from pathlib import Path

import pytest

from adreel.ai.base import Transcript, TranscriptWord
from adreel.autoedit.brief import extract_price, first_phrase
from adreel.autoedit.orchestrator import choose_template
from adreel.autoedit.prompt import ClipAnalysis, build_prompt, build_system
from adreel.autoedit.schema import BusinessContext
from adreel.config import Settings
from adreel.render.color import ColorStats, Grade, correction, measure, parse_signalstats
from adreel.render.ffmpeg import FFmpeg
from adreel.render.pipeline import Quality, Renderer, RenderRequest
from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.template import TemplateSpec
from tests.conftest import make_plan, requires_ffmpeg

# ---------------------------------------------------------------- brief facts


@pytest.mark.parametrize(
    ("text", "price"),
    [
        ("ፈቃት ማስታወቂያ ነው፣ ዋጋው 500 ብር ነው", 500.0),
        ("ዋጋ: 1,200", 1200.0),
        ("price 99.50, two days only", 99.5),
        ("1200birr", 1200.0),
        ("ETB 3 500 only", 3500.0),
        ("ለ3 ቀን ብቻ ቅናሽ", None),  # a number that is not a price
        ("ፈጣን አድርጉት", None),
        (None, None),
    ],
)
def test_extract_price(text: str | None, price: float | None) -> None:
    assert extract_price(text) == price


def test_first_phrase() -> None:
    assert first_phrase("አዲስ ቡና ደርሷል። ዛሬውኑ ይምጡ") == "አዲስ ቡና ደርሷል"
    assert first_phrase("word " * 20, max_chars=12) == "word word"
    assert first_phrase("") is None


def _clip(cid: str, dur: float, speech: list[tuple[float, float]]) -> ClipAnalysis:
    words = tuple(TranscriptWord("ቃል", s, e) for s, e in speech)
    tr = Transcript("am", "", words, dur, "fake", "fake") if words else None
    return ClipAnalysis(cid, dur, 1080, 1920, Path("x"), transcript=tr)


def test_choose_template() -> None:
    talking = [_clip("c1", 10, [(i, i + 0.8) for i in range(9)])]
    shots = [_clip("c1", 10, [(0, 1)]), _clip("c2", 8, [])]
    with_price = BusinessContext(price=450)
    assert choose_template(talking, with_price) == "talking_head"
    assert choose_template(shots, with_price) == "product_promo"
    assert choose_template(shots, BusinessContext()) == "talking_head"  # no price: no price card


def test_prompt_leads_with_the_brief(promo_tpl: TemplateSpec, brand: BrandKit) -> None:
    ctx = BusinessContext(brief="ፈቃት ማስታወቂያ፣ ዋጋው 500 ብር፣ ለወጣቶች", price=500)
    p = build_prompt(
        template=promo_tpl,
        brand=brand,
        context=ctx,
        clips=[_clip("c1", 5, [])],
        lines=[],
        want_voiceover=False,
        language="am",
    )
    assert p.startswith("## What the business wants")
    assert "ፈቃት ማስታወቂያ፣ ዋጋው 500 ብር፣ ለወጣቶች" in p
    system = build_system("am")
    assert "You write ALL the on-screen text yourself" in system and "look:" in system


# ---------------------------------------------------------------- colour maths


def S(yavg: float, ylow: float = 30, yhigh: float = 215, sat: float = 38) -> ColorStats:
    return ColorStats(yavg, ylow, yhigh, sat, 10)


def test_parse_signalstats() -> None:
    text = "\n".join(
        f"frame:{i}\nlavfi.signalstats.YAVG={y}\nlavfi.signalstats.YLOW=20\n"
        f"lavfi.signalstats.YHIGH=200\nlavfi.signalstats.SATAVG=30.5"
        for i, y in enumerate((100, 120))
    )
    assert parse_signalstats(text) == ColorStats(110.0, 20.0, 200.0, 30.5, 2)
    assert parse_signalstats("nothing") is None


def test_dark_footage_is_lifted_bright_is_calmed_ok_is_left() -> None:
    assert correction(S(60)).gamma > 1.2
    assert correction(S(190)).gamma < 0.95
    assert correction(S(16 + 0.47 * 219)).gamma == 1.0  # inside the dead band
    assert correction(S(5)).gamma == 1.45  # clamped


def test_contrast_and_saturation() -> None:
    assert correction(S(110, ylow=80, yhigh=160)).contrast > 1.2  # flat
    assert correction(S(110)).contrast == 1.0
    assert correction(S(110, sat=12)).saturation > 1.2  # dull
    assert correction(S(110, sat=100)).saturation < 1.0  # garish


def test_looks() -> None:
    base = correction(S(110))
    assert correction(S(110), "vivid").saturation > base.saturation
    warm = correction(S(110), "warm")
    assert warm.warmth > 0 and "colorbalance=rm=0.050:bm=-0.050" in warm.filter()
    assert correction(S(110), "cool").warmth < 0
    assert correction(S(30), "none").is_identity and correction(None, "none").filter() == ""
    assert correction(None, "vivid").filter() == "eq=contrast=1.070:saturation=1.220"
    two_pass = correction(S(60, ylow=20, yhigh=120)).filter()
    assert two_pass.startswith("eq=gamma=") and ",eq=contrast=" in two_pass  # lift, then contrast
    assert Grade().filter() == ""


# ---------------------------------------------------------------- real renders


def _yavg(ff: FFmpeg, path: Path, tmp: Path) -> float:
    stats = measure(ff, path)  # caches next to the file
    assert stats is not None
    return stats.yavg


@requires_ffmpeg
@pytest.mark.ffmpeg
def test_dark_clip_is_brightened_in_the_render(tmp_path: Path) -> None:
    ff = FFmpeg()
    dark = tmp_path / "dark.mp4"
    ff.run(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=360x640:r=30:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=d=3",
            "-vf",
            "lutyuv=y=val*0.35",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(dark),
        ]
    )
    s = Settings(work_dir=tmp_path / "work", face_tracking=False)
    brand = BrandKit(business_name="x")
    outs = {}
    for look in ("none", "natural"):
        plan = make_plan([("a", "c1", 0.0, 2.5)]).model_copy(update={"look": look})
        out = tmp_path / f"{look}.mp4"
        with Renderer(settings=s, face_tracker=None) as r:
            r.render(
                RenderRequest(
                    plan=plan, brand=brand, clips={"c1": dark}, output=out, quality=Quality.PREVIEW
                )
            )
        # measure only the main part (before the end card)
        main = tmp_path / f"{look}_main.mp4"
        ff.run(["-i", str(out), "-t", "2", "-c", "copy", str(main)])
        outs[look] = _yavg(ff, main, tmp_path)
    assert outs["natural"] > outs["none"] + 15

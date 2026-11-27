"""Planning logic: transcript lines, smart cuts, prompt, assembly, heuristic planner."""

from __future__ import annotations

from pathlib import Path

import pytest

from adreel.ai.base import Transcript, TranscriptWord
from adreel.autoedit.assemble import assemble_plan
from adreel.autoedit.cuts import is_filler, refine_segments
from adreel.autoedit.heuristic import default_voiceover, heuristic_plan
from adreel.autoedit.prompt import ClipAnalysis, build_prompt, build_system
from adreel.autoedit.schema import BusinessContext, CaptionFix, PlannedSegment, PlannerOutput
from adreel.autoedit.transcript_lines import (
    build_transcript_lines,
    finalize_captions,
    lines_from_words,
    split_lines,
    to_caption_lines,
)
from adreel.plan_checks import check_plan, planned_duration
from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.edit_plan import SegmentRole, TemplateSlots
from adreel.schemas.template import TemplateSpec


def W(text: str, s: float, e: float) -> TranscriptWord:
    return TranscriptWord(text, s, e)


def tr(words: list[TranscriptWord], dur: float = 10.0) -> Transcript:
    return Transcript("am", " ".join(w.text for w in words), tuple(words), dur, "fake", "fake")


SPEECH = [
    W("ሰላም!", 0.5, 0.9),
    W("እኔ", 1.0, 1.3),
    W("አበበ", 1.35, 1.8),
    W("እባላለሁ።", 1.85, 2.4),
    W("እ...", 2.6, 3.0),
    W("የቡና", 3.2, 3.6),
    W("ቤት", 3.65, 3.9),
    W("አለኝ", 5.0, 5.5),
]


# ---------------------------------------------------------------- transcript lines


def test_split_lines_on_punctuation_gaps_and_length() -> None:
    lines = split_lines(SPEECH, max_units=40)
    assert [" ".join(w.text for w in ln) for ln in lines] == [
        "ሰላም!",
        "እኔ አበበ እባላለሁ።",
        "እ... የቡና ቤት",
        "አለኝ",
    ]
    assert len(split_lines(SPEECH[1:4], max_units=6)) == 2  # too wide for one line


def test_transcript_line_ids_are_global_and_ordered() -> None:
    lines = build_transcript_lines({"c2": tr(SPEECH[:2]), "c1": tr(SPEECH[5:7])})
    assert [(ln.id, ln.clip_id) for ln in lines] == [(1, "c1"), (2, "c2"), (3, "c2")]


def test_caption_fixes_keep_word_timings_when_word_count_matches() -> None:
    lines = build_transcript_lines({"c1": tr(SPEECH[1:4])})
    same = to_caption_lines(lines, {1: "እኔ አበበ እባላለሁ"})[0]
    assert same.text == "እኔ አበበ እባላለሁ" and [w.text for w in same.words] == ["እኔ", "አበበ", "እባላለሁ"]
    assert same.words[1].start == 1.35
    diff = to_caption_lines(lines, {1: "እኔ አበበ ነኝ እባላለሁ"})[0]
    assert diff.words == [] and (diff.start, diff.end) == (1.0, 2.4)


def test_voiceover_lines_are_output_timebase() -> None:
    lines = lines_from_words([W("ሀ።", 0.0, 0.4), W("ለ", 0.6, 1.0)], offset=0.3, max_units=20)
    caps = to_caption_lines(lines)
    assert [c.timebase.value for c in caps] == ["output", "output"] and caps[0].clip_id is None
    assert caps[0].start == pytest.approx(0.3) and caps[1].end == pytest.approx(1.3)


# ---------------------------------------------------------------- cuts


def test_is_filler() -> None:
    assert is_filler("እ...") and is_filler("ኧ፣") and is_filler("Um")
    assert not is_filler("እኔ") and not is_filler("አበበ")


def seg(
    id_: str, s: float, e: float, role: SegmentRole = SegmentRole.SPEAKER, clip: str = "c1"
) -> PlannedSegment:
    return PlannedSegment(id=id_, clip_id=clip, start=s, end=e, role=role)


def test_tighten_removes_pauses_and_fillers() -> None:
    out = refine_segments([seg("s1", 0.0, 6.0)], {"c1": SPEECH}, {"c1": 10.0})
    spans = [(o.id, o.start, o.end) for o in out]
    # pieces: ሰላም..እባላለሁ | (filler cut) የቡና ቤት | (1.1 s pause) አለኝ
    assert spans == [("s1", 0.42, 2.48), ("s1_2", 3.12, 3.98), ("s1_3", 4.92, 5.58)]


def test_tighten_keeps_segment_without_words() -> None:
    s = seg("s1", 6.0, 9.0)
    assert refine_segments([s], {"c1": SPEECH}, {"c1": 10.0}) == [s]


def test_snap_moves_to_nearest_word_edge() -> None:
    a = seg("a", 0.0, 1.25, SegmentRole.PRODUCT)  # ends early in "እኔ" (1.0–1.3) -> after the word
    b = seg("b", 1.25, 3.0, SegmentRole.PRODUCT)  # starts late in "እኔ" -> after the word
    c = seg("c", 1.05, 3.0, SegmentRole.PRODUCT)  # starts early in "እኔ" -> before the word
    out = refine_segments([a, b, c], {"c1": SPEECH}, {"c1": 10.0})
    assert out[0].end == pytest.approx(1.33) and out[1].start == pytest.approx(1.33)
    assert out[2].start == pytest.approx(0.97)


# ---------------------------------------------------------------- prompt


@pytest.fixture
def clips(tmp_path: Path) -> list[ClipAnalysis]:
    return [
        ClipAnalysis("c1", 10.0, 1920, 1080, tmp_path / "c1.mp4", [(2.4, 3.2)], [4.0], tr(SPEECH)),
        ClipAnalysis("c2", 6.0, 1080, 1920, tmp_path / "c2.mp4"),
    ]


def test_prompt_contains_what_the_llm_needs(
    clips: list[ClipAnalysis], promo_tpl: TemplateSpec, brand: BrandKit
) -> None:
    lines = build_transcript_lines({"c1": clips[0].transcript})  # type: ignore[dict-item]
    ctx = BusinessContext(product_name="ቡና", price=1200, highlight="ትኩስ")
    p = build_prompt(
        template=promo_tpl,
        brand=brand,
        context=ctx,
        clips=clips,
        lines=lines,
        want_voiceover=True,
        language="am",
    )
    for needle in (
        "Price (ETB): 1200",
        "Clip c1: 10.0 s, landscape",
        "Clip c2: 6.0 s, portrait",
        "no speech",
        "[L1] c1 0.50-0.90: ሰላም!",
        "shot changes at: 4.0",
        "silent: 2.4-3.2",
        "Segments must total 17–27 s",
        "at most 35 words",
        "Required slots",
    ):
        assert needle in p, needle
    p2 = build_prompt(
        template=promo_tpl,
        brand=brand,
        context=ctx,
        clips=clips,
        lines=[],
        want_voiceover=False,
        language="am",
    )
    assert "leave voiceover_script null" in p2 and "no speech in these clips" in p2
    assert "Amharic" in build_system("am") and "English" in build_system("en")


# ---------------------------------------------------------------- assembly


def test_assemble_merges_facts_from_context(promo_tpl: TemplateSpec) -> None:
    out = PlannerOutput(
        segments=[
            seg("s1", 0.0, 3.0, SegmentRole.HOOK),
            seg("s2", 4.0, 9.0, SegmentRole.PRODUCT),
            seg("x", 0.0, 2.0, SegmentRole.PRODUCT, clip="c9"),
        ],
        slots=TemplateSlots(product_name="Wrong", price=1, hook_text="ትኩስ!"),
        price_card_segment_id="nope",
        caption_fixes=[CaptionFix(id=2, text="እኔ አበበ እባላለሁ")],
        on_screen_text=[{"text": "አዲስ", "segment_id": "s2"}, {"text": "x", "segment_id": "gone"}],  # type: ignore[list-item]
    )
    ctx = BusinessContext(product_name="ቡና", price=1200, old_price=1500, cta="ይዘዙ")
    lines = build_transcript_lines({"c1": tr(SPEECH)})
    plan = assemble_plan(
        out,
        template=promo_tpl,
        language="am",
        context=ctx,
        lines=lines,
        words_by_clip={"c1": tuple(SPEECH)},
        clip_durations={"c1": 10.0},
    )
    assert [s.id for s in plan.segments] == ["s1", "s2"]  # unknown clip dropped
    assert (
        plan.slots.product_name == "ቡና"
        and plan.slots.price == 1200
        and plan.slots.old_price == 1500
    )
    assert plan.slots.hook_text == "ትኩስ!" and plan.slots.cta_text == "ይዘዙ"
    assert plan.price_card.enabled and plan.price_card.segment_id is None
    assert [t.text for t in plan.on_screen_text] == ["አዲስ"]
    assert next(c for c in plan.captions if c.id == 2).text == "እኔ አበበ እባላለሁ"


def test_finalize_captions_drops_invisible_and_renumbers(talk_tpl: TemplateSpec) -> None:
    out = PlannerOutput(segments=[seg("s1", 3.0, 6.0), seg("s0", 0.0, 2.5)])
    lines = build_transcript_lines({"c1": tr(SPEECH)})
    plan = assemble_plan(
        out,
        template=talk_tpl,
        language="am",
        context=BusinessContext(),
        lines=lines,
        words_by_clip={"c1": tuple(SPEECH)},
        clip_durations={"c1": 10.0},
    )
    fin = finalize_captions(plan, 30)
    texts = [(c.id, c.text) for c in fin.captions]
    # output order: s1 material (የቡና ቤት..., አለኝ) comes first, then s0 (ሰላም!, እኔ አበበ...)
    assert [i for i, _ in texts] == list(range(1, len(texts) + 1))
    assert texts[0][1].startswith("እ... የቡና") and texts[-1][1] == "እኔ አበበ እባላለሁ።"


# ---------------------------------------------------------------- heuristic planner


def test_heuristic_promo_fits_template(tmp_path: Path, promo_tpl: TemplateSpec) -> None:
    clips = [
        ClipAnalysis("c1", 20.0, 1920, 1080, tmp_path / "a"),
        ClipAnalysis("c2", 12.0, 1080, 1920, tmp_path / "b"),
    ]
    durations = {"c1": 20.0, "c2": 12.0}
    ctx = BusinessContext(product_name="ቡና", price=1200, highlight="ትኩስ ቡና")
    out = heuristic_plan(promo_tpl, ctx, clips, want_voiceover=True, language="am")
    plan = assemble_plan(
        out,
        template=promo_tpl,
        language="am",
        context=ctx,
        lines=[],
        words_by_clip={},
        clip_durations=durations,
    )
    assert plan.segments[0].role is SegmentRole.HOOK
    assert promo_tpl.duration.min_s <= planned_duration(plan, promo_tpl) <= promo_tpl.duration.max_s
    assert not [i for i in check_plan(plan, promo_tpl, durations) if i.severity == "error"]
    assert out.voiceover_script and "1,200 ብር" in out.voiceover_script
    assert plan.price_card.segment_id == plan.segments[-1].id


def test_heuristic_promo_with_little_footage_uses_what_exists(
    clips: list[ClipAnalysis], promo_tpl: TemplateSpec
) -> None:
    out = heuristic_plan(
        promo_tpl, BusinessContext(price=1), clips, want_voiceover=False, language="am"
    )
    used = sum(s.end - s.start for s in out.segments)
    assert 13.0 <= used <= 16.0  # nearly all of the 16 s, never invented


def test_heuristic_talking_head(clips: list[ClipAnalysis], talk_tpl: TemplateSpec) -> None:
    out = heuristic_plan(talk_tpl, BusinessContext(), clips, want_voiceover=False, language="am")
    assert [s.role for s in out.segments] == [SegmentRole.SPEAKER, SegmentRole.BROLL]
    assert out.segments[0].start == pytest.approx(0.4) and out.voiceover_script is None


def test_default_voiceover_languages() -> None:
    ctx = BusinessContext(product_name="Coffee", price=99.5)
    assert default_voiceover(ctx, "en") == "Coffee! Only 99.50 birr. Order now!"
    assert default_voiceover(BusinessContext(), "am") == "አሁኑኑ ይዘዙ!"

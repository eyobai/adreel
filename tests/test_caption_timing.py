from __future__ import annotations

import itertools

import pytest

from adreel.render.captions import (
    CaptionEvent,
    CaptionLayout,
    TimedWord,
    caption_listing,
    chunk_tokens,
    layout,
    map_to_output,
    merge_intervals,
    text_units,
    tokenize,
    wrap_lines,
)
from adreel.render.timeline import build_timeline
from tests.conftest import make_plan

FPS = 30


def cap(id_: int, text: str, start: float, end: float, clip: str = "c1", words=None):  # type: ignore[no-untyped-def]
    d = {"id": id_, "text": text, "clip_id": clip, "start": start, "end": end}
    if words:
        d["words"] = [{"text": t, "start": s, "end": e} for t, s, e in words]
    return d


# ---------------------------------------------------------------- timeline


def test_timeline_is_frame_quantised() -> None:
    plan = make_plan([("a", "c1", 0.0, 1.01), ("b", "c1", 2.0, 3.333)])
    tl = build_timeline(plan, FPS, end_card_s=2.5)
    assert [p.frames for p in tl.placed] == [30, 40]
    assert tl.placed[1].out_start == pytest.approx(1.0)
    assert tl.main_duration == pytest.approx(70 / 30)
    assert tl.total_duration == pytest.approx(70 / 30 + 2.5)


def test_source_to_output_across_a_removed_silence() -> None:
    plan = make_plan([("a", "c1", 0.0, 1.4), ("b", "c1", 2.0, 3.5)])
    tl = build_timeline(plan, FPS)
    # source 1.0–2.5 spans the cut (1.4–2.0 removed)
    pieces = tl.source_to_output("c1", 1.0, 2.5)
    assert pieces == [pytest.approx((1.0, 1.4)), pytest.approx((1.4, 1.9))]
    assert tl.source_to_output("c1", 1.5, 1.9) == []  # fully cut
    assert tl.source_to_output("c2", 0, 1) == []  # other clip


# ---------------------------------------------------------------- mapping


def test_caption_spanning_cut_is_merged_into_one_event() -> None:
    plan = make_plan(
        [("a", "c1", 0.0, 1.4), ("b", "c1", 2.0, 3.5)], [cap(1, "ሰላም እኔ አበበ ነኝ", 0.2, 3.0)]
    )
    ev = map_to_output(plan.captions, build_timeline(plan, FPS))
    assert len(ev) == 1
    assert (ev[0].start, ev[0].end) == pytest.approx((0.2, 2.4))
    assert ev[0].text == "ሰላም እኔ አበበ ነኝ"


def test_cut_words_disappear_from_caption_text() -> None:
    words = [("የቡና", 4.0, 4.5), ("እ...", 4.6, 5.0), ("ቤታችን", 5.1, 5.6)]
    plan = make_plan(
        [("a", "c1", 3.9, 4.55), ("b", "c1", 5.05, 6.0)],
        [cap(1, "የቡና እ... ቤታችን", 4.0, 5.6, words=words)],
    )
    ev = map_to_output(plan.captions, build_timeline(plan, FPS))
    assert [e.text for e in ev] == ["የቡና ቤታችን"]
    assert [w.text for w in ev[0].words] == ["የቡና", "ቤታችን"]
    # "ቤታችን" now starts right after the cut
    assert ev[0].words[1].start == pytest.approx(0.65 + 0.05, abs=0.04)


def test_word_kept_only_if_mostly_visible() -> None:
    words = [("ሀ", 0.0, 0.4), ("ለ", 0.8, 1.2)]  # second word only 25% visible
    plan = make_plan([("a", "c1", 0.0, 0.9)], [cap(1, "ሀ ለ", 0.0, 1.2, words=words)])
    ev = map_to_output(plan.captions, build_timeline(plan, FPS))
    assert [e.text for e in ev] == ["ሀ"]


def test_natural_pause_does_not_split_caption() -> None:
    words = [("ለ3", 4.0, 4.4), ("ቀን", 4.45, 4.8), ("ልዩ", 6.0, 6.4), ("ቅናሽ", 6.45, 7.2)]
    plan = make_plan([("a", "c1", 3.5, 8.0)], [cap(1, "ለ3 ቀን ልዩ ቅናሽ", 4.0, 7.2, words=words)])
    ev = map_to_output(plan.captions, build_timeline(plan, FPS))
    assert len(ev) == 1 and ev[0].text == "ለ3 ቀን ልዩ ቅናሽ"


def test_inserted_broll_splits_caption() -> None:
    words = [("ሀ", 1.0, 1.4), ("ለ", 1.5, 1.9), ("ሐ", 2.6, 3.0)]
    plan = make_plan(
        [("a", "c1", 0.5, 2.0), ("br", "c2", 0.0, 2.0), ("b", "c1", 2.5, 3.5)],
        [cap(1, "ሀ ለ ሐ", 1.0, 3.0, words=words)],
    )
    ev = map_to_output(plan.captions, build_timeline(plan, FPS))
    assert [e.text for e in ev] == ["ሀ ለ", "ሐ"]
    assert ev[1].start == pytest.approx(1.5 + 2.0 + 0.1)


def test_reordered_segments_without_words_use_longest_piece() -> None:
    plan = make_plan([("b", "c1", 2.0, 4.0), ("a", "c1", 0.0, 1.0)], [cap(1, "ሀለሐ", 0.5, 3.0)])
    ev = map_to_output(plan.captions, build_timeline(plan, FPS))
    assert len(ev) == 1
    assert (ev[0].start, ev[0].end) == pytest.approx((0.0, 1.0))  # 2.0–3.0 → out 0–1


def test_fully_cut_caption_is_dropped() -> None:
    plan = make_plan([("a", "c1", 0.0, 1.0)], [cap(1, "ሀ", 5.0, 6.0)])
    assert map_to_output(plan.captions, build_timeline(plan, FPS)) == []


def test_output_timebase_passes_through() -> None:
    plan = make_plan(
        [("a", "c1", 0.0, 5.0)],
        [{"id": 1, "text": "ድምፅ", "timebase": "output", "start": 1.0, "end": 2.0}],
    )
    ev = map_to_output(plan.captions, build_timeline(plan, FPS))
    assert (ev[0].start, ev[0].end, ev[0].text) == (1.0, 2.0, "ድምፅ")


# ---------------------------------------------------------------- text measuring & wrapping


def test_units_ethiopic_wider_than_latin() -> None:
    assert text_units("ሀለሐመ") == pytest.approx(4.0)
    assert text_units("abcd") < text_units("ሀለሐመ")
    assert text_units("ABCD") > text_units("abcd")


def test_tokenize_handles_ethiopic_wordspace() -> None:
    assert tokenize("ሰላም፡ዓለም  ዛሬ") == ["ሰላም፡", "ዓለም", "ዛሬ"]


def test_wrap_short_line_untouched() -> None:
    assert wrap_lines("ዛሬ አዲስ", 10, 2) == ["ዛሬ አዲስ"]


def test_wrap_is_balanced() -> None:
    lines = wrap_lines("ዛሬ አዲስ የኢትዮጵያ ቡና ደርሷል", 14, 2)
    assert len(lines) == 2
    widths = [text_units(ln) for ln in lines]
    assert max(widths) <= 14
    assert abs(widths[0] - widths[1]) < 6  # no "long line + one word"


def test_wrap_overlong_word_overflows_gracefully() -> None:
    assert wrap_lines("ሀ" * 30, 10, 2) == ["ሀ" * 30]


def test_chunking_keeps_each_chunk_within_max_lines() -> None:
    text = "አንድ ሁለት ሶስት አራት አምስት ስድስት ሰባት ስምንት ዘጠኝ አስር አስራ አንድ"
    chunks = chunk_tokens(tokenize(text), 8, 2)
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) == len(tokenize(text))
    for c in chunks:
        assert len(wrap_lines(" ".join(c), 8, 2)) <= 2


def test_merge_intervals() -> None:
    assert merge_intervals([(2, 3), (0, 1), (1.05, 1.5)], 0.1) == [(0, 1.5), (2, 3)]


# ---------------------------------------------------------------- layout

CFG = CaptionLayout(
    max_units_per_line=10, max_lines=2, min_duration_s=0.8, min_visible_s=0.25, gap_s=0.04
)


def ev(i: int, s: float, e: float, text: str = "ሀለ", words=()) -> CaptionEvent:  # type: ignore[no-untyped-def]
    return CaptionEvent(i, s, e, text, tuple(words))


def test_layout_extends_short_caption_into_gap() -> None:
    out = layout([ev(1, 1.0, 1.2), ev(2, 3.0, 4.0)], CFG, 10)
    assert out[0].end == pytest.approx(1.8)


def test_layout_short_caption_limited_by_next_then_extends_back() -> None:
    out = layout([ev(1, 0.5, 0.7), ev(2, 0.9, 2.0)], CFG, 10)
    assert out[0].end == pytest.approx(0.86)
    assert out[0].start == pytest.approx(0.06)
    assert out[0].end <= out[1].start


def test_layout_resolves_overlaps() -> None:
    out = layout([ev(1, 0.0, 2.0), ev(2, 1.5, 3.0)], CFG, 10)
    assert out[0].end == pytest.approx(1.46)
    assert all(a.end <= b.start for a, b in itertools.pairwise(out))


def test_layout_drops_slivers() -> None:
    out = layout([ev(1, 0.0, 1.0), ev(2, 0.1, 2.0)], CFG, 10)
    assert [e.caption_id for e in out] == [2]


def test_layout_never_runs_into_end_card() -> None:
    out = layout([ev(1, 9.5, 12.0), ev(2, 11.0, 13.0)], CFG, 10.0)
    assert [e.caption_id for e in out] == [1]
    assert out[-1].end <= 10.0


def test_layout_splits_long_caption_by_word_timing() -> None:
    texts = ["አንድ", "ሁለት", "ሶስት", "አራት", "አምስት", "ስድስት", "ሰባት", "ስምንት"]
    words = [TimedWord(t, i * 0.5, i * 0.5 + 0.4) for i, t in enumerate(texts)]
    out = layout([ev(1, 0.0, 4.0, " ".join(texts), words)], CFG, 10)
    assert len(out) >= 2
    assert out[0].start == 0.0 and out[-1].end == pytest.approx(4.0)
    assert " ".join(e.text for e in out) == " ".join(texts)
    for e in out:
        assert 1 <= len(e.lines) <= 2
        assert all(e.start <= w.start and w.end <= e.end for w in e.words)
    # continuous: each chunk stays until the next starts
    assert all(a.end <= b.start + 1e-9 for a, b in itertools.pairwise(out))


def test_layout_splits_long_caption_proportionally_without_words() -> None:
    text = "አንድ ሁለት ሶስት አራት አምስት ስድስት ሰባት ስምንት ዘጠኝ"
    out = layout([ev(1, 2.0, 6.0, text)], CFG, 10)
    assert len(out) >= 2
    assert out[0].start == pytest.approx(2.0) and out[-1].end == pytest.approx(6.0, abs=0.05)


def test_caption_listing_for_fix_flow() -> None:
    plan = make_plan([("a", "c1", 0, 5)], [cap(2, "ለ", 1, 2), cap(1, "ሀ", 0, 1)])
    assert caption_listing(plan.captions) == "1: ሀ\n2: ለ"

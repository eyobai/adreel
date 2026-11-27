"""Output timeline: where each source segment lands, frame-accurately.

Every segment duration is quantised to whole frames and positions are accumulated in
integer frames, so caption/overlay times computed here match the rendered video exactly
(no float drift over 30+ cuts).
"""

from __future__ import annotations

from dataclasses import dataclass

from adreel.schemas.edit_plan import EditPlan, Segment


def to_frames(seconds: float, fps: int) -> int:
    return max(1, round(seconds * fps))


@dataclass(frozen=True)
class PlacedSegment:
    segment: Segment
    index: int
    frame_start: int
    frames: int
    fps: int

    @property
    def out_start(self) -> float:
        return self.frame_start / self.fps

    @property
    def duration(self) -> float:
        return self.frames / self.fps

    @property
    def out_end(self) -> float:
        return (self.frame_start + self.frames) / self.fps

    @property
    def src_start(self) -> float:
        return self.segment.start

    @property
    def src_end(self) -> float:
        """Source out-point actually used (after quantisation)."""
        return self.segment.start + self.duration


@dataclass(frozen=True)
class Timeline:
    fps: int
    placed: tuple[PlacedSegment, ...]
    end_card_frames: int

    @property
    def main_frames(self) -> int:
        return sum(p.frames for p in self.placed)

    @property
    def main_duration(self) -> float:
        return self.main_frames / self.fps

    @property
    def end_card_duration(self) -> float:
        return self.end_card_frames / self.fps

    @property
    def total_duration(self) -> float:
        return (self.main_frames + self.end_card_frames) / self.fps

    def window(self, segment_id: str) -> tuple[float, float]:
        for p in self.placed:
            if p.segment.id == segment_id:
                return p.out_start, p.out_end
        raise KeyError(segment_id)

    def source_to_output(self, clip_id: str, start: float, end: float) -> list[tuple[float, float]]:
        """Map a source interval to the output intervals where it is visible (sorted)."""
        out: list[tuple[float, float]] = []
        for p in self.placed:
            if p.segment.clip_id != clip_id:
                continue
            s, e = max(start, p.src_start), min(end, p.src_end)
            if e > s:
                out.append((p.out_start + (s - p.src_start), p.out_start + (e - p.src_start)))
        return sorted(out)

    def source_point_to_output(self, clip_id: str, t: float) -> float | None:
        """Output time of a source instant (first placement that contains it)."""
        for p in self.placed:
            if p.segment.clip_id == clip_id and p.src_start <= t < p.src_end:
                return p.out_start + (t - p.src_start)
        return None


def build_timeline(plan: EditPlan, fps: int, end_card_s: float = 0.0) -> Timeline:
    placed: list[PlacedSegment] = []
    cursor = 0
    for i, seg in enumerate(plan.segments):
        n = to_frames(seg.duration, fps)
        placed.append(PlacedSegment(seg, i, cursor, n, fps))
        cursor += n
    end_frames = round(end_card_s * fps) if end_card_s > 0 else 0
    return Timeline(fps=fps, placed=tuple(placed), end_card_frames=end_frames)

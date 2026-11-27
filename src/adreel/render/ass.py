"""Minimal, correct ASS (Advanced SubStation Alpha) writer for libass.

Why ASS for *all* text (captions, price card, end card, watermark): one text engine with
proper complex-script shaping (libass + HarfBuzz + FriBidi), outlines, boxes, vector
shapes and animation tags. drawtext can't shape or fall back between fonts.

Ethiopic gotcha: Noto Sans Ethiopic has no Latin letters, ASCII digits or ``! ? . ,``.
Amharic marketing copy mixes them constantly ("ዋጋ 1,200 ብር!"), so ``font_runs`` tags each
script run with an explicit ``\\fn``. Deterministic on every host, no fontconfig fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from adreel.render.captions import is_ethiopic

ETHIOPIC_FONT = "Noto Sans Ethiopic"
LATIN_FONT = "Noto Sans"


def ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def alpha_byte(opacity: float) -> int:
    """ASS alpha is inverted: 00 = opaque, FF = transparent."""
    return round(255 * (1 - min(1.0, max(0.0, opacity))))


def style_color(hex_color: str, opacity: float = 1.0) -> str:
    """Colour for a Style line: &HAABBGGRR."""
    r, g, b = _rgb(hex_color)
    return f"&H{alpha_byte(opacity):02X}{b:02X}{g:02X}{r:02X}"


def tag_color(hex_color: str) -> str:
    """Colour for override tags (\\c, \\3c ...): &HBBGGRR&."""
    r, g, b = _rgb(hex_color)
    return f"&H{b:02X}{g:02X}{r:02X}&"


def tag_alpha(opacity: float) -> str:
    return f"&H{alpha_byte(opacity):02X}&"


def _renderable(ch: str) -> bool:
    """libass cannot draw colour emoji; drop them (and variation selectors / ZWJ)."""
    cp = ord(ch)
    return not (
        0x1F000 <= cp <= 0x1FAFF  # emoji & pictographs
        or 0x2600 <= cp <= 0x27BF  # misc symbols, dingbats
        or 0xFE00 <= cp <= 0xFE0F  # variation selectors
        or cp in (0x200D, 0x20E3)  # ZWJ, keycap
    )


def escape(text: str) -> str:
    """Make arbitrary user text inert: braces start override blocks, backslash escapes."""
    text = "".join(ch for ch in text if _renderable(ch))
    return (
        text.replace("\\", "/")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\r\n", "\n")
        .replace("\n", " ")
    )


def font_runs(text: str, ethiopic_font: str = ETHIOPIC_FONT, latin_font: str = LATIN_FONT) -> str:
    """Escape ``text`` and insert ``{\\fn...}`` at every Ethiopic <-> non-Ethiopic switch.

    Whitespace stays with the current run. The first run is always tagged explicitly so the
    result is independent of the style's default font.
    """
    out: list[str] = []
    current: str | None = None
    buf = ""
    for ch in escape(text):
        if ch.isspace():
            buf += ch
            continue
        font = ethiopic_font if is_ethiopic(ch) else latin_font
        if font != current:
            if buf:
                out.append(buf)
                buf = ""
            out.append(f"{{\\fn{font}}}")
            current = font
        buf += ch
    out.append(buf)
    return "".join(out)


def rounded_rect(w: float, h: float, r: float) -> str:
    """ASS vector drawing (\\p1) of a w×h rectangle with corner radius r, origin top-left."""
    r = max(0.0, min(r, w / 2, h / 2))
    k = r * 0.5523  # cubic Bézier circle approximation

    def f(v: float) -> str:
        return f"{v:.0f}"

    return " ".join(
        [
            f"m {f(r)} 0",
            f"l {f(w - r)} 0",
            f"b {f(w - r + k)} 0 {f(w)} {f(r - k)} {f(w)} {f(r)}",
            f"l {f(w)} {f(h - r)}",
            f"b {f(w)} {f(h - r + k)} {f(w - r + k)} {f(h)} {f(w - r)} {f(h)}",
            f"l {f(r)} {f(h)}",
            f"b {f(r - k)} {f(h)} 0 {f(h - r + k)} 0 {f(h - r)}",
            f"l 0 {f(r)}",
            f"b 0 {f(r - k)} {f(r - k)} 0 {f(r)} 0",
        ]
    )


@dataclass(frozen=True)
class AssStyle:
    name: str
    size: float
    primary: str = "&H00FFFFFF"
    secondary: str = "&H00FFFFFF"
    outline: str = "&H00000000"
    back: str = "&H80000000"
    font: str = ETHIOPIC_FONT
    bold: bool = True
    border_style: int = 1  # 1 = outline + shadow, 3 = opaque box
    outline_w: float = 4
    shadow: float = 0
    alignment: int = 2  # numpad layout: 2 = bottom centre, 5 = middle centre, 8 = top centre
    margin_l: int = 0
    margin_r: int = 0
    margin_v: int = 0

    def line(self) -> str:
        return (
            f"Style: {self.name},{self.font},{self.size:g},{self.primary},{self.secondary},"
            f"{self.outline},{self.back},{-1 if self.bold else 0},0,0,0,100,100,0,0,"
            f"{self.border_style},{self.outline_w:g},{self.shadow:g},{self.alignment},"
            f"{self.margin_l},{self.margin_r},{self.margin_v},1"
        )


@dataclass(frozen=True)
class AssEvent:
    start: float
    end: float
    style: str
    text: str  # already escaped / tagged
    layer: int = 0

    def line(self) -> str:
        return (
            f"Dialogue: {self.layer},{ass_time(self.start)},{ass_time(self.end)},"
            f"{self.style},,0,0,0,,{self.text}"
        )


@dataclass
class AssDocument:
    width: int
    height: int
    styles: dict[str, AssStyle] = field(default_factory=dict)
    events: list[AssEvent] = field(default_factory=list)

    def add_style(self, style: AssStyle) -> None:
        self.styles[style.name] = style

    def add(self, event: AssEvent) -> None:
        if event.style not in self.styles:
            raise KeyError(f"unknown ASS style {event.style!r}")
        if event.end > event.start:
            self.events.append(event)

    def render(self) -> str:
        head = [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {self.width}",
            f"PlayResY: {self.height}",
            "WrapStyle: 2",  # we wrap ourselves; libass must not re-wrap
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            *(s.line() for s in self.styles.values()),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        events = sorted(self.events, key=lambda e: (e.start, e.layer))
        return "\n".join([*head, *(e.line() for e in events)]) + "\n"

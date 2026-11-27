"""Build the single ASS overlay document for a render.

Everything textual lives in one .ass file timed against the output timeline: captions (with
active-word highlight), on-screen text, the animated price card, end-card text and the
free-tier watermark. The logo is a bitmap and is composited by ffmpeg; its geometry is
computed here too so layout decisions stay in one place.

All coordinates use the template canvas (PlayRes = 1080×1920). libass scales to the actual
video size, so the 540×960 preview and the full render have identical layout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from adreel.render.ass import (
    AssDocument,
    AssEvent,
    AssStyle,
    font_runs,
    rounded_rect,
    style_color,
    tag_alpha,
    tag_color,
)
from adreel.render.captions import (
    CaptionEvent,
    CaptionLayout,
    layout,
    map_to_output,
    text_width_px,
    tokenize,
    units_that_fit,
    wrap_lines,
)
from adreel.render.timeline import Timeline
from adreel.schemas.brand_kit import BrandKit, contrast_ratio
from adreel.schemas.edit_plan import (
    EditPlan,
    OnScreenText,
    SegmentRole,
    TextPosition,
    TextStyle,
)
from adreel.schemas.template import CaptionMode, TemplateSpec

SIDE_MARGIN = 80
LINE_HEIGHT = 1.08  # rendered line pitch ≈ font size × this (libass scales by ascent+descent)
FALLBACK_HIGHLIGHT = "#FFD400"


class Palette:
    """Resolves template colour tokens against a brand kit."""

    def __init__(self, brand: BrandKit) -> None:
        self._map = {
            "primary": brand.primary_color,
            "on_primary": brand.on_primary,
            "accent": brand.accent,
            "white": "#FFFFFF",
            "black": "#000000",
        }

    def __call__(self, ref: str) -> str:
        return ref.upper() if ref.startswith("#") else self._map[ref]


def readable_highlight(color: str, against: str = "#000000", min_ratio: float = 3.0) -> str:
    """Highlighted caption words sit on a dark outline; dark brand colours become yellow."""
    return color if contrast_ratio(color, against) >= min_ratio else FALLBACK_HIGHLIGHT


def format_number(v: float) -> str:
    return f"{int(v):,}" if float(v).is_integer() else f"{v:,.2f}"


def fit_size(lines: list[str], size: float, max_width_px: float) -> float:
    widest = max((text_width_px(ln, size) for ln in lines), default=0.0)
    return size if widest <= max_width_px or widest == 0 else size * max_width_px / widest


def _ml(lines: list[str] | tuple[str, ...]) -> str:
    return "\\N".join(font_runs(ln) for ln in lines)


@dataclass(frozen=True)
class LogoPlacement:
    """Logo position in canvas pixels; the compositor scales it to the output size."""

    box_w: int  # logo is scaled to fit inside box_w × box_h, aspect preserved
    box_h: int
    anchor: Literal["top_left", "top_right", "bottom_left", "bottom_right", "center"]
    margin: int  # corner anchors: distance from the edges
    center_y: int  # "center" anchor: vertical centre of the logo
    start: float
    end: float
    opacity: float

    def overlay_xy(self, k: float) -> tuple[str, str]:
        """ffmpeg overlay x/y expressions at output scale ``k`` (output px / canvas px)."""
        m, cy = round(self.margin * k), round(self.center_y * k)
        if self.anchor == "center":
            return "(W-w)/2", f"{cy}-h/2"
        x = f"{m}" if self.anchor.endswith("left") else f"W-w-{m}"
        y = f"{m}" if self.anchor.startswith("top") else f"H-h-{m}"
        return x, y


@dataclass
class OverlayResult:
    doc: AssDocument
    captions: list[CaptionEvent]
    logos: list[LogoPlacement] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_overlays(
    plan: EditPlan,
    brand: BrandKit,
    tpl: TemplateSpec,
    timeline: Timeline,
    *,
    has_logo: bool,
    watermark_text: str | None = None,
) -> OverlayResult:
    W, H = tpl.canvas.width, tpl.canvas.height
    pal = Palette(brand)
    doc = AssDocument(W, H)
    res = OverlayResult(doc=doc, captions=[])
    main, total = timeline.main_duration, timeline.total_duration
    lang = plan.language

    _captions(plan, tpl, timeline, pal, res)
    _on_screen_text(plan, tpl, timeline, pal, res)
    _price_card(plan, tpl, timeline, pal, res)
    if timeline.end_card_frames:
        _end_card(plan, brand, tpl, timeline, pal, res, has_logo=has_logo, lang=lang)

    if has_logo and tpl.logo.enabled:
        lg = tpl.logo
        bw = round(W * lg.width_frac)
        res.logos.append(
            LogoPlacement(bw, bw, lg.corner.value, lg.margin_px, 0, 0.0, main, lg.opacity)
        )
    if has_logo and timeline.end_card_frames and tpl.end_card.enabled:
        ec = tpl.end_card
        bw = round(W * ec.logo_width_frac)
        cy = round(H * ec.logo_center_y)
        res.logos.append(LogoPlacement(bw, bw, "center", 0, cy, main, total, 1.0))

    if watermark_text:
        wm = tpl.watermark
        doc.add_style(
            AssStyle(
                "Watermark",
                wm.font_size,
                primary=style_color("#FFFFFF", wm.opacity),
                outline=style_color("#000000", wm.opacity * 0.6),
                outline_w=2,
                alignment=2,
                bold=True,
            )
        )
        doc.add(
            AssEvent(
                0.0,
                total,
                "Watermark",
                f"{{\\an2\\pos({W // 2},{round(H * wm.y)})}}{font_runs(watermark_text)}",
                layer=50,
            )
        )
    return res


# --------------------------------------------------------------------------- captions


def _captions(
    plan: EditPlan, tpl: TemplateSpec, timeline: Timeline, pal: Palette, res: OverlayResult
) -> None:
    cs = tpl.captions
    if not cs.enabled or not plan.captions:
        return
    W, H = tpl.canvas.width, tpl.canvas.height
    avail = W - 2 * cs.side_margin_px
    cfg = CaptionLayout(
        max_units_per_line=min(cs.max_units_per_line, units_that_fit(avail, cs.font_size)),
        max_lines=cs.max_lines,
        min_duration_s=cs.min_duration_s,
        min_visible_s=cs.min_visible_s,
        gap_s=cs.gap_s,
        merge_gap_s=cs.merge_gap_s,
    )
    events = layout(
        map_to_output(plan.captions, timeline, cs.merge_gap_s), cfg, timeline.main_duration
    )
    res.captions = events
    text_hex = pal(cs.text_color)
    if cs.mode is CaptionMode.BOX:
        style = AssStyle(
            "Caption",
            cs.font_size,
            primary=style_color(text_hex),
            outline=style_color(pal(cs.box_color), cs.box_opacity),
            back=style_color("#000000", 0),
            border_style=3,
            outline_w=14,
            shadow=0,
            alignment=2,
            bold=cs.bold,
            margin_l=cs.side_margin_px,
            margin_r=cs.side_margin_px,
            margin_v=round(H * (1 - cs.y)),
        )
    else:
        style = AssStyle(
            "Caption",
            cs.font_size,
            primary=style_color(text_hex),
            outline=style_color(pal(cs.outline_color)),
            back=style_color("#000000", 0.5),
            border_style=1,
            outline_w=cs.outline_px,
            shadow=cs.shadow_px,
            alignment=2,
            bold=cs.bold,
            margin_l=cs.side_margin_px,
            margin_r=cs.side_margin_px,
            margin_v=round(H * (1 - cs.y)),
        )
    res.doc.add_style(style)
    hl = readable_highlight(pal(cs.highlight_color))

    for ev in events:
        size = fit_size(list(ev.lines), cs.font_size, avail)
        size_tag = f"\\fs{size:.0f}" if size < cs.font_size else ""
        line_tokens = [tokenize(ln) for ln in ev.lines]
        flat = [t for toks in line_tokens for t in toks]
        if not (cs.highlight_active_word and ev.words and len(ev.words) == len(flat)):
            res.doc.add(
                AssEvent(
                    ev.start,
                    ev.end,
                    "Caption",
                    f"{{\\fad(60,40){size_tag}}}{_ml(ev.lines)}",
                    layer=20,
                )
            )
            continue
        # One sub-event per active word; contiguous so the line never flickers.
        bounds = [ev.start] + [w.start for w in ev.words[1:]] + [ev.end]
        for i in range(len(ev.words)):
            s, e = max(bounds[i], ev.start), bounds[i + 1]
            if e - s < 0.01:
                continue
            parts, k = [], 0
            for toks in line_tokens:
                row = []
                for tok in toks:
                    run = font_runs(tok)
                    if k == i:
                        run = f"{{\\c{tag_color(hl)}}}{run}{{\\c{tag_color(text_hex)}}}"
                    row.append(run)
                    k += 1
                parts.append(" ".join(row))
            fade = "\\fad(60,0)" if i == 0 else ("\\fad(0,40)" if i == len(ev.words) - 1 else "")
            res.doc.add(
                AssEvent(s, e, "Caption", f"{{{fade}{size_tag}}}" + "\\N".join(parts), layer=20)
            )


# --------------------------------------------------------------------------- on-screen text


def _text_style_name(style: TextStyle) -> str:
    return f"Text_{style.value}"


def _on_screen_text(
    plan: EditPlan, tpl: TemplateSpec, timeline: Timeline, pal: Palette, res: OverlayResult
) -> None:
    W, H = tpl.canvas.width, tpl.canvas.height
    items = list(plan.on_screen_text)
    # Template slot fallback: show hook_text on the first segment unless the plan placed one.
    if plan.slots.hook_text and not any(t.style is TextStyle.HOOK for t in items):
        first = plan.segments[0]
        items.insert(
            0,
            OnScreenText(
                text=plan.slots.hook_text,
                segment_id=first.id,
                duration=min(3.0, first.duration),
                style=TextStyle.HOOK,
                position=TextPosition.TOP,
            ),
        )
    if not items:
        return
    for st, spec in tpl.text.styles.items():
        res.doc.add_style(
            AssStyle(
                _text_style_name(st),
                spec.font_size,
                bold=spec.bold,
                primary=style_color(pal(spec.color)),
                outline=style_color(pal(spec.box_color if spec.box else spec.outline_color)),
                back=style_color("#000000", 0.45),
                border_style=3 if spec.box else 1,
                outline_w=16 if spec.box else spec.outline_px,
                shadow=spec.shadow_px,
                alignment=5,
            )
        )
    ys = {
        TextPosition.TOP: tpl.text.top_y,
        TextPosition.CENTER: tpl.text.center_y,
        TextPosition.BOTTOM: tpl.text.bottom_y,
    }
    avail = W - 2 * SIDE_MARGIN
    for t in items:
        s0, s1 = timeline.window(t.segment_id)
        start = s0 + t.start_offset
        end = min(start + t.duration if t.duration else s1, timeline.main_duration)
        if end - start < 0.2:
            res.warnings.append(f"on-screen text {t.text!r} has no time left after cuts; skipped")
            continue
        spec = tpl.text.styles[t.style]
        max_units = min(tpl.text.max_units_per_line, units_that_fit(avail, spec.font_size))
        lines = wrap_lines(t.text, max_units, 3)
        size = fit_size(lines, spec.font_size, avail)
        tags = f"\\an5\\pos({W // 2},{round(H * ys[t.position])})"
        if size < spec.font_size:
            tags += f"\\fs{size:.0f}"
        if spec.pop_in:
            tags += "\\fscx70\\fscy70\\t(0,140,\\fscx110\\fscy110)\\t(140,240,\\fscx100\\fscy100)"
        tags += "\\fad(100,160)"
        res.doc.add(
            AssEvent(start, end, _text_style_name(t.style), f"{{{tags}}}{_ml(lines)}", layer=30)
        )


# --------------------------------------------------------------------------- price card


def _price_card(
    plan: EditPlan, tpl: TemplateSpec, timeline: Timeline, pal: Palette, res: OverlayResult
) -> None:
    pc, spec = plan.price_card, tpl.price_card
    if not (pc.enabled and spec.enabled):
        return
    slots = plan.slots
    if slots.price is None:
        res.warnings.append("price card enabled but slots.price is empty; skipped")
        return
    W, H = tpl.canvas.width, tpl.canvas.height
    seg_id = pc.segment_id
    if seg_id is None:
        products = [s for s in plan.segments if s.role is SegmentRole.PRODUCT]
        seg_id = (products[-1] if products else plan.segments[-1]).id
    s0, _ = timeline.window(seg_id)
    main = timeline.main_duration
    start = min(s0 + pc.start_offset, max(0.0, main - pc.duration))
    end = min(start + pc.duration, main)
    if end - start < 0.8:
        res.warnings.append("price card has under 0.8 s on screen; skipped")
        return

    lang = plan.language
    currency = spec.currency.get(lang)
    fg = pal(spec.text_color)
    pad = 44
    card_w = round(W * spec.width_frac)
    inner = card_w - 2 * pad
    rows: list[tuple[str, float, str]] = []  # (text, size, extra tags)
    if slots.product_name:
        rows.append((slots.product_name, spec.name_size, ""))
    rows.append((f"{format_number(slots.price)} {currency}", spec.price_size, ""))
    if slots.old_price is not None:
        rows.append(
            (
                f"{format_number(slots.old_price)} {currency}",
                spec.old_price_size,
                f"\\s1\\alpha{tag_alpha(0.75)}",
            )
        )
    if slots.price_note:
        rows.append((slots.price_note, spec.note_size, ""))
    fitted = [(txt, fit_size([txt], size, inner), tags) for txt, size, tags in rows]
    gap = 10
    card_h = round(2 * pad + sum(sz * LINE_HEIGHT for _, sz, _ in fitted) + gap * (len(fitted) - 1))
    x0 = (W - card_w) // 2
    y0 = round(H * spec.y - card_h / 2)

    def anim(x: int, y: int) -> str:
        if spec.slide_in:
            return f"\\move({x - W},{y},{x},{y},0,320)\\fad(0,200)"
        return f"\\pos({x},{y})\\fad(200,200)"

    res.doc.add_style(AssStyle("Card", 40, outline_w=0, shadow=0, alignment=7))
    res.doc.add_style(
        AssStyle("CardText", 60, primary=style_color(fg), outline_w=0, shadow=0, alignment=8)
    )
    shape = rounded_rect(card_w, card_h, spec.radius_px)
    res.doc.add(
        AssEvent(
            start,
            end,
            "Card",
            f"{{\\an7{anim(x0 + 10, y0 + 16)}\\1c{tag_color('#000000')}\\1a{tag_alpha(0.35)}"
            f"\\bord0\\shad0\\p1}}{shape}{{\\p0}}",
            layer=40,
        )
    )
    res.doc.add(
        AssEvent(
            start,
            end,
            "Card",
            f"{{\\an7{anim(x0, y0)}\\1c{tag_color(pal(spec.background))}\\bord0\\shad0\\p1}}"
            f"{shape}{{\\p0}}",
            layer=41,
        )
    )
    y: float = y0 + pad
    for txt, size, extra in fitted:
        res.doc.add(
            AssEvent(
                start,
                end,
                "CardText",
                f"{{\\an8{anim(W // 2, round(y))}\\fs{size:.0f}{extra}}}{font_runs(txt)}",
                layer=42,
            )
        )
        y += size * LINE_HEIGHT + gap


# --------------------------------------------------------------------------- end card


def _end_card(
    plan: EditPlan,
    brand: BrandKit,
    tpl: TemplateSpec,
    timeline: Timeline,
    pal: Palette,
    res: OverlayResult,
    *,
    has_logo: bool,
    lang: str,
) -> None:
    ec = tpl.end_card
    W, H = tpl.canvas.width, tpl.canvas.height
    t0, t1 = timeline.main_duration, timeline.total_duration
    fg, bg = pal(ec.text_color), pal(ec.background)
    avail = W - 2 * SIDE_MARGIN
    res.doc.add_style(
        AssStyle("End", ec.title_size, primary=style_color(fg), outline_w=0, shadow=0, alignment=8)
    )
    res.doc.add_style(AssStyle("EndShape", 40, outline_w=0, shadow=0, alignment=7))

    y = H * (0.47 if has_logo else 0.3)
    step = 0

    def appear() -> str:
        nonlocal step
        d = 120 + 110 * step
        step += 1
        return f"\\alpha&HFF&\\t({d},{d + 260},\\alpha&H00&)"

    title_lines = wrap_lines(brand.business_name, units_that_fit(avail, ec.title_size), 2)
    size = fit_size(title_lines, ec.title_size, avail)
    res.doc.add(
        AssEvent(
            t0,
            t1,
            "End",
            f"{{\\an8\\pos({W // 2},{round(y)})\\fs{size:.0f}{appear()}}}{_ml(title_lines)}",
            layer=60,
        )
    )
    y += size * LINE_HEIGHT * len(title_lines) + 48

    cta = plan.slots.cta_text or ec.default_cta.get(lang)
    cta_size = fit_size([cta], ec.cta_size, avail - 80)
    pill_w = min(avail, text_width_px(cta, cta_size) + 110)
    pill_h = cta_size * LINE_HEIGHT + 44
    px = round((W - pill_w) / 2)
    a = appear()
    res.doc.add(
        AssEvent(
            t0,
            t1,
            "EndShape",
            f"{{\\an7\\pos({px},{round(y)})\\1c{tag_color(fg)}\\bord0\\shad0{a}\\p1}}"
            f"{rounded_rect(pill_w, pill_h, pill_h / 2)}{{\\p0}}",
            layer=61,
        )
    )
    res.doc.add(
        AssEvent(
            t0,
            t1,
            "End",
            f"{{\\an8\\pos({W // 2},{round(y + 22)})\\fs{cta_size:.0f}\\c{tag_color(bg)}{a}}}"
            f"{font_runs(cta)}",
            layer=62,
        )
    )
    y += pill_h + 56

    info = []
    if brand.phone_display:
        info.append(f"{ec.phone_label.get(lang)}: {brand.phone_display}")
    if brand.telegram_handle:
        info.append(f"{ec.telegram_label.get(lang)}: {brand.telegram_handle}")
    for line in info:
        sz = fit_size([line], ec.info_size, avail)
        res.doc.add(
            AssEvent(
                t0,
                t1,
                "End",
                f"{{\\an8\\pos({W // 2},{round(y)})\\fs{sz:.0f}{appear()}}}{font_runs(line)}",
                layer=60,
            )
        )
        y += sz * LINE_HEIGHT + 22

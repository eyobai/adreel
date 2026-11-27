"""Prompt construction for the planner LLM. Pure functions: easy to test and to tune."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from adreel.ai.base import Transcript
from adreel.autoedit.schema import BusinessContext
from adreel.autoedit.transcript_lines import TranscriptLine
from adreel.schemas.brand_kit import BrandKit
from adreel.schemas.template import TemplateSpec

# Measured Amharic TTS pace is ~2.2–2.6 words/s; budget conservatively so VO fits.
VO_WORDS_PER_SECOND = 2.1


@dataclass(frozen=True)
class ClipAnalysis:
    clip_id: str
    duration: float
    width: int
    height: int
    proxy: Path
    silences: list[tuple[float, float]] = field(default_factory=list)
    scenes: list[float] = field(default_factory=list)
    transcript: Transcript | None = None

    @property
    def orientation(self) -> str:
        r = self.width / self.height
        return "portrait" if r < 0.8 else ("landscape" if r > 1.25 else "square")

    def describe(self) -> str:
        speech = "speech" if self.transcript and self.transcript.spoken else "no speech"
        size = f"{self.orientation} {self.width}x{self.height}"
        return f"Clip {self.clip_id}: {self.duration:.1f} s, {size}, {speech}"


SYSTEM = """\
You are a senior short-form video editor making TikTok/Instagram Reels for small businesses \
in Ethiopia (cafés, boutiques, salons, gyms, shops). You watch the owner's raw phone clips, \
read the transcript, and return an edit decision as JSON that exactly follows the schema.

Rules:
- Use only the clip ids given. All times are seconds INSIDE the source clip, with \
0 <= start < end <= clip duration. Segment ids must be unique (s1, s2, ...).
- Segments play in the order you list them. Pick the strongest, steadiest, best-lit moments; \
skip shaky, blurry, dark or repeated footage and false starts.
- Speech: start and end segments on word boundaries using the transcript times; never cut \
mid-word. Silences and filler words inside speaker segments are removed automatically \
afterwards, so choose whole thoughts.
- Respect the template's structure and the total length range (it INCLUDES the end card).
- Text you write (on_screen_text, slots, voiceover_script) is in {language_name}, \
{script_rule} Short and punchy: on-screen text at most 4 words.
- Never invent prices, discounts, phone numbers or claims. Use the business context as given.
- caption_fixes: only correct clear speech-recognition mistakes in transcript lines \
(spelling, wrong word). Keep the meaning and the word count when possible. Do not \
translate or rewrite style. Omit lines that are fine.
- reframe_mode: "auto" unless you have a reason; use focus_x to point at the subject when \
it is off-centre in a landscape shot.
"""


def language_rules(lang: str) -> tuple[str, str]:
    if lang == "am":
        return "Amharic", "written in Ethiopic script (numbers may use digits, e.g. 1,200 ብር)."
    return "English", "plain and simple."


def build_system(language: str) -> str:
    name, rule = language_rules(language)
    return SYSTEM.format(language_name=name, script_rule=rule)


def build_prompt(
    *,
    template: TemplateSpec,
    brand: BrandKit,
    context: BusinessContext,
    clips: Sequence[ClipAnalysis],
    lines: Sequence[TranscriptLine],
    want_voiceover: bool,
    language: str,
) -> str:
    end_card = template.end_card.duration_s if template.end_card.enabled else 0.0
    lo, hi = template.duration.min_s - end_card, template.duration.max_s - end_card
    out: list[str] = []
    out.append("## Business")
    out.append(f"- Name: {brand.business_name} ({brand.business_type.value})")
    for label, value in [
        ("Product", context.product_name),
        ("Price (ETB)", context.price),
        ("Old price (ETB)", context.old_price),
        ("Highlight", context.highlight),
        ("Target customer", context.target_customer),
        ("Call to action", context.cta),
        ("Notes", context.notes),
    ]:
        if value not in (None, ""):
            out.append(f"- {label}: {value}")

    out.append(f"\n## Template: {template.id} ({template.name.en})")
    out.append(template.description)
    out.append(
        f"- Segments must total {lo:.0f}–{hi:.0f} s "
        f"(plus a {end_card:.1f} s end card added automatically)."
    )
    for st in template.structure:
        rep = ", may repeat" if st.repeat else ""
        req = "required" if st.required else "optional"
        out.append(f"- role {st.role.value}: {st.min_s:g}–{st.max_s:g} s each, {req}{rep}")
    if template.required_slots:
        out.append(f"- Required slots: {', '.join(template.required_slots)}")
    if template.price_card.enabled and context.price is not None:
        out.append(
            "- A price card is shown over price_card_segment_id: choose a steady product shot."
        )
    if template.ai_guidance:
        out.append(f"- Guidance: {template.ai_guidance}")

    out.append("\n## Clips (videos attached in this order)")
    for c in clips:
        out.append(f"- {c.describe()}")
        if c.scenes:
            out.append(f"  shot changes at: {', '.join(f'{s:.1f}' for s in c.scenes[:30])}")
        if c.silences:
            sil = ", ".join(f"{s:.1f}-{e:.1f}" for s, e in c.silences[:40])
            out.append(f"  silent: {sil}")

    if lines:
        out.append("\n## Transcript lines ([L<id>] clip start-end: text)")
        out.extend(
            f"[L{ln.id}] {ln.clip_id} {ln.start:.2f}-{ln.end:.2f}: {ln.text}" for ln in lines
        )
    else:
        out.append("\n## Transcript: no speech in these clips.")

    out.append("\n## Voiceover")
    if want_voiceover:
        words = int(lo * VO_WORDS_PER_SECOND)
        out.append(
            f"Write voiceover_script: natural spoken {language_rules(language)[0]}, "
            f"at most {words} words, "
            "mentioning the product, the price as given and the call to action. It plays over the "
            "video and the original sound is lowered."
        )
    else:
        out.append("No voiceover: leave voiceover_script null.")
    out.append("\nReturn only the JSON object.")
    return "\n".join(out)


def format_problems(problems: Sequence[str]) -> str:
    body = "\n".join(f"- {p}" for p in problems[:20])
    return f"Your JSON had these problems:\n{body}\nReturn the complete corrected JSON object."

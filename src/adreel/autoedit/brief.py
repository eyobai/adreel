"""Read facts out of the business's free-text brief, deterministically.

The planner LLM writes the creative text, but money must never be invented: a price is
only taken when the business wrote it next to a currency word or "price"/"ዋጋ".
"""

from __future__ import annotations

import re

_NUM = r"(\d{1,3}(?:[,\s]\d{3})+|\d+)(?:\.(\d{1,2}))?"
_AFTER = re.compile(_NUM + r"\s*(?:ብር|birr|etb|br\b)", re.IGNORECASE)
_BEFORE = re.compile(r"(?:ዋጋ\w*|price|etb|birr)\s*[:=\-–]?\s*(?:ብር\s*)?" + _NUM, re.IGNORECASE)
_SENTENCE = re.compile(r"[።!?.\n፤]+")


def _to_float(whole: str, frac: str | None) -> float:
    value = float(re.sub(r"[,\s]", "", whole))
    return value + (float(f"0.{frac}") if frac else 0.0)


def extract_price(text: str | None) -> float | None:
    """'ፈቃት 500 ብር ነው' -> 500.0; 'ዋጋው 1,200' -> 1200.0; '3 ቀን ብቻ' -> None."""
    if not text:
        return None
    for pattern in (_AFTER, _BEFORE):
        m = pattern.search(text)
        if m:
            value = _to_float(m.group(1), m.group(2))
            if 0 < value <= 10_000_000:
                return value
    return None


def first_phrase(text: str | None, max_chars: int = 40) -> str | None:
    """First sentence of the brief, cut at a word boundary (heuristic hook text)."""
    if not text:
        return None
    first = next((s.strip() for s in _SENTENCE.split(text) if s.strip()), "")
    if len(first) <= max_chars:
        return first or None
    cut = first[:max_chars].rsplit(" ", 1)[0]
    return cut or first[:max_chars]


AUTO_TEMPLATE = "auto"  # let the system pick the template from the footage

LOOK_BY_BUSINESS = {
    "cafe": "warm",
    "restaurant": "warm",
    "boutique": "vivid",
    "salon": "vivid",
    "shop": "vivid",
    "gym": "cool",
    "other": "natural",
}

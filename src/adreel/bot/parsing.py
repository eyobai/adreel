"""Parse what people type into the bot. Pure functions, unit-tested."""

from __future__ import annotations

import re

from adreel.schemas.brand_kit import normalize_et_phone

_PRICE_NOISE = re.compile(r"(ብር|birr|etb|br\.?|\s|,|')", re.IGNORECASE)
_PRICE = re.compile(r"^\d{1,8}(\.\d{1,2})?$")
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")
_HANDLE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")
_FIX_LINE = re.compile(r"^\s*(\d{1,3})\s*[:.)\-–—]\s*(.+?)\s*$")
_ETHIOPIC_DIGITS = {chr(0x1369 + i): str(i + 1) for i in range(9)}  # ፩..፱ (no zero in Ge'ez)


def parse_price(text: str) -> float | None:
    s = _PRICE_NOISE.sub("", text.strip())
    if not _PRICE.match(s):
        return None
    value = float(s)
    return value if 0 < value <= 10_000_000 else None


def parse_hex_color(text: str) -> str | None:
    m = _HEX.match(text.strip())
    return f"#{m.group(1).upper()}" if m else None


def parse_phone(text: str) -> str | None:
    try:
        return normalize_et_phone(text)
    except ValueError:
        return None


def parse_handle(text: str) -> str | None:
    s = text.strip().removeprefix("https://t.me/").removeprefix("t.me/")
    m = _HANDLE.match(s)
    return f"@{m.group(1)}" if m else None


def parse_caption_fixes(text: str) -> list[tuple[int, str]]:
    """``"3: new text"`` lines -> [(3, "new text")]. Raises ValueError on any bad line."""
    fixes: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = "".join(_ETHIOPIC_DIGITS.get(ch, ch) for ch in raw)
        if not line.strip():
            continue
        m = _FIX_LINE.match(line)
        if not m:
            raise ValueError(raw)
        fixes.append((int(m.group(1)), m.group(2)))
    if not fixes:
        raise ValueError(text)
    return fixes


def clean_text(text: str | None, max_len: int) -> str | None:
    s = " ".join((text or "").split())
    return s if 0 < len(s) <= max_len else None

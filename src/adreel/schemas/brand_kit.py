"""Brand kit: what a business sets up once in onboarding and every video reuses."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]


class BusinessType(StrEnum):
    CAFE = "cafe"
    RESTAURANT = "restaurant"
    BOUTIQUE = "boutique"
    SALON = "salon"
    GYM = "gym"
    SHOP = "shop"
    OTHER = "other"


_PHONE_RE = re.compile(r"^(?:\+?251|0)?([79]\d{8})$")
_HANDLE_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")


def normalize_et_phone(raw: str) -> str:
    """Normalise an Ethiopian mobile number to E.164 (+2519XXXXXXXX / +2517XXXXXXXX)."""
    digits = re.sub(r"[\s\-().]", "", raw)
    m = _PHONE_RE.match(digits)
    if not m:
        raise ValueError(f"not an Ethiopian mobile number: {raw!r}")
    return f"+251{m.group(1)}"


def display_et_phone(e164: str) -> str:
    """+251911234567 -> 0911 234 567 (how people write it locally)."""
    local = "0" + e164.removeprefix("+251")
    return f"{local[:4]} {local[4:7]} {local[7:]}"


def relative_luminance(hex_color: str) -> float:
    def ch(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(a: str, b: str) -> float:
    la, lb = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class BrandKit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    business_name: str = Field(min_length=1, max_length=60)
    business_type: BusinessType = BusinessType.OTHER
    logo_path: Path | None = None
    primary_color: HexColor = "#E4572E"
    secondary_color: HexColor | None = None
    phone: str | None = None
    telegram_handle: str | None = None
    language: Literal["am", "en"] = "am"

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        return normalize_et_phone(v) if v else None

    @field_validator("telegram_handle")
    @classmethod
    def _handle(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = v.removeprefix("https://t.me/").removeprefix("t.me/")
        m = _HANDLE_RE.match(v)
        if not m:
            raise ValueError(f"invalid Telegram handle: {v!r}")
        return "@" + m.group(1)

    @field_validator("primary_color", "secondary_color")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v

    @property
    def on_primary(self) -> str:
        """Text colour readable on the primary colour (white unless the brand is light)."""
        white, ink = "#FFFFFF", "#111111"
        return white if contrast_ratio(self.primary_color, white) >= 3.0 else ink

    @property
    def accent(self) -> str:
        return self.secondary_color or self.on_primary

    @property
    def phone_display(self) -> str | None:
        return display_et_phone(self.phone) if self.phone else None

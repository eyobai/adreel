"""Per-run API cost accounting.

Every provider call records billable units (audio seconds, tokens, characters). Prices
come from config, because vendor prices and plan tiers change. Phase 3 stores these
entries per job in Postgres; Phase 4's /stats reports average cost per video.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field


class Prices(BaseModel):
    """USD unit prices. Defaults are list prices at the time of writing: check them
    against your own plans and override with ADREEL_PRICE_* env vars."""

    stt_elevenlabs_per_hour: float = Field(0.40, ge=0)
    llm_gemini_input_per_mtok: float = Field(0.30, ge=0)  # text / image / video tokens
    llm_gemini_audio_input_per_mtok: float = Field(1.00, ge=0)
    llm_gemini_output_per_mtok: float = Field(2.50, ge=0)  # includes thinking tokens
    tts_azure_per_mchar: float = Field(16.0, ge=0)
    tts_elevenlabs_per_kchar: float = Field(0.18, ge=0)
    usd_to_etb: float = Field(155.0, gt=0, description="For reporting only")


@dataclass(frozen=True)
class CostEntry:
    step: str  # transcribe | plan | tts
    provider: str
    model: str
    units: Mapping[str, float]
    usd: float
    detail: str = ""


@dataclass
class CostLog:
    prices: Prices
    entries: list[CostEntry] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _add(self, entry: CostEntry) -> CostEntry:
        with self._lock:
            self.entries.append(entry)
        return entry

    def transcription(
        self, provider: str, model: str, audio_seconds: float, detail: str = ""
    ) -> CostEntry:
        usd = (
            0.0
            if provider.startswith("fake")
            else audio_seconds / 3600 * self.prices.stt_elevenlabs_per_hour
        )
        return self._add(
            CostEntry("transcribe", provider, model, {"audio_seconds": audio_seconds}, usd, detail)
        )

    def llm(
        self,
        provider: str,
        model: str,
        *,
        input_tokens: int,
        input_audio_tokens: int,
        output_tokens: int,
        detail: str = "",
    ) -> CostEntry:
        p = self.prices
        non_audio = max(0, input_tokens - input_audio_tokens)
        usd = (
            non_audio * p.llm_gemini_input_per_mtok
            + input_audio_tokens * p.llm_gemini_audio_input_per_mtok
            + output_tokens * p.llm_gemini_output_per_mtok
        ) / 1_000_000
        units = {
            "input_tokens": input_tokens,
            "input_audio_tokens": input_audio_tokens,
            "output_tokens": output_tokens,
        }
        return self._add(CostEntry("plan", provider, model, units, usd, detail))

    def tts(self, provider: str, voice: str, characters: int, detail: str = "") -> CostEntry:
        p = self.prices
        usd = (
            characters / 1000 * p.tts_elevenlabs_per_kchar
            if provider == "elevenlabs"
            else characters / 1_000_000 * p.tts_azure_per_mchar
        )
        if provider.startswith("fake"):
            usd = 0.0
        return self._add(CostEntry("tts", provider, voice, {"characters": characters}, usd, detail))

    # ------------------------------------------------------------------ reporting

    @property
    def total_usd(self) -> float:
        return sum(e.usd for e in self.entries)

    def totals(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in self.entries:
            for k, v in e.units.items():
                out[k] = out.get(k, 0) + v
        return out

    def summary(self) -> dict[str, object]:
        by_step: dict[str, float] = {}
        for e in self.entries:
            by_step[e.step] = by_step.get(e.step, 0.0) + e.usd
        return {
            "total_usd": round(self.total_usd, 5),
            "total_etb": round(self.total_usd * self.prices.usd_to_etb, 3),
            "by_step_usd": {k: round(v, 5) for k, v in by_step.items()},
            "units": self.totals(),
            "entries": [asdict(e) | {"usd": round(e.usd, 6)} for e in self.entries],
            "prices": self.prices.model_dump(),
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.summary(), indent=2, ensure_ascii=False), encoding="utf-8")

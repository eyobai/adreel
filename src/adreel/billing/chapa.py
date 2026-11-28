"""Chapa hosted checkout: initialize, verify, webhook signatures.

We never trust a webhook or a redirect on its own: they only tell us *which* tx_ref to look
at. Credits are granted only after ``GET /v1/transaction/verify/{tx_ref}`` says the payment
succeeded for exactly the amount, currency and mode (test/live) we asked for.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

import httpx

from adreel.log import get_logger

log = get_logger(__name__)


class ChapaError(Exception):
    """Transient or unexpected API failure (retry later)."""


class Verdict(StrEnum):
    PAID = "paid"
    PENDING = "pending"  # not finished yet / unknown: check again later
    FAILED = "failed"  # Chapa says it failed or was cancelled
    MISMATCH = "mismatch"  # succeeded, but not what we asked for: never credit


@dataclass(frozen=True)
class Judgement:
    verdict: Verdict
    reason: str = ""
    provider_ref: str | None = None


def is_test_key(secret_key: str) -> bool:
    return secret_key.startswith("CHASECK_TEST")


def to_cents(amount: Any) -> int | None:
    try:
        return int((Decimal(str(amount)) * 100).to_integral_value())
    except (InvalidOperation, ValueError, TypeError):
        return None


def judge_verification(
    payload: dict[str, Any], *, tx_ref: str, amount_cents: int, currency: str, test_mode: bool
) -> Judgement:
    """Decide what a verify response means for the payment we created."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        message = str(payload.get("message") or "") if isinstance(payload, dict) else ""
        if isinstance(payload, dict) and payload.get("mode_mismatch"):
            # Opened with a key of the other mode: asking again will never succeed.
            return Judgement(Verdict.MISMATCH, f"mode mismatch ({message[:100]})")
        return Judgement(Verdict.PENDING, f"not ready ({message[:100] or 'no data'})")
    status = str(data.get("status") or "").lower()
    ref = data.get("reference") or None
    if status in {"failed", "failure", "cancelled", "canceled", "reversed"}:
        return Judgement(Verdict.FAILED, f"status {status}", ref)
    if status != "success":
        return Judgement(Verdict.PENDING, f"status {status or 'unknown'}", ref)
    problems = []
    if data.get("tx_ref") != tx_ref:
        problems.append(f"tx_ref {data.get('tx_ref')!r}")
    if str(data.get("currency") or "").upper() != currency.upper():
        problems.append(f"currency {data.get('currency')!r}")
    if to_cents(data.get("amount")) != amount_cents:
        problems.append(f"amount {data.get('amount')!r}")
    mode = str(data.get("mode") or "").lower()
    if mode != ("test" if test_mode else "live"):
        problems.append(f"mode {data.get('mode')!r} does not match the key in use")
    if problems:
        return Judgement(Verdict.MISMATCH, "; ".join(problems), ref)
    return Judgement(Verdict.PAID, "", ref)


def valid_webhook_signature(body: bytes, headers: Any, secret: str) -> bool:
    """Chapa sends ``x-chapa-signature`` = HMAC-SHA256(body, secret) and/or
    ``chapa-signature`` = HMAC-SHA256(secret, secret). Either one matching is accepted."""
    if not secret:
        return False
    key = secret.encode()
    body_sig = hmac.new(key, body, hashlib.sha256).hexdigest()
    secret_sig = hmac.new(key, key, hashlib.sha256).hexdigest()
    got_body = (headers.get("x-chapa-signature") or "").strip().lower()
    got_secret = (headers.get("chapa-signature") or "").strip().lower()
    return (bool(got_body) and hmac.compare_digest(got_body, body_sig)) or (
        bool(got_secret) and hmac.compare_digest(got_secret, secret_sig)
    )


class ChapaClient:
    def __init__(
        self,
        secret_key: str,
        *,
        base_url: str = "https://api.chapa.co",
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.secret_key = secret_key
        self.test_mode = is_test_key(secret_key)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {secret_key}"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def initialize(
        self,
        *,
        tx_ref: str,
        amount_cents: int,
        currency: str,
        title: str,
        description: str,
        return_url: str | None = None,
        callback_url: str | None = None,
        first_name: str | None = None,
    ) -> str:
        """Create a hosted checkout and return its URL."""
        body: dict[str, Any] = {
            "amount": f"{amount_cents / 100:.2f}",
            "currency": currency,
            "tx_ref": tx_ref,
            # Chapa limits: title <= 16 chars; description letters/numbers/.-_ and spaces.
            "customization": {"title": title[:16], "description": description[:50]},
        }
        if return_url:
            body["return_url"] = return_url
        if callback_url:
            body["callback_url"] = callback_url
        if first_name:
            body["first_name"] = first_name[:50]
        try:
            resp = await self._client.post("/v1/transaction/initialize", json=body)
        except httpx.HTTPError as exc:
            raise ChapaError(f"initialize: {exc}") from exc
        payload = _json(resp)
        url = (payload.get("data") or {}).get("checkout_url") if isinstance(payload, dict) else None
        if resp.status_code >= 400 or not url:
            raise ChapaError(f"initialize {resp.status_code}: {_message(payload)}")
        return str(url)

    async def verify(self, tx_ref: str) -> dict[str, Any]:
        """Raw verify payload. A 4xx 'not found' means the user hasn't paid (yet)."""
        try:
            resp = await self._client.get(f"/v1/transaction/verify/{tx_ref}")
        except httpx.HTTPError as exc:
            raise ChapaError(f"verify: {exc}") from exc
        payload = _json(resp)
        if resp.status_code >= 500:
            raise ChapaError(f"verify {resp.status_code}: {_message(payload)}")
        if resp.status_code >= 400:
            msg = _message(payload)
            mismatch = resp.status_code in (401, 403) and "mode mismatch" in msg.lower()
            if resp.status_code in (401, 403) and not mismatch:
                raise ChapaError(f"verify {resp.status_code}: {msg}")  # bad key: our problem
            log.info("chapa_verify_not_ready", tx_ref=tx_ref, status=resp.status_code, message=msg)
            return {"status": "failed", "message": msg, "data": None, "mode_mismatch": mismatch}
        return payload if isinstance(payload, dict) else {}


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"message": resp.text[:200]}


def _message(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("message") or payload)[:300]
    return str(payload)[:300]

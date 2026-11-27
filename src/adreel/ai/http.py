"""HTTP with retries for provider REST APIs.

Retries on network errors, 429 and 5xx with exponential backoff and jitter, honouring
``Retry-After``. 4xx other than 408/429 fail fast (bad key, bad request: retrying won't help).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from adreel.ai.base import ProviderError
from adreel.log import get_logger

log = get_logger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    provider: str,
    attempts: int = 4,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    last: str = ""
    for attempt in range(1, attempts + 1):
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.TransportError as exc:  # DNS, connect, read timeout...
            last = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code < 400:
                return resp
            body = resp.text[:500]
            if resp.status_code not in RETRY_STATUS:
                raise ProviderError(provider, f"HTTP {resp.status_code}: {body}")
            last = f"HTTP {resp.status_code}: {body}"
            retry_after = resp.headers.get("retry-after", "")
            if retry_after.isdigit() and attempt < attempts:
                log.warning("http.retry", provider=provider, attempt=attempt, reason=last[:120])
                sleep(min(float(retry_after), 30.0))
                continue
        if attempt < attempts:
            delay = base_delay * 2 ** (attempt - 1) * (0.75 + random.random() / 2)
            log.warning("http.retry", provider=provider, attempt=attempt, reason=last[:120])
            sleep(delay)
    raise ProviderError(provider, f"failed after {attempts} attempts: {last}", retryable=True)

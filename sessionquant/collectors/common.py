"""Bounded retry and pacing for read-only collectors; no order retry utility."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import monotonic

import httpx


class CapabilityUnavailable(RuntimeError):
    pass


class DataQualityError(ValueError):
    pass


class RateLimiter:
    """Serialize starts, including retries. Share one instance for one provider."""
    def __init__(self, requests_per_second: float = 5.0):
        if not 0 < requests_per_second <= 20:
            raise ValueError("invalid request rate")
        self.interval = 1 / requests_per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            await asyncio.sleep(max(0.0, self._next - monotonic()))
            self._next = monotonic() + self.interval


def retry_after(value: str | None, fallback: float) -> float:
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError):
                return fallback
        # Large server delays should be surfaced, never silently shortened.
        if delay > 60:
            raise CapabilityUnavailable("Provider requested a retry after more than 60 seconds; retry later")
        return max(0, delay)
    return fallback


async def get_readonly(client: httpx.AsyncClient, url: str, *, limiter: RateLimiter,
                       attempts: int = 3, **kwargs) -> httpx.Response:
    """GET only. Authentication failures fail immediately without credential churn."""
    if not 1 <= attempts <= 5:
        raise ValueError("attempts must be 1..5")
    for attempt in range(attempts):
        await limiter.wait()
        try:
            response = await client.get(url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt + 1 == attempts:
                raise
            await asyncio.sleep(0.25 * 2**attempt)
            continue
        if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
            await asyncio.sleep(retry_after(response.headers.get("Retry-After"), 0.25 * 2**attempt))
            continue
        response.raise_for_status()
        return response
    raise AssertionError("unreachable")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

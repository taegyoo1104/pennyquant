"""Observe Toss current prices without constructing a trading engine.

Only the OAuth token and batch prices endpoints are used. A price observation is
not a two-sided quote, a verified live feed, or permission to trade.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import math
import os
from pathlib import Path
import re

import httpx

from .collectors.common import CapabilityUnavailable, DataQualityError, now_utc
from .collectors.toss_market import TossMarketClient
from .connection import REQUIRED_ENVIRONMENT, _price_summary
from .database.repository import KINDS, Repository
from .locking import exclusive_lock


SOURCE = {"synthetic": False, "label": "Toss observed prices, partial venues, not NBBO"}


def _configuration(symbols: list[str], interval_seconds: float, max_cycles: int | None) -> list[str]:
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 20:
        raise ValueError("Supply between 1 and 20 US ticker symbols")
    normalized = []
    for symbol in symbols:
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9.\-]{1,20}", symbol.strip().upper()):
            raise ValueError("Supply valid US ticker symbols, for example AAPL")
        normalized.append(symbol.strip().upper())
    if (isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds) or interval_seconds < 1):
        raise ValueError("The polling interval must be finite and at least one second")
    if max_cycles is not None and (isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1):
        raise ValueError("max_cycles must be a positive integer or None")
    return list(dict.fromkeys(normalized))


def _guard_database(repo: Repository) -> None:
    runtime_kind = repo.get_control("runtime_kind")
    if repo.load_state() is not None or runtime_kind not in (None, "toss_monitor"):
        raise ValueError("This database belongs to a paper or another runtime; use a separate Toss monitor database")
    # An unlabelled database with observations may be an older paper/demo DB.
    # Only an empty, unclaimed database can be assigned a new runtime identity.
    if runtime_kind is None and (repo.get_control("source") is not None or any(repo.all(kind) for kind in KINDS)):
        raise ValueError("This database already contains unclaimed data; use a separate Toss monitor database")


def _save_status(repo: Repository, status: dict) -> None:
    repo.set_control("monitor", status)
    repo.set_control("runtime", {
        "mode": "monitor", "provider": "toss", "strategy": None,
        "last_event": status["last_success"], "halt_reasons": [],
    })


def _error(code: str, message: str, *, stage: str = "price_read", http_status: int | None = None) -> dict:
    result = {"code": code, "stage": stage, "message": message}
    if http_status is not None:
        result["http_status"] = http_status
    return result


def _safe_error(exc: Exception, adapter: TossMarketClient) -> dict:
    # Inspect only a known request path, never serialize requests or exceptions.
    stage = "price_read" if getattr(adapter, "_token", "") else "authentication"
    if isinstance(exc, httpx.HTTPStatusError):
        stage = "authentication" if exc.request.url.path == "/oauth2/token" else "price_read"
        status = exc.response.status_code
        if status in (401, 403):
            message = "The provider rejected access; check credentials, permissions and registered-IP requirements"
        elif status == 429:
            message = "The provider rate limit was reached; retry later"
        else:
            message = "The provider returned an unsuccessful HTTP response; check provider status and API access"
        return _error("http_error", message, stage=stage, http_status=status)
    if isinstance(exc, httpx.RequestError):
        return _error("network_error", "The connection failed; check network and provider availability", stage=stage)
    if isinstance(exc, CapabilityUnavailable):
        return _error("capability_unavailable", "The API capability is unavailable; check local configuration and access", stage=stage)
    if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError)):
        return _error("invalid_response", "The provider response could not be validated", stage=stage)
    return _error("unexpected_error", "The price monitor stopped unexpectedly", stage=stage)


def _observations(payload, symbols: list[str], received_at: str) -> tuple[list[dict], list[str], list[str]]:
    if not isinstance(payload, list):
        raise DataQualityError("invalid price response")
    rows, missing, invalid = [], [], []
    for symbol in symbols:
        matches = [item for item in payload if isinstance(item, dict) and item.get("symbol") == symbol]
        if not matches:
            missing.append(symbol)
            continue
        try:
            summary = _price_summary(payload, symbol)
        except (ValueError, TypeError, KeyError, AttributeError):
            invalid.append(symbol)
            continue
        rows.append({
            "id": symbol, "symbol": symbol, "last_price": summary["last_price"],
            "currency": "USD", "timestamp": summary["market_timestamp"],
            "received_at": received_at, "source": "toss",
        })
    return rows, missing, invalid


async def monitor_toss(symbols: list[str], db_path: Path, *, interval_seconds: float = 3,
                       max_cycles: int | None = None, client: TossMarketClient | None = None,
                       on_ready: Callable[[], None] | None = None) -> int:
    """Persist observed prices until stopped; return 0 on clean completion, 2 on a safe failure.

    Injected adapters remain caller-owned. The lease covers validation, startup,
    all polls and cleanup. Cancellation propagates after recording stopped status.
    No failure triggers token reissue or another polling cycle.
    """
    symbols = _configuration(symbols, interval_seconds, max_cycles)
    db_path = Path(db_path)
    with exclusive_lock(Path(str(db_path) + ".engine.lock"), message="another engine is already using this database"):
        repo = Repository(db_path)
        adapter = client
        owns_client = client is None
        initialized = failed = False
        status = None
        try:
            _guard_database(repo)
            previous = repo.get_control("monitor", {})
            status = {
                "provider": "toss", "status": "starting", "symbols": symbols,
                "interval_seconds": interval_seconds, "last_success": previous.get("last_success"),
                "error": None, "mode": "monitor", "feed_verified": False, "trading_enabled": False,
                "missing_symbols": [], "invalid_symbols": [],
            }
            with repo.transaction():
                repo.set_control("runtime_kind", "toss_monitor")
                repo.set_control("source", SOURCE)
                _save_status(repo, status)
            initialized = True
            if on_ready is not None:
                on_ready()
            if adapter is None:
                if any(not os.getenv(name, "").strip() for name in REQUIRED_ENVIRONMENT):
                    status.update(status="error", error=_error("missing_credentials",
                        "Set TOSS_CLIENT_ID and TOSS_CLIENT_SECRET in the local environment", stage="configuration"))
                    failed = True
                    with repo.transaction():
                        _save_status(repo, status)
                    return 2
                try:
                    adapter = TossMarketClient()
                except Exception:
                    status.update(status="error", error=_error("invalid_configuration",
                        "The local API configuration could not be validated", stage="configuration"))
                    failed = True
                    with repo.transaction():
                        _save_status(repo, status)
                    return 2
            cycle = 0
            while max_cycles is None or cycle < max_cycles:
                try:
                    # The adapter authenticates lazily and reuses its cached token.
                    payload = await adapter.prices(symbols)
                    received_at = now_utc().isoformat()
                    rows, missing, invalid = _observations(payload, symbols, received_at)
                except Exception as exc:
                    status.update(status="error", error=_safe_error(exc, adapter))
                    failed = True
                    with repo.transaction():
                        _save_status(repo, status)
                    return 2
                next_status = {**status, "missing_symbols": missing, "invalid_symbols": invalid}
                if rows:
                    next_status["last_success"] = received_at
                if missing or invalid:
                    code = "invalid_response" if invalid else ("partial_data" if rows else "no_data")
                    next_status.update(status="error", error=_error(code,
                        "Prices are missing or invalid for requested symbols; previous observations were retained"))
                else:
                    next_status.update(status="connected", error=None)
                try:
                    with repo.transaction():
                        for row in rows:
                            repo.record("stocks", row)
                            repo.record("price_samples", {**row, "id": f"{row['symbol']}:{received_at}"})
                        _save_status(repo, next_status)
                except Exception:
                    # A rolled-back poll must not advance the success receipt.
                    status.update(status="error", error=_error("storage_error",
                        "Price observations could not be saved; the monitor stopped", stage="persistence"))
                    failed = True
                    try:
                        with repo.transaction():
                            _save_status(repo, status)
                    except Exception:
                        # Disk or connection failures may also prevent saving a
                        # diagnostic. Never expose the underlying exception.
                        pass
                    return 2
                status = next_status
                failed = bool(missing or invalid)
                if failed:
                    return 2
                cycle += 1
                if max_cycles is None or cycle < max_cycles:
                    await asyncio.sleep(interval_seconds)
            return 0
        finally:
            try:
                try:
                    if initialized and not failed:
                        status["status"] = "stopped"
                        with repo.transaction():
                            _save_status(repo, status)
                finally:
                    if owns_client and adapter is not None:
                        try:
                            await adapter.aclose()
                        except Exception:
                            if initialized:
                                status["cleanup_warning"] = "The temporary API client could not be closed cleanly"
                                try:
                                    with repo.transaction():
                                        _save_status(repo, status)
                                except Exception:
                                    pass
            finally:
                repo.close()

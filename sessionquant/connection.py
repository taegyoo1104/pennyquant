"""Read-only Toss authentication/current-price check with safe diagnostics.

This verifies one REST read, not real-time entitlement, consolidated quote
coverage, market eligibility, or readiness to run news-driven paper trading.
No credential, access token, raw response, account or order data is returned.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import os
import re

import httpx

from .collectors.common import CapabilityUnavailable, DataQualityError, now_utc
from .collectors.toss_market import TossMarketClient
from .models import utc


REQUIRED_ENVIRONMENT = ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET")


def _failure(result: dict, stage: str, code: str, message: str, *, http_status: int | None = None) -> dict:
    result["success"] = False
    if stage == "authentication":
        result["authentication"] = "failed"
    elif stage == "price_read":
        result["price_read"] = "failed"
    result["error"] = {"stage": stage, "code": code, "message": message}
    if http_status is not None:
        result["error"]["http_status"] = http_status
    return result


def _price_summary(payload, symbol: str) -> dict:
    """Allowlist the official PriceResponse fields; never return raw payloads."""
    if not isinstance(payload, list):
        raise DataQualityError("invalid price response")
    matching = [item for item in payload if isinstance(item, dict) and item.get("symbol") == symbol]
    if len(matching) != 1:
        raise DataQualityError("requested symbol missing or duplicated")
    item = matching[0]
    raw_price = item.get("lastPrice")
    if not isinstance(raw_price, str) or item.get("currency") != "USD":
        raise DataQualityError("USD decimal price required")
    try:
        price = Decimal(raw_price)
    except InvalidOperation:
        raise DataQualityError("invalid price") from None
    if not price.is_finite() or price <= 0:
        raise DataQualityError("positive finite price required")
    timestamp = item.get("timestamp")
    if timestamp is not None and not isinstance(timestamp, str):
        raise DataQualityError("invalid market timestamp")
    # The official API permits a null/absent timestamp, for example when no
    # trade time is available. Keep it unknown rather than inventing a time.
    market_time = utc(timestamp).isoformat() if timestamp is not None else None
    return {"last_price": str(price), "currency": "USD", "market_timestamp": market_time}


async def check_toss(symbol: str = "AAPL", *, client: TossMarketClient | None = None) -> dict:
    """Return a sanitized authentication + current-price endpoint report.

    An injected adapter remains owned by its caller. With no injected adapter,
    credentials are read only from environment variables and one temporary
    adapter is created and closed. All network calls are OAuth and price reads.
    """
    result = {
        "provider": "toss", "success": False, "symbol": None,
        "authentication": "not_checked", "price_read": "not_checked",
        "last_price": None, "currency": None, "market_timestamp": None,
        "checked_at": now_utc().isoformat(), "price_received_at": None,
        "live_orders_enabled": False, "realtime_feed_verified": False,
        "paper_trading_ready": False,
        "quote_scope": "US feed can cover partial venues; this check does not verify consolidated NBBO or depth",
    }
    if not isinstance(symbol, str):
        return _failure(result, "configuration", "invalid_symbol", "Supply one US ticker, for example AAPL")
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-]{1,20}", symbol):
        return _failure(result, "configuration", "invalid_symbol", "Supply one US ticker, for example AAPL")
    result["symbol"] = symbol
    owns_client = client is None
    if owns_client:
        missing = [name for name in REQUIRED_ENVIRONMENT if not os.getenv(name, "").strip()]
        if missing:
            result["missing_environment_variables"] = missing
            return _failure(result, "configuration", "missing_credentials",
                            "Set TOSS_CLIENT_ID and TOSS_CLIENT_SECRET in your local environment; do not paste values into chat")
    stage = "configuration"
    adapter = client
    try:
        if adapter is None:
            adapter = TossMarketClient()
        stage = "authentication"
        token = await adapter.access_token()
        if not isinstance(token, str) or not token.strip():
            return _failure(result, stage, "invalid_auth_response", "The provider did not return a usable authentication response")
        result["authentication"] = "ok"
        # Do not retain or display the token in the report.
        del token
        stage = "price_read"
        payload = await adapter.prices([symbol])
        result.update(_price_summary(payload, symbol))
        result["price_received_at"] = now_utc().isoformat()
        result["price_read"] = "ok"
        result["success"] = True
        result["message"] = "Authentication and price read succeeded. Real-time feed, quote freshness and paper-trading inputs still require separate verification"
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            message = "The provider rejected access. Check credentials, API permissions and any registered-IP requirements in the provider settings"
        elif status == 429:
            message = "The provider rate limit was reached; retry later"
        else:
            message = "The provider returned an unsuccessful HTTP response; check provider status and API access"
        _failure(result, stage, "http_error", message, http_status=status)
    except httpx.RequestError:
        _failure(result, stage, "network_error", "The connection failed; check your network and provider availability")
    except CapabilityUnavailable:
        _failure(result, stage, "capability_unavailable", "The requested API capability is unavailable; check local configuration and provider access")
    except (ValueError, TypeError, KeyError, AttributeError):
        _failure(result, stage, "invalid_response" if stage != "configuration" else "invalid_configuration",
                 "The response or local configuration could not be validated; no live-feed or trading readiness is established")
    except Exception:
        # Exceptions can contain response bodies, tokens or credential values.
        # A diagnostic must never echo them, including unexpected provider errors.
        _failure(result, stage, "unexpected_error", "The check stopped unexpectedly; no live-feed or trading readiness is established")
    finally:
        if owns_client and adapter is not None:
            try:
                await adapter.aclose()
            except Exception:
                result["cleanup_warning"] = "The temporary API client could not be closed cleanly"
    return result


async def run_check(symbol: str = "AAPL") -> int:
    """Print only the sanitized report; return a CLI-compatible exit code."""
    result = await check_toss(symbol)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["success"] else 2

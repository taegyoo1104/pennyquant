"""Toss read-only REST adapter, canonical OpenAPI v1.2.28 (2026-11-13).

No order-changing methods. OAuth token issuance is the sole POST operation.
Tokens are memory-only. Use one adapter per client: reissuance invalidates old tokens.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from time import monotonic

import httpx

from ..models import Bar, Quote, utc
from .common import CapabilityUnavailable, DataQualityError, RateLimiter, get_readonly, now_utc

BASE_URL = "https://openapi.tossinvest.com"


def symbols_parameter(symbols: list[str] | tuple[str, ...], maximum: int = 200) -> str:
    if not 1 <= len(symbols) <= maximum or any(not re.fullmatch(r"[A-Z0-9.\-]+", s) for s in symbols):
        raise ValueError("invalid symbols or batch size")
    return ",".join(dict.fromkeys(symbols))


def parse_orderbook(symbol: str, data: dict, received_at: datetime, *,
                    exchange: str = "UNKNOWN", tradable: bool = False, halted: bool = False) -> Quote:
    """Orderbook alone does not establish exchange, halt or order eligibility."""
    if data.get("currency") != "USD" or not data.get("timestamp"):
        raise DataQualityError("USD orderbook with a known event time required")
    asks, bids = data.get("asks", []), data.get("bids", [])
    if not asks or not bids:
        raise DataQualityError("two-sided orderbook required")
    ask = min(asks, key=lambda x: float(x["price"]))
    bid = max(bids, key=lambda x: float(x["price"]))
    return Quote(symbol, utc(data["timestamp"]), utc(received_at), float(bid["price"]),
                 float(ask["price"]), int(float(bid["volume"])), int(float(ask["volume"])),
                 tradable, halted, exchange)


def parse_candles(symbol: str, data: dict, received_at: datetime) -> list[Bar]:
    # Official 1m timestamp is the END of [timestamp-1minute,timestamp).
    result = []
    for item in data.get("candles", []):
        if item.get("currency") != "USD":
            raise DataQualityError("USD candles required")
        timestamp = utc(item["timestamp"])
        if timestamp > utc(received_at):
            continue  # never expose an unfinished or future bar
        result.append(Bar(symbol, timestamp, utc(received_at), float(item["openPrice"]),
                          float(item["highPrice"]), float(item["lowPrice"]),
                          float(item["closePrice"]), int(float(item["volume"]))))
    return sorted(result, key=lambda b: b.timestamp)


class TossMarketClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 account_seq: int | None = None, *, client: httpx.AsyncClient | None = None):
        self._client_id = client_id or os.getenv("TOSS_CLIENT_ID", "")
        self._client_secret = client_secret or os.getenv("TOSS_CLIENT_SECRET", "")
        seq = account_seq if account_seq is not None else os.getenv("TOSS_ACCOUNT_SEQ")
        self.account_seq = int(seq) if seq is not None and seq != "" else None
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._token = ""
        self._token_deadline = 0.0
        self._token_lock = asyncio.Lock()
        # Conservative global pacing also fits 1 TPS account endpoints.
        self.limiter = RateLimiter(1)

    async def access_token(self) -> str:
        async with self._token_lock:
            if self._token and monotonic() < self._token_deadline:
                return self._token
            if not self._client_id or not self._client_secret:
                raise CapabilityUnavailable("Set TOSS_CLIENT_ID and TOSS_CLIENT_SECRET outside source code")
            await self.limiter.wait()
            # Never retry token POST: successful reissue invalidates the previous token.
            response = await self.client.post(BASE_URL + "/oauth2/token", data={
                "grant_type": "client_credentials", "client_id": self._client_id,
                "client_secret": self._client_secret,
            })
            response.raise_for_status()
            data = response.json()
            self._token = data["access_token"]
            self._token_deadline = monotonic() + max(0, float(data["expires_in"]) - 120)
            return self._token

    async def _get(self, path: str, *, params: dict | None = None, account: bool = False):
        headers = {"Authorization": "Bearer " + await self.access_token()}
        if account:
            if self.account_seq is None:
                raise CapabilityUnavailable("Set TOSS_ACCOUNT_SEQ from the accounts response")
            headers["X-Tossinvest-Account"] = str(self.account_seq)
        response = await get_readonly(self.client, BASE_URL + path, limiter=self.limiter,
                                      headers=headers, params=params)
        data = response.json()
        if "result" not in data or "error" in data:
            raise DataQualityError("Toss response missing successful result envelope")
        return data["result"]

    async def prices(self, symbols: list[str]):
        return await self._get("/api/v1/prices", params={"symbols": symbols_parameter(symbols)})

    async def stocks(self, symbols: list[str]):
        return await self._get("/api/v1/stocks", params={"symbols": symbols_parameter(symbols)})

    async def warnings(self, symbol: str):
        symbols_parameter([symbol])
        return await self._get(f"/api/v1/stocks/{symbol}/warnings")

    async def orderbook(self, symbol: str):
        return await self._get("/api/v1/orderbook", params={"symbol": symbols_parameter([symbol])})

    async def quote(self, symbol: str, *, exchange: str = "UNKNOWN", tradable: bool = False,
                    halted: bool = False) -> Quote:
        data = await self.orderbook(symbol)
        return parse_orderbook(symbol, data, now_utc(), exchange=exchange, tradable=tradable, halted=halted)

    async def get_quote(self, symbol: str, *, exchange: str = "UNKNOWN", tradable: bool = False,
                        halted: bool = False) -> Quote:
        return await self.quote(symbol, exchange=exchange, tradable=tradable, halted=halted)

    async def candles(self, symbol: str, *, count: int = 200, before: datetime | None = None):
        if not 1 <= count <= 200:
            raise ValueError("count must be 1..200")
        params = {"symbol": symbols_parameter([symbol]), "interval": "1m", "count": count,
                  "adjusted": "false"}
        if before:
            params["before"] = utc(before).isoformat()
        data = await self._get("/api/v1/candles", params=params)
        return parse_candles(symbol, data, now_utc()), data.get("nextBefore")

    async def rankings(self, *, ranking_type: str = "MARKET_TRADING_AMOUNT", count: int = 20):
        if ranking_type not in {"MARKET_TRADING_AMOUNT", "MARKET_TRADING_VOLUME", "TOP_GAINERS", "TOP_LOSERS"}:
            raise ValueError("market-wide ranking required")
        if not 1 <= count <= 100:
            raise ValueError("count must be 1..100")
        return await self._get("/api/v1/rankings", params={"type": ranking_type,
            "marketCountry": "US", "duration": "1d", "count": count, "excludeInvestmentCaution": "true"})

    async def accounts(self):
        return await self._get("/api/v1/accounts")

    async def holdings(self):
        return await self._get("/api/v1/holdings", account=True)

    async def orders(self, status: str = "OPEN", *, cursor: str | None = None):
        if status not in {"OPEN", "CLOSED"}:
            raise ValueError("status must be OPEN or CLOSED")
        params = {"status": status}
        if cursor:
            params["cursor"] = cursor
        return await self._get("/api/v1/orders", params=params, account=True)

    async def market_calendar(self, date: str):
        return await self._get("/api/v1/market-calendar/US", params={"date": date})

    async def create_order(self, *args, **kwargs):
        raise CapabilityUnavailable("Live orders are disabled. Validate broker behavior and approve a separate live implementation first")

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()

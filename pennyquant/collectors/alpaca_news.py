"""Alpaca news only; no Alpaca brokerage account or trading operations."""
from __future__ import annotations

from collections import OrderedDict
import json
import os
from typing import AsyncIterator

import httpx

from ..models import NewsEvent, utc
from .common import CapabilityUnavailable, RateLimiter, get_readonly, now_utc
from .toss_market import symbols_parameter

REST_URL = "https://data.alpaca.markets/v1beta1/news"
STREAM_URL = "wss://stream.data.alpaca.markets/v1beta1/news"


def parse_news(item: dict, received_at, *, real_time: bool = False) -> NewsEvent:
    return NewsEvent(id="alpaca:" + str(item["id"]), symbols=tuple(item.get("symbols", [])),
        title=item["headline"], published_at=utc(item["created_at"]), received_at=utc(received_at),
        source=item.get("source", "alpaca"), url=item.get("url", ""),
        body=item.get("content") or item.get("summary", ""), verified=True, real_time=real_time)


class AlpacaNewsClient:
    def __init__(self, key: str | None = None, secret: str | None = None, *,
                 realtime_verified: bool = False, client: httpx.AsyncClient | None = None):
        self._key = key or os.getenv("APCA_API_KEY_ID", "")
        self._secret = secret or os.getenv("APCA_API_SECRET_KEY", "")
        self.realtime_verified = realtime_verified
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._owns_client = client is None
        self.limiter = RateLimiter(2)
        self._seen = OrderedDict()

    def _headers(self):
        if not self._key or not self._secret:
            raise CapabilityUnavailable("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY")
        return {"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret}

    def _new(self, item: dict) -> bool:
        # Article updates preserve first receipt; root's DB keeps stable article id.
        identity = (str(item["id"]), item.get("updated_at", item.get("created_at")))
        if identity in self._seen:
            return False
        self._seen[identity] = True
        if len(self._seen) > 10000:
            self._seen.popitem(last=False)
        return True

    async def fetch(self, symbols: list[str], *, start=None, end=None,
                    max_pages: int = 10) -> list[NewsEvent]:
        if not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be 1..100")
        params = {"symbols": symbols_parameter(symbols), "limit": 50,
                  "include_content": "true", "sort": "asc"}
        if start is not None:
            params["start"] = utc(start).isoformat()
        if end is not None:
            params["end"] = utc(end).isoformat()
        result = []
        tokens = set()
        for _ in range(max_pages):
            response = await get_readonly(self.client, REST_URL, limiter=self.limiter,
                                          headers=self._headers(), params=params)
            data, received = response.json(), now_utc()
            for item in data.get("news", []):
                if self._new(item):
                    # REST/historical responses cannot prove original live availability.
                    result.append(parse_news(item, received, real_time=False))
            token = data.get("next_page_token")
            if not token or token in tokens:
                break
            tokens.add(token)
            params["page_token"] = token
        return result

    async def stream(self, symbols: list[str]) -> AsyncIterator[NewsEvent]:
        from websockets.asyncio.client import connect
        symbols_parameter(symbols)
        if not self.realtime_verified:
            raise CapabilityUnavailable("Verify the account's real-time news entitlement before enabling streaming signals")
        headers = self._headers()
        async with connect(STREAM_URL, additional_headers=headers, open_timeout=15,
                           ping_interval=20, ping_timeout=20, max_size=2**20) as ws:
            authenticated = False
            subscribed = False
            # Official header authentication produces connected/authenticated frames.
            async for raw in ws:
                messages = json.loads(raw)
                if not isinstance(messages, list):
                    messages = [messages]
                for item in messages:
                    kind = item.get("T")
                    if kind == "error":
                        raise CapabilityUnavailable("Alpaca news stream error: " + str(item.get("code", "unknown")))
                    if kind == "success" and item.get("msg") == "authenticated":
                        authenticated = True
                        await ws.send(json.dumps({"action": "subscribe", "news": symbols}))
                    if kind == "subscription":
                        if not authenticated or not set(symbols).issubset(set(item.get("news", []))):
                            raise CapabilityUnavailable("Alpaca did not confirm the requested news subscription")
                        subscribed = True
                    if kind == "n":
                        if not authenticated or not subscribed:
                            raise CapabilityUnavailable("Alpaca news arrived before entitlement/subscription confirmation")
                        if self._new(item):
                            yield parse_news(item, now_utc(), real_time=True)
            raise CapabilityUnavailable("Alpaca news stream closed; halt new entries before reconnect")

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()

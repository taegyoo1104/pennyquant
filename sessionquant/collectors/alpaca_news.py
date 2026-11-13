"""Alpaca news only; no Alpaca brokerage account or trading operations."""
from __future__ import annotations

from collections import OrderedDict
import json
import logging
import os
from typing import AsyncIterator

import httpx

from ..models import NewsEvent, symbol_valid, utc
from .common import CapabilityUnavailable, DataQualityError, RateLimiter, get_readonly, now_utc
from .toss_market import symbols_parameter

REST_URL = "https://data.alpaca.markets/v1beta1/news"
STREAM_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
logger = logging.getLogger(__name__)


def _stock_symbols(symbols) -> tuple[str, ...]:
    """Keep exact supported tags; provider tags need not all be stock tickers."""
    if not isinstance(symbols, list):
        raise DataQualityError("Alpaca news requires a symbol array (field: symbols)")
    result: dict[str, None] = {}
    omitted = 0
    for symbol in symbols:
        try:
            symbol_valid(symbol)
        except ValueError:
            # This also omits null/metadata tags. Never stringify them, strip
            # punctuation or infer stock aliases from provider tags.
            omitted += 1
            continue
        result[symbol] = None
    if omitted:
        logger.warning(
            "Alpaca news: omitted %d unsupported ticker tag(s); retained %d exact ticker(s). "
            "No ticker aliases were inferred.", omitted, len(result)
        )
    return tuple(result)


def parse_news(item: dict, received_at, *, real_time: bool = False) -> NewsEvent:
    # Fixed field diagnostics must not echo provider values, article bodies or keys.
    if not isinstance(item, dict):
        raise DataQualityError("Alpaca news item must be an object (field: news[])")
    if not isinstance(item.get("id"), (str, int)) or isinstance(item.get("id"), bool) or not str(item["id"]).strip():
        raise DataQualityError("Alpaca news has an invalid article ID (field: id)")
    if not isinstance(item.get("headline"), str) or not item["headline"].strip():
        raise DataQualityError("Alpaca news requires a non-empty headline (field: headline)")
    symbols = _stock_symbols(item.get("symbols", []))
    try:
        published_at = utc(item["created_at"])
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        raise DataQualityError(
            "Alpaca news requires a timezone-aware ISO timestamp (field: created_at)"
        ) from None
    for field in ("source", "url", "content", "summary", "updated_at"):
        if item.get(field) is not None and not isinstance(item[field], str):
            raise DataQualityError(f"Alpaca news requires text or null (field: {field})")
    return NewsEvent(id="alpaca:" + str(item["id"]), symbols=symbols,
        title=item["headline"], published_at=published_at, received_at=utc(received_at),
        source=item.get("source", "alpaca") or "", url=item.get("url") or "",
        body=item.get("content") or item.get("summary") or "", verified=True, real_time=real_time)


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
        for name, value in (("APCA_API_KEY_ID", self._key), ("APCA_API_SECRET_KEY", self._secret)):
            if not isinstance(value, str) or any(not 33 <= ord(char) <= 126 for char in value):
                raise CapabilityUnavailable(
                    f"{name} has invalid characters; enter only the issued key, "
                    "without spaces, line breaks or non-ASCII characters"
                )
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
        headers = self._headers()  # Validate credentials before opening a request.
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
                                          headers=headers, params=params)
            try:
                data = response.json()
            except ValueError:
                raise DataQualityError("Alpaca returned invalid JSON (field: response)") from None
            if not isinstance(data, dict) or not isinstance(data.get("news"), list):
                raise DataQualityError("Alpaca response requires a news array (field: news)")
            token = data.get("next_page_token")
            if token is not None and not isinstance(token, str):
                raise DataQualityError("Alpaca response requires a text page token (field: next_page_token)")
            received = now_utc()
            for item in data["news"]:
                article = parse_news(item, received, real_time=False)
                # Commit deduplication only when the whole fetch can be returned.
                # A later bad item/page must not consume earlier unwritten articles.
                result.append((item, article))
            if not token or token in tokens:
                break
            tokens.add(token)
            params["page_token"] = token
        return [article for item, article in result if self._new(item)]

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
                        article = parse_news(item, now_utc(), real_time=True)
                        if self._new(item):
                            yield article
            raise CapabilityUnavailable("Alpaca news stream closed; halt new entries before reconnect")

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()

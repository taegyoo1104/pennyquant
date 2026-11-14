"""Toss read-only REST adapter, canonical OpenAPI v1.2.28 (2026-11-13).

No order-changing methods. OAuth token issuance is the sole POST operation.
Tokens are memory-only. Use one adapter per client: reissuance invalidates old tokens.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from time import monotonic

import httpx

from ..models import Bar, Quote, utc
from .common import CapabilityUnavailable, DataQualityError, RateLimiter, get_readonly, now_utc

BASE_URL = "https://openapi.tossinvest.com"


def symbols_parameter(symbols: list[str] | tuple[str, ...], maximum: int = 200) -> str:
    if not 1 <= len(symbols) <= maximum or any(not re.fullmatch(r"[A-Z0-9.\-]+", s) for s in symbols):
        raise ValueError("invalid symbols or batch size")
    return ",".join(dict.fromkeys(symbols))


def _number(value, *, integral=False, positive=False):
    """문자열 숫자를 검사하며 거래량의 소수 부분을 버리지 않는다."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('숫자 형식 미확인')
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or (positive and number <= 0):
        raise ValueError('유효 범위 밖의 숫자')
    if integral and number != number.to_integral_value():
        raise ValueError('소수 거래량·잔량은 현재 정수 모델에서 지원하지 않습니다.')
    return int(number) if integral else float(number)


def parse_orderbook(symbol: str, data: dict, received_at: datetime, *,
                    exchange: str = "UNKNOWN", tradable: bool = False, halted: bool = False) -> Quote:
    """호가는 거래 가능·거래정지 여부를 증명하지 않는다. 역전·미래·결측은 거부한다."""
    if not isinstance(data, dict) or data.get("currency") != "USD" or not data.get("timestamp"):
        raise DataQualityError("USD orderbook with a known event time required")
    asks, bids = data.get("asks", []), data.get("bids", [])
    if not isinstance(asks,list) or not isinstance(bids,list) or not asks or not bids:
        raise DataQualityError("two-sided orderbook required")
    try:
        asks = [(_number(x['price'],positive=True),_number(x['volume'],integral=True)) for x in asks]
        bids = [(_number(x['price'],positive=True),_number(x['volume'],integral=True)) for x in bids]
        ask, bid = min(asks), max(bids)
        stamp, known = utc(data['timestamp']), utc(received_at)
        if stamp > known:
            raise ValueError('미래 호가')
        return Quote(symbol,stamp,known,bid[0],ask[0],bid[1],ask[1],tradable,halted,exchange)
    except (ValueError,TypeError,KeyError,AttributeError,InvalidOperation,OverflowError):
        raise DataQualityError('호가 검증 실패: 역전·숫자·시각·잔량 형식을 확인하세요.') from None


def parse_candles(symbol: str, data: dict, received_at: datetime, *, observation=None) -> list[Bar]:
    """1분 종료 시각을 보존하고 미완성 봉을 제외한다. 누락 봉은 생성하지 않는다."""
    result = []
    report = observation if observation is not None else {}
    report.update(source='toss:/api/v1/candles:1m',price_adjustment='raw',volume_comparability='UNKNOWN',
                  zero_volume_policy='UNKNOWN',received_at=utc(received_at).isoformat(),future_excluded=0)
    try:
        if not isinstance(data,dict) or not isinstance(data.get('candles'),list):
            raise ValueError('분봉 응답은 배열이어야 합니다.')
        report['raw_count'] = len(data['candles'])
        for item in data['candles']:
            if item.get("currency") != "USD":
                raise ValueError('USD 분봉 필요')
            timestamp = utc(item['timestamp'])
            if timestamp.second or timestamp.microsecond:
                raise ValueError('1분 종료 경계 필요')
            if timestamp > utc(received_at):
                report['future_excluded'] += 1
                continue
            result.append(Bar(symbol,timestamp,utc(received_at),_number(item['openPrice'],positive=True),
                _number(item['highPrice'],positive=True),_number(item['lowPrice'],positive=True),
                _number(item['closePrice'],positive=True),_number(item['volume'],integral=True),
                source=report['source'],price_adjustment='raw'))
        report.update(status='PARSED',accepted_count=len(result))
    except (ValueError,TypeError,KeyError,AttributeError,InvalidOperation,OverflowError):
        report.update(status='INVALID',accepted_count=0)
        raise DataQualityError('분봉 검증 실패: 통화·종료 시각·정수 거래량·OHLC를 확인하세요.') from None
    return sorted(result,key=lambda b:b.timestamp)


class TossMarketClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 account_seq: int | None = None, *, client: httpx.AsyncClient | None = None):
        self._client_id = client_id or os.getenv("TOSS_CLIENT_ID", "")
        self._client_secret = client_secret or os.getenv("TOSS_CLIENT_SECRET", "")
        seq = account_seq if account_seq is not None else os.getenv("TOSS_ACCOUNT_SEQ")
        self.account_seq = int(seq) if seq is not None and seq != "" else None
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self.read_observations = {}
        self.candle_observations = {}
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
        started = monotonic()
        try:
            response = await get_readonly(self.client, BASE_URL + path, limiter=self.limiter,
                                          headers=headers, params=params)
        except httpx.HTTPStatusError as exc:
            self._observe_read(path,exc.response,started)
            raise
        self._observe_read(path,response,started)
        data = response.json()
        if "result" not in data or "error" in data:
            raise DataQualityError("Toss response missing successful result envelope")
        return data["result"]

    def _observe_read(self,path,response,started):
        """시장 조회 계측만 보존한다. 인증·계좌·임의 응답 헤더는 저장하지 않는다."""
        if path not in {'/api/v1/prices','/api/v1/stocks','/api/v1/candles','/api/v1/orderbook','/api/v1/sectors'} and not re.fullmatch(r'/api/v1/sectors/[A-Za-z0-9_-]{1,40}/stocks',path):
            return
        limits = {}
        for key in ('ratelimit-limit','ratelimit-remaining','ratelimit-reset',
                    'x-ratelimit-limit','x-ratelimit-remaining','x-ratelimit-reset'):
            value = response.headers.get(key,'')
            if re.fullmatch(r'[0-9]{1,15}(?:[.][0-9]{1,6})?',value):
                limits[key] = value
        self.read_observations[path] = {'http_status':response.status_code,
            'duration_seconds':round(monotonic()-started,3),'received_at':now_utc().isoformat(),
            'rate_headers':limits,'rate_headers_observed':bool(limits),'account_quota_verified':False,'cost_usd':None}

    async def sectors(self):
        """전체 섹터 계층을 조회한다. 시총은 거래대금 순위와 구분한다."""
        return await self._get('/api/v1/sectors')

    async def sector_stocks(self, sector_id: str):
        """하위 섹터 포함 미국 보통주의 원화 환산 시총을 읽는다."""
        if not isinstance(sector_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',sector_id):
            raise ValueError('토스 섹터 ID 형식을 확인하세요.')
        return await self._get(f'/api/v1/sectors/{sector_id}/stocks',params={'marketCountry':'US'})

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
        report = self.candle_observations.setdefault(symbol,{})
        received = now_utc()
        bars = parse_candles(symbol,data,received,observation=report)
        following = data.get('nextBefore')
        try:
            if following is not None and (not isinstance(following,str) or utc(following) > received):
                raise ValueError('과거 페이지 커서 미확인')
            if following is not None:
                utc(following)
        except (ValueError,TypeError,AttributeError):
            report['status'] = 'INVALID_CURSOR'
            raise DataQualityError('분봉 다음 페이지 시각을 확인할 수 없습니다.') from None
        return bars, following

    async def reference_candles(self,symbol: str,*,interval='1m',count=200):
        """참조군은 원본 Decimal 문자열로 계산하며 주문·거래 가능 플래그를 만들지 않는다."""
        if interval not in {'1m','1d'} or not 1 <= count <= 200:
            raise ValueError('참조군 봉 주기·개수를 확인하세요.')
        return await self._get('/api/v1/candles',params={'symbol':symbols_parameter([symbol]),
            'interval':interval,'count':count,'adjusted':'false'})

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

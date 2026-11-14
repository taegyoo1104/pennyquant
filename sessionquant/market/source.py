"""토스 읽기 전용 API에서 시장 확인용 자료를 수집한다.

StockInfo.status=ACTIVE는 상장 상태만 뜻한다. 공식 응답으로 확인하지 못한
미국 거래정지·소수점 지원·계좌 주문 가능성을 생성하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..collectors.toss_market import TossMarketClient
from ..models import Bar, Quote, utc
from .bars import session_bounds
from .scanner import InstrumentEligibility


@dataclass
class MarketBatch:
    bars: list[Bar] = field(default_factory=list)
    quotes: list[Quote] = field(default_factory=list)
    eligibility: list[InstrumentEligibility] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    observed_at: datetime | None = None


class TossSessionSource:
    def __init__(self, client: TossMarketClient | None = None, *, candidates_per_cycle: int = 2, clock=None):
        if type(candidates_per_cycle) is not int or not 1 <= candidates_per_cycle <= 10:
            raise ValueError("한 주기에 상세 수집할 종목 수는 1~10개입니다")
        self.client = client or TossMarketClient()
        self.owns_client = client is None
        self.candidates_per_cycle = candidates_per_cycle
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cursor = 0
        self._seen: dict[str, set[tuple]] = {}

    async def poll(self, symbols: list[str], now: datetime | None = None, *,
                   priority_symbols: tuple[str, ...] | list[str] = ()) -> MarketBatch:
        now = utc(now or self.clock())
        candidates = list(dict.fromkeys(symbol for symbol in symbols if symbol not in {"SPY", "QQQ"}))
        count = min(self.candidates_per_cycle, len(candidates))
        chosen = [candidates[(self.cursor + i) % len(candidates)] for i in range(count)] if candidates else []
        if candidates:
            self.cursor = (self.cursor + count) % len(candidates)
        selected = list(dict.fromkeys(["SPY", "QQQ", *priority_symbols, *chosen]))
        batch, metadata = MarketBatch(), {}
        try:
            response = await self.client.stocks(selected)
            rows = response if isinstance(response, list) else response.get("stocks", [])
            metadata = {row["symbol"]: row for row in rows if isinstance(row, dict) and row.get("symbol") in selected}
        except Exception as exc:
            batch.errors.append({"symbol": "*", "source": "toss_stocks", "error_type": type(exc).__name__,
                                 "message": "종목 마스터를 확인하지 못했습니다"})
        bounds = session_bounds(now)
        for symbol in selected:
            try:
                bars, before = await self.client.candles(symbol, count=200)
                # 장중 늦게 시작해도 당일 VWAP에 필요한 개장 이후 봉을 확인한다.
                if bounds and bars and min(b.timestamp for b in bars) > bounds[0] and before:
                    previous, _ = await self.client.candles(symbol, count=200, before=utc(before))
                    bars = previous + bars
                seen = self._seen.setdefault(symbol, set())
                for bar in bars:
                    identity = (bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume)
                    if identity not in seen:
                        seen.add(identity)
                        batch.bars.append(bar)
                if len(seen) > 1000:
                    self._seen[symbol] = set(sorted(seen, key=lambda item: item[0])[-800:])
            except Exception as exc:
                batch.errors.append({"symbol": symbol, "source": "toss_candles", "error_type": type(exc).__name__,
                                     "message": "확정봉 수집에 실패했습니다"})
        # 오래 걸릴 수 있는 봉 조회를 마친 뒤 호가를 갱신한다.
        for symbol in selected:
            stock = metadata.get(symbol, {})
            exchange = stock.get("market", "UNKNOWN") if stock.get("currency") == "USD" else "UNKNOWN"
            try:
                batch.quotes.append(await self.client.quote(symbol, exchange=exchange, tradable=False))
            except Exception as exc:
                batch.errors.append({"symbol": symbol, "source": "toss_orderbook", "error_type": type(exc).__name__,
                                     "message": "호가 수집에 실패했습니다"})
            if symbol in chosen:
                batch.eligibility.append(InstrumentEligibility(symbol, utc(self.clock()), "toss:stock_info:incomplete",
                                                               exchange=exchange))
        batch.observed_at = utc(self.clock())
        batch.bars.sort(key=lambda bar: (bar.received_at, bar.timestamp, bar.symbol))
        return batch

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()

"""거래소 달력과 소수점 접수 마감에 맞춘 당일 청산 관리자."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .broker import PaperBroker
from .manager import PortfolioManager
from .models import aware, serialize


@lru_cache(maxsize=16)
def _calendar(year):
    import exchange_calendars
    return exchange_calendars.get_calendar('XNYS', start=f'{year}-01-01', end=f'{year}-12-31')


@dataclass(frozen=True)
class SessionSchedule:
    session_date: str
    opens_at: datetime
    closes_at: datetime
    entry_start: datetime
    entry_end: datetime
    exit_start: datetime
    residual_check: datetime
    emergency_at: datetime
    fractional_deadline: datetime

    def to_dict(self):
        return serialize(self)


class SessionExitManager:
    """주문 전송과 청산 완료를 구분하며 잔여 수량을 끝까지 추적한다.

    미국 소수점 주문 접수는 정규장 종료 1시간 전까지라는 공식 제한을
    적용한다. 조기 폐장일에는 마감·청산 시각을 같은 간격으로 앞당긴다.
    진입 시작은 개장 75분 후로 유지하므로 짧은 거래일에는 진입이 없을 수 있다.
    """
    def __init__(self, broker: PaperBroker):
        self.broker = broker
        self.manager = PortfolioManager(broker)

    def schedule(self, now: datetime) -> SessionSchedule | None:
        now = aware(now)
        day = now.astimezone(ZoneInfo('America/New_York')).date()
        try:
            import pandas as pd
            cal = _calendar(day.year)
            session = pd.Timestamp(day)
            if not cal.is_session(session):
                return None
            opening = aware(cal.session_open(session).to_pydatetime())
            closing = aware(cal.session_close(session).to_pydatetime())
        except (ValueError, KeyError, IndexError, RuntimeError, ImportError, TypeError):
            return None
        from ..session_window import schedule_times
        times=schedule_times(now,min_entry_to_exit_minutes=self.broker.settings.min_entry_to_exit_minutes)
        if not times: return None
        return SessionSchedule(day.isoformat(),**times)

    def permits_entry(self, now: datetime) -> bool:
        now = aware(now)
        schedule = self.schedule(now)
        return bool(schedule and schedule.entry_start <= now < schedule.entry_end and not self.broker.halted)

    def tick(self, now: datetime, quotes: dict) -> dict:
        """청산을 요청한다. 체결 시점은 연결된 모의 주문 처리 정책을 따른다."""
        now = aware(now)
        schedule = self.schedule(now)
        self.broker.roll_session(now)
        self.broker.expire_orders(now)
        self.broker.check_daily_loss(now)
        can_enter = self.permits_entry(now)
        orders = []
        if not can_enter:
            for order in self.broker.get_open_orders():
                if order.intent.side == 'buy':
                    self.broker.cancel_order(order.client_id,now,
                        reason='daily_loss_limit' if self.broker.halted else 'outside_entry_window')
            if schedule and now >= schedule.exit_start:
                for order in self.broker.get_open_orders():
                    if order.intent.side == 'buy':
                        self.broker._alert('entry_cancel_unconfirmed',now,'청산 전에 매수 취소 확인이 필요합니다.',order.intent.symbol)
        today = now.astimezone(ZoneInfo('America/New_York')).date()
        for symbol, position in list(self.broker.positions.items()):
            overnight = position.opened_at.astimezone(ZoneInfo('America/New_York')).date() < today
            due = bool(schedule and now >= schedule.exit_start)
            if not due and not overnight and symbol not in self.broker.exit_intents:
                continue
            reason = 'overnight_position' if overnight else 'session_exit'
            self.broker.exit_intents.setdefault(symbol, {'reason': reason, 'opened_at': position.opened_at.isoformat(),
                                                         'requested_at': now.isoformat(), 'attempt': 0})
            if overnight:
                self.broker._alert('overnight_position', now, '전일 미청산 포지션이 있습니다. 잔여 수량을 확인하세요.', symbol)
            if schedule is None:
                self.broker._alert('session_unknown', now, '거래소 세션을 확인할 수 없어 주문을 보류합니다.', symbol)
                continue
            if now >= schedule.emergency_at:
                self.broker._alert('session_exit_incomplete', now, '당일 청산이 완료되지 않았습니다. 보유 수량과 주문 상태를 확인하세요.', symbol)
            if not schedule.opens_at <= now < schedule.fractional_deadline:
                continue
            quote = quotes.get(symbol)
            if quote is None or not self.broker.quote_is_fresh(quote, now):
                self.broker._alert('exit_quote_unavailable', now, '청산에 필요한 최신 호가가 없습니다. 잔여 포지션을 유지합니다.', symbol)
                continue
            order = self.manager._queue_exit(symbol, quote, now, reason)
            if order is not None:
                orders.append(order)
        return self.snapshot(now,orders=[o.client_id for o in orders],schedule=schedule)

    def snapshot(self, now, *, orders=(), schedule=None):
        """주문을 만들지 않고 현재 체결 잔량·진행 주문으로 청산 상태만 계산한다."""
        now = aware(now)
        schedule = schedule or self.schedule(now)
        remaining = {symbol:str(p.qty) for symbol,p in self.broker.positions.items()}
        pending = self.broker.get_open_orders()
        return {'schedule': schedule.to_dict() if schedule else None,
                'entry_allowed':bool(schedule and schedule.entry_start <= now < schedule.entry_end and not self.broker.halted),
                'positions_remaining':remaining,'liquidation_complete':not remaining and not pending,
                'pending_orders':[{'client_id':o.client_id,'symbol':o.intent.symbol,'side':o.intent.side,'status':o.status} for o in pending],
                'pending_buy_cancellations':[o.client_id for o in pending if o.intent.side == 'buy' and (o.cancel_requested_at is not None or o.status == 'unknown')],
                'unknown_orders':[o.client_id for o in pending if o.status == 'unknown'],
                'orders':list(orders),'alerts':list(self.broker.alerts),
                'session_status': 'UNKNOWN' if schedule is None else 'KNOWN'}

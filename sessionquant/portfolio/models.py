"""소수점 모의 주문의 금액·수량과 복구 가능한 상태를 정의한다."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_CEILING
from typing import Any

ZERO = Decimal('0')
CENT = Decimal('0.01')
QUANTITY_STEP = Decimal('0.000001')


def decimal(value: Any, name: str = 'value', *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    """JSON 숫자와 문자열을 이진 부동소수점 연산 없이 검증한다."""
    if isinstance(value, bool):
        raise ValueError(f'{name}: 불리언은 금액으로 사용할 수 없습니다.')
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'{name}: 올바른 숫자가 필요합니다.') from None
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f'{name}: 유효한 유한 금액 또는 수량이 필요합니다.')
    return result


def aware(value: datetime | str) -> datetime:
    value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('시간대가 포함된 시각이 필요합니다.')
    return value.astimezone(timezone.utc)


def serialize(value: Any) -> Any:
    """금융 숫자는 문자열로 저장하여 JSON 왕복 시 정밀도를 유지한다."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return aware(value).isoformat()
    if hasattr(value, '__dataclass_fields__'):
        return {f.name: serialize(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): serialize(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [serialize(v) for v in value]
    return value


@dataclass(frozen=True)
class PortfolioSettings:
    initial_cash: Decimal = Decimal('100')
    initial_weight: Decimal = Decimal('0.20')
    add_weight: Decimal = Decimal('0.20')
    max_symbol_weight: Decimal = Decimal('0.40')
    max_gross_weight: Decimal = Decimal('0.80')
    risk_per_symbol: Decimal = Decimal('0.0075')
    daily_loss_limit: Decimal = Decimal('0.02')
    target_return: Decimal = Decimal('0.10')
    commission_rate: Decimal = Decimal('0.001')
    commission_free_notional: Decimal = Decimal('10')
    sell_regulatory_rate: Decimal = Decimal('0.0000206')
    sell_regulatory_min: Decimal = Decimal('0.01')
    slippage_rate: Decimal = Decimal('0.001')
    minimum_order: Decimal = Decimal('1')
    max_positions: int = 5
    preferred_positions: int = 2
    max_adds: int = 1
    allow_adds: bool = True
    max_quote_age_seconds: int = 10
    order_ttl_seconds: int = 60
    min_entry_to_exit_minutes: int = 30
    paper_fill_model: str = 'market_v2'
    paper_latency_seconds: Decimal = Decimal('1')
    paper_cancel_latency_seconds: Decimal = ZERO
    paper_depth_participation: Decimal = Decimal('0.10')

    def __post_init__(self):
        for item in fields(self):
            if isinstance(item.default, Decimal):
                object.__setattr__(self, item.name, decimal(getattr(self, item.name), item.name, nonnegative=True))
        if self.initial_cash <= 0 or self.minimum_order <= 0:
            raise ValueError('초기 자본과 최소 모의 주문 금액은 양수여야 합니다.')
        if self.paper_fill_model not in {'market_v2','legacy_limit_v1'}:
            raise ValueError('모의 체결 모델은 market_v2 또는 legacy_limit_v1이어야 합니다.')
        if not ZERO < self.paper_depth_participation <= 1 or max(self.paper_latency_seconds,self.paper_cancel_latency_seconds) > 3600:
            raise ValueError('호가 참여율은 0 초과 1 이하, 가정 지연은 최대 3600초여야 합니다.')
        if any(value != value.quantize(Decimal('0.000001')) for value in (self.paper_latency_seconds,self.paper_cancel_latency_seconds)):
            raise ValueError('모의 시계의 지연은 마이크로초 단위까지만 지원합니다.')
        for name in ('initial_weight', 'add_weight', 'max_symbol_weight', 'max_gross_weight', 'risk_per_symbol',
                     'daily_loss_limit', 'target_return'):
            if not ZERO < getattr(self, name) < 1:
                raise ValueError(f'{name}: 0과 1 사이 비율이 필요합니다.')
        if self.initial_weight > self.max_symbol_weight or self.max_symbol_weight > self.max_gross_weight:
            raise ValueError('최초·종목·전체 투자 비중의 순서가 잘못되었습니다.')
        if self.slippage_rate >= 1 or self.commission_rate + self.sell_regulatory_rate >= 1:
            raise ValueError('수수료와 슬리피지는 거래대금보다 작아야 합니다.')
        for name in ('max_positions', 'preferred_positions', 'max_adds', 'max_quote_age_seconds', 'order_ttl_seconds'):
            if type(getattr(self, name)) is not int:
                raise ValueError(f'{name}: 정수가 필요합니다.')
        if not 1 <= self.max_positions <= 5 or not 1 <= self.preferred_positions <= self.max_positions:
            raise ValueError('최대 보유 종목은 5개, 권장 보유 수는 최대 보유 수 이하여야 합니다.')
        if not 0 <= self.max_adds <= 1 or type(self.allow_adds) is not bool:
            raise ValueError('추가매수는 최대 1회만 설정할 수 있습니다.')
        if type(self.min_entry_to_exit_minutes) is not int or self.min_entry_to_exit_minutes<30:
            raise ValueError('청산 전 진입 여유는 최소 30분이어야 합니다.')
        if min(self.max_quote_age_seconds, self.order_ttl_seconds) <= 0:
            raise ValueError('유효 기간은 양수여야 합니다.')

    def fees(self, side: str, notional: Decimal) -> Decimal:
        """실제 체결 수수료와 다를 수 있는 보수적인 모의 비용을 계산한다."""
        return self.fee_breakdown(side,notional)['total']

    def fee_breakdown(self, side: str, notional: Decimal) -> dict:
        """기존 모의 요율의 주문 누적 비용을 항목별로 분리한다. 실제 청구액이 아니다."""
        amount = decimal(notional, 'notional', nonnegative=True)
        if side not in ('buy', 'sell'):
            raise ValueError('주문 방향은 buy 또는 sell이어야 합니다.')
        if amount == 0:
            return {'commission':ZERO,'regulatory':ZERO,'total':ZERO}
        commission = ZERO if amount <= self.commission_free_notional else (amount * self.commission_rate).quantize(CENT, rounding=ROUND_CEILING)
        regulatory = max(amount * self.sell_regulatory_rate, self.sell_regulatory_min).quantize(CENT, rounding=ROUND_CEILING) if side == 'sell' else ZERO
        return {'commission':commission,'regulatory':regulatory,'total':commission+regulatory}


@dataclass(frozen=True)
class OrderIntent:
    client_id: str
    symbol: str
    side: str
    created_at: datetime
    limit_price: Decimal
    amount: Decimal | None = None
    qty: Decimal | None = None
    stop_price: Decimal | None = None
    strategy: str = ''
    reason: str = ''
    is_add: bool = False
    expected_price: Decimal | None = None
    reference_bid: Decimal | None = None

    def __post_init__(self):
        from ..models import symbol_valid
        symbol_valid(self.symbol)
        if not isinstance(self.client_id, str) or not self.client_id or len(self.client_id) > 200:
            raise ValueError('1~200자의 주문 고유 ID가 필요합니다.')
        if self.side not in ('buy', 'sell') or type(self.is_add) is not bool:
            raise ValueError('주문 방향 또는 추가매수 표시가 올바르지 않습니다.')
        object.__setattr__(self, 'created_at', aware(self.created_at))
        object.__setattr__(self, 'limit_price', decimal(self.limit_price, 'limit_price', positive=True))
        for name in ('amount', 'qty', 'stop_price', 'expected_price', 'reference_bid'):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, decimal(getattr(self, name), name, positive=True))
        if self.side == 'buy' and (self.amount is None or self.qty is not None or self.stop_price is None):
            raise ValueError('매수는 금액과 손절 가격을 지정하고 수량은 지정하지 않습니다.')
        if self.side == 'sell' and (self.qty is None or self.amount is not None):
            raise ValueError('매도는 소수점 수량으로 지정합니다.')
        if self.side == 'buy' and self.stop_price >= self.limit_price:
            raise ValueError('손절 가격은 매수 제한 가격보다 낮아야 합니다.')
        if self.reference_bid is not None and self.expected_price is not None and self.reference_bid > self.expected_price:
            raise ValueError('참조 매수호가가 예상 매도호가보다 높습니다.')


@dataclass
class FractionalOrder:
    intent: OrderIntent
    status: str = 'open'
    filled_qty: Decimal = ZERO
    filled_notional: Decimal = ZERO
    fee: Decimal = ZERO
    rejection_reason: str = ''
    fill_model_version: str = 'legacy_limit_v1'
    submitted_at: datetime | None = None
    accepted_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    cancel_effective_at: datetime | None = None
    expected_price: Decimal | None = None
    expected_price_source: str = 'UNKNOWN'
    simulated_fill_price: Decimal | None = None
    assumed_latency: Decimal | None = None
    slippage_bps: Decimal | None = None
    fee_breakdown: dict = field(default_factory=dict)
    unfilled_reason: str = ''
    lifecycle: list[dict] = field(default_factory=list)

    @property
    def client_id(self):
        return self.intent.client_id

    @property
    def remaining_qty(self):
        return max(ZERO, self.intent.qty - self.filled_qty) if self.intent.qty is not None else ZERO

    @property
    def remaining_amount(self):
        return max(ZERO, self.intent.amount - self.filled_notional) if self.intent.amount is not None else ZERO


@dataclass
class FractionalPosition:
    symbol: str
    qty: Decimal
    avg_price: Decimal
    entry_fee: Decimal
    stop_price: Decimal
    opened_at: datetime
    strategy: str = ''
    adds: int = 0
    initial_amount: Decimal = ZERO
    add_amount: Decimal = ZERO
    mark_price: Decimal = ZERO

    @property
    def cost_basis(self):
        return self.qty * self.avg_price + self.entry_fee


@dataclass(frozen=True)
class FractionalFill:
    id: str
    client_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    timestamp: datetime
    realized_pnl: Decimal = ZERO
    fill_model_version: str = 'legacy_limit_v1'
    expected_price: Decimal | None = None
    simulated_fill_price: Decimal | None = None
    assumed_latency: Decimal | None = None
    slippage_bps: Decimal | None = None
    fee_breakdown: dict = field(default_factory=dict)
    quote_timestamp: datetime | None = None
    quote_received_at: datetime | None = None
    observed_latency: Decimal | None = None
    execution_mode: str = 'paper'


@dataclass(frozen=True)
class AllocationDecision:
    approved: bool
    reason: str
    intent: OrderIntent | None = None
    audit: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AccountView:
    cash: Decimal
    equity: Decimal
    day_start_equity: Decimal
    reserved_cash: Decimal
    realized_pnl: Decimal
    positions: dict[str, FractionalPosition] = field(default_factory=dict)
    halted: bool = False

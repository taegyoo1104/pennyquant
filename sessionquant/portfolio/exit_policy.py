"""검증 전 손절·익절 가설을 비교하는 연구 설정. 운영 기본값을 최적화하지 않는다."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .models import aware, decimal


@dataclass(frozen=True)
class ExitPolicy:
    stop_mode: str = 'strategy_atr'
    atr_multiple: Decimal = Decimal('1')
    fixed_stop_pct: Decimal = Decimal('0.01')
    profit_protection: bool = True
    max_holding_minutes: int | None = None

    def __post_init__(self):
        if self.stop_mode not in {'strategy_atr','atr','fixed_percent'}:
            raise ValueError('손절 비교 방식은 strategy_atr·atr·fixed_percent 중 하나입니다.')
        object.__setattr__(self,'atr_multiple',decimal(self.atr_multiple,positive=True))
        object.__setattr__(self,'fixed_stop_pct',decimal(self.fixed_stop_pct,positive=True))
        if self.fixed_stop_pct >= 1 or type(self.profit_protection) is not bool:
            raise ValueError('고정 손절 비율·이익 보호 설정이 올바르지 않습니다.')
        if self.max_holding_minutes is not None and (type(self.max_holding_minutes) is not int or not 1 <= self.max_holding_minutes <= 390):
            raise ValueError('보유 시간 비교는 1~390분 또는 미사용이어야 합니다.')

    def stop_price(self, reference, original, atr_value):
        """부족한 ATR을 고정 수치로 채우지 않는다."""
        reference = decimal(reference,positive=True)
        if self.stop_mode == 'strategy_atr':
            result = decimal(original,positive=True)
        elif self.stop_mode == 'atr':
            if atr_value is None:
                return None
            result = reference-decimal(atr_value,positive=True)*self.atr_multiple
        else:
            result = reference*(1-self.fixed_stop_pct)
        return result if 0 < result < reference else None

    def time_due(self, opened_at, now):
        return bool(self.max_holding_minutes is not None and aware(now) >= aware(opened_at)+timedelta(minutes=self.max_holding_minutes))

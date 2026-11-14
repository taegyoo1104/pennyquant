"""거래소 달력과 청산 전 최소 진입 여유시간을 공유한다."""
from datetime import timedelta
from .market.bars import session_bounds
from .models import utc


def schedule_times(now,*,min_entry_to_exit_minutes=30):
    if type(min_entry_to_exit_minutes) is not int or min_entry_to_exit_minutes<30:
        raise ValueError('청산 전 신규 매수 금지 여유는 최소 30분입니다.')
    bounds=session_bounds(now)
    if not bounds: return None
    opened,closed=bounds; deadline=closed-timedelta(hours=1); exit_start=deadline-timedelta(minutes=20)
    return {'opens_at':opened,'closes_at':closed,'fractional_deadline':deadline,
        'entry_start':opened+timedelta(minutes=75),
        'entry_end':min(deadline-timedelta(minutes=90),exit_start-timedelta(minutes=min_entry_to_exit_minutes)),
        'exit_start':exit_start,'residual_check':deadline-timedelta(minutes=15),'emergency_at':deadline-timedelta(minutes=10)}


def permits_new_entry(now,*,min_entry_to_exit_minutes=30):
    now=utc(now); schedule=schedule_times(now,min_entry_to_exit_minutes=min_entry_to_exit_minutes)
    return bool(schedule and schedule['entry_start']<=now<schedule['entry_end'])

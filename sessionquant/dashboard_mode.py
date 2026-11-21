"""Explicit display-only process mode, never inferred from stale DB records."""
import os
from datetime import datetime, timezone


def inspection_mode():
    return os.environ.get('SQ_DASHBOARD_READ_ONLY') == '1'


def inspection_start_message(now=None):
    from .market.bars import session_bounds
    now = now or datetime.now(timezone.utc)
    try:
        bounds = session_bounds(now)
    except (ValueError, RuntimeError, OSError):
        return '거래소 일정을 확인하지 못했습니다. 매매를 시작하지 않습니다.'
    if bounds is None:
        return '오늘은 휴장일입니다. 저장된 기록은 계속 확인할 수 있습니다.'
    if now >= bounds[1]:
        return '오늘 정규장이 종료되었습니다. 저장된 기록은 계속 확인할 수 있습니다.'
    return '점검 화면이 실행 중입니다. 매매 프로그램을 시작한 뒤 다시 시도하세요.'


def require_writable_dashboard():
    if inspection_mode():
        raise ValueError(inspection_start_message())

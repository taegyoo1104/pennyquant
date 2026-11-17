"""거래소 달력에 따른 서버 종료 시각. 외부 API·DB·주문을 사용하지 않는다."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import math
import signal

from .market.bars import NY, calendar
from .models import utc


def shutdown_delay(now: datetime, grace_minutes: int = 5) -> int:
    """뉴욕 현지 날짜의 폐장 + 유예 시간까지 초. 휴장/이미 종료면 0."""
    if type(grace_minutes) is not int or not 0 <= grace_minutes <= 60:
        raise ValueError('종료 유예 시간은 0~60분 정수여야 합니다.')
    now = utc(now)
    day = now.astimezone(NY).date()
    cal = calendar(day.year)
    # 달력 오류와 휴장을 구분한다. 오류는 호출자에게 전달한다.
    if not cal.is_session(str(day)):
        return 0
    end = cal.session_close(str(day)).to_pydatetime() + timedelta(minutes=grace_minutes)
    return max(0, math.ceil((end - now).total_seconds()))


async def with_graceful_sigterm(operation):
    """컨테이너 종료 요청에도 런타임 finally의 장부 저장·작업 정리를 실행한다."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous = signal.getsignal(signal.SIGTERM)
    installed = requested = False

    def terminate():
        nonlocal requested
        if not requested:
            requested = True
            task.cancel()

    try:
        try:
            loop.add_signal_handler(signal.SIGTERM, terminate)
            installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows/보조 스레드에서는 기존 종료 동작을 유지한다.
        try:
            return await operation()
        except asyncio.CancelledError:
            if not requested:
                raise
            return 0
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, previous)


def main():
    parser = argparse.ArgumentParser(description='오늘 장 마감 후 자동 종료까지 남은 초')
    parser.add_argument('--grace-minutes', type=int, default=5)
    args = parser.parse_args()
    print(shutdown_delay(datetime.now(timezone.utc), args.grace_minutes))


if __name__ == '__main__':
    main()

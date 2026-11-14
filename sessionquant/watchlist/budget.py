"""한 수집 주기의 실제 읽기 재시도까지 제한한다. 주문 재전송 도구가 아니다."""
from contextvars import ContextVar


class ReadBudgetExceeded(RuntimeError):
    pass


class ReadCallBudget:
    def __init__(self, limit):
        self.limit, self.used, self.reserved = limit, 0, 0

    def take(self):
        if self.used+self.reserved >= self.limit:
            raise ReadBudgetExceeded('시세 조회 예산을 소진했습니다. 이전 데이터를 최신 값으로 표시하지 않습니다.')
        self.used += 1


retry_budget = ContextVar('sessionquant_read_retry_budget', default=None)


def provider_retry_until(value, now):
    """초 단위와 HTTP 날짜 형식의 대기를 보존하고 큰 값을 임의로 줄이지 않는다."""
    from datetime import datetime, timedelta, timezone
    from email.utils import parsedate_to_datetime
    import math
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            raise ValueError('유한 대기시간 필요')
        return now+timedelta(seconds=max(60,seconds))
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                raise ValueError('시간대 필요')
            return max(now+timedelta(seconds=60),date.astimezone(timezone.utc))
        except (ValueError,TypeError,AttributeError):
            return now+timedelta(seconds=60)

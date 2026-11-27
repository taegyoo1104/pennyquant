from datetime import datetime, timezone

import pytest

from sessionquant.dashboard import session_hours as hours
from sessionquant.dashboard.labels import market_hours_label


@pytest.mark.parametrize('stamp,kind,opening,closing,entry', [
    ('2026-11-25T16:00:00+00:00', '정규장', '11/25 23:30', '11/26 06:00', '11/26 00:45 ~ 11/26 03:30'),
    ('2026-07-08T16:00:00+00:00', '정규장', '07/08 22:30', '07/09 05:00', '07/08 23:45 ~ 07/09 02:30'),
    ('2026-11-27T15:15:00+00:00', '조기 폐장', '11/27 23:30', '11/28 03:00', '오늘 신규 매수 없음'),
])
def test_exchange_calendar_and_korean_date_rollover(stamp, kind, opening, closing, entry):
    now = datetime.fromisoformat(stamp)
    result = hours.session_hours(now)
    assert result['kind'] == kind
    assert result['rows'] == [('개장', opening), ('폐장', closing)]
    assert entry in result['note']
    html = hours.session_hours_html(now)
    assert '한국시간(KST)' in html and opening in html and closing in html
    if kind == '조기 폐장':
        assert result['warning'] and '11/28 00:45' in result['note'] and '11/28 00:30' in result['note']
        assert market_hours_label(now) == '오늘 신규 매수 없음 · 보유 종목 관리'


@pytest.mark.parametrize('day', [26, 28])
def test_holiday_and_weekend_have_no_invented_trading_hours(day):
    result = hours.session_hours(datetime(2026, 11, day, 17, tzinfo=timezone.utc))
    assert result['kind'] == '휴장' and result['rows'] == [] and not result['warning']


def test_korean_midnight_keeps_active_us_session_date():
    result = hours.session_hours(datetime.fromisoformat('2026-11-28T01:00:00+09:00'))
    assert result['day'] == '2026-11-27' and result['kind'] == '조기 폐장'


def test_failed_calendar_lookup_is_unknown_not_holiday(monkeypatch):
    def fail(*args):
        raise ValueError('calendar unavailable')
    monkeypatch.setattr(hours, 'calendar', fail)
    result = hours.session_hours(datetime(2026, 11, 27, 17, tzinfo=timezone.utc))
    assert result['kind'] == '시간 미확인' and result['rows'] == [] and result['warning']


def test_missing_schedule_does_not_claim_holiday(monkeypatch):
    monkeypatch.setattr(hours, 'schedule_times', lambda now: None)
    result = hours.session_hours(datetime(2026, 11, 27, 17, tzinfo=timezone.utc))
    assert result['kind'] == '시간 미확인' and result['rows'] == []

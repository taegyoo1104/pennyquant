"""Today's exchange session, displayed in Korea time without DB/API reads."""
from datetime import timedelta
from html import escape
from zoneinfo import ZoneInfo

from ..market.bars import calendar
from ..models import utc
from ..session_window import schedule_times

KST = ZoneInfo('Asia/Seoul')
NY = ZoneInfo('America/New_York')


def session_hours(now):
    now = utc(now)
    day = now.astimezone(NY).date()
    result = {'day': day.isoformat(), 'kind': '시간 미확인', 'rows': [],
              'note': '거래소 달력을 확인하지 못했습니다. 매매 가능 시간을 확인하세요.', 'warning': True}
    try:
        # session_bounds returns None for both holidays and lookup failures.
        # Only a successful calendar lookup is evidence of a holiday.
        if not calendar(day.year).is_session(str(day)):
            return {**result, 'kind': '휴장', 'note': '오늘은 미국 정규장이 열리지 않습니다.', 'warning': False}
        times = schedule_times(now)
        if times is None:
            return result
        early = times['closes_at'] - times['opens_at'] < timedelta(hours=6, minutes=30)
        def display(value):
            return value.astimezone(KST).strftime('%m/%d %H:%M')
        rows = [('개장', display(times['opens_at'])), ('폐장', display(times['closes_at']))]
        no_entry = times['entry_start'] >= times['entry_end']
        note = ('오늘 신규 매수 없음 · 현재 시간 규칙상 매수 시작(' + display(times['entry_start'])
                + ')이 마감(' + display(times['entry_end']) + ')보다 늦거나 같습니다.'
                if no_entry else '신규 매수 가능 시간 · ' + display(times['entry_start']) + ' ~ '
                + display(times['entry_end']) + ' (마감 시각 제외)')
        return {**result, 'kind': '조기 폐장' if early else '정규장', 'rows': rows,
                'note': note, 'warning': no_entry}
    except (ValueError, KeyError, TypeError, ImportError, OverflowError, RuntimeError):
        return result


def session_hours_html(now):
    view = session_hours(now)
    rows = ''.join('<div><span>' + escape(name) + '</span><strong>' + escape(value) + '</strong></div>'
                   for name, value in view['rows'])
    return ('<section class="sq-session-hours" aria-label="오늘의 미국장 시간">'
            '<div class="sq-session-heading"><strong>오늘의 미국장</strong><span>' + escape(view['kind']) + '</span></div>'
            '<p>미국 기준 ' + escape(view['day']) + ' · 아래 시간은 한국시간(KST)</p>'
            + ('<div class="sq-session-times">' + rows + '</div>' if rows else '')
            + '<p class="sq-session-note' + (' sq-session-warning' if view['warning'] else '') + '">'
            + escape(view['note']) + '</p></section>')

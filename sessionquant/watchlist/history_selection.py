"""Daily operating subset; minute coverage never grants trading eligibility."""
from datetime import timedelta
from functools import lru_cache

from ..models import utc
from ..market.history import HistoryStore, history_plan, session_quality
from ..scanners.relative_volume import NEW_YORK, _calendar

CONTROL = 'watchlist_history_selection_v1'


@lru_cache(maxsize=32)
def _sessions(day):
    end = day + timedelta(days=14)
    cal = _calendar(day.year, end.year)
    return tuple((str(s.date()), cal.session_open(s).to_pydatetime(),
                  cal.session_close(s).to_pydatetime())
                 for s in cal.sessions_in_range(str(day), str(end)))


def operating_session(now):
    now = utc(now)
    return next(row for row in _sessions(now.astimezone(NEW_YORK).date()) if now < row[2])


def inspect_history(store, symbols, now):
    """Read only. Recompute coverage from observations available at inspection time."""
    now = utc(now)
    day, opened, _ = operating_session(now)
    plan = history_plan(opened)
    items = []
    for symbol in dict.fromkeys(symbols):
        problems = []
        complete = 0
        for date, start, end in plan:
            try:
                row, bars = store.get(symbol, date)
                visible = [b for b in bars if b.received_at <= now]
                quality = session_quality(visible, start, end)
                invalid = any(not start < b.timestamp <= end or b.timestamp.second or b.timestamp.microsecond
                              for b in visible)
                if row and quality['complete'] and not invalid:
                    complete += 1
                    continue
                reasons = (['history_session_missing'] if not row else
                           ['history_minute_boundary_invalid'] if invalid else
                           ['history_minutes_missing_or_conflicting'])
                problems.append({'session':date, 'reasons':reasons, **quality})
            except (ValueError, TypeError, KeyError, AttributeError):
                problems.append({'session':date, 'reasons':['history_record_invalid']})
        items.append({'symbol':symbol, 'included':bool(plan) and not problems,
                      'complete_sessions':complete, 'required_sessions':len(plan),
                      'problems':problems})
    included = [r['symbol'] for r in items if r['included']]
    return {'session':day, 'session_open':opened.isoformat(), 'checked_at':now.isoformat(), 'known_at':now.isoformat(),
            'required_full_sessions':20, 'session_dates':[r[0] for r in plan],
            'base_symbols':list(dict.fromkeys(symbols)), 'included_symbols':included,
            'base_count':len(items), 'included_count':len(included),
            'excluded_count':len(items)-len(included), 'items':items,
            'coverage_only':True}


class DailyHistorySelection:
    def __init__(self, repo):
        self.repo = repo
        saved = repo.get_control(CONTROL, {}) or {}
        # A new process rechecks pre-open preparation; an active session remains frozen.
        self.frame = saved if saved.get('frozen') else {}

    def ensure(self, symbols, now):
        now = utc(now)
        day, opened, _ = operating_session(now)
        frozen = now >= opened
        frame = self.frame
        if (frame.get('session') != day or utc(frame['checked_at']) > now
                or (frozen and not frame.get('frozen'))
                or (not frame.get('frozen') and frame.get('base_symbols') != list(symbols))):
            # A fresh reader also sees backfills committed since the collector started.
            frame = inspect_history(HistoryStore(self.repo), symbols, now)
            frame['frozen'] = frozen
            self.frame = frame
            with self.repo.transaction():
                self.repo.set_control(CONTROL, frame)
                self.repo.record('pipeline_status', {'id':'watchlist-history:'+now.isoformat(),
                    'kind':'watchlist_history_selection', 'known_at':now.isoformat(), **frame})
        return frame

    def allowed(self, symbols, now):
        if not symbols:
            return []
        frame = self.ensure(symbols, now)
        included = set(frame['included_symbols'])
        # A cap/universe change can remove members but cannot add them intraday.
        return [s for s in symbols if s in included]

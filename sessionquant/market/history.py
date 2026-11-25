"""Persistent regular-session minute history. A complete cache is not volume evidence."""
from __future__ import annotations

from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from ..events import parse_event
from ..models import jsonable, utc, symbol_valid
from ..scanners.relative_volume import NEW_YORK, _calendar
from .bars import session_bounds
from ..performance import timed

KIND = 'market_history_sessions'
SOURCE = 'toss:/api/v1/candles:1m'
VIEW = 'watchlist_rvol_v1'


@lru_cache(maxsize=128)
def _plan(day, count):
    start = day - timedelta(days=max(150, count * 4))
    cal = _calendar(start.year, day.year)
    rows, full = [], 0
    for session in reversed(cal.sessions_in_range(str(start), str(day))):
        opened, closed = cal.session_open(session).to_pydatetime(), cal.session_close(session).to_pydatetime()
        rows.append((str(session.date()), opened, closed))
        full += int((closed-opened).total_seconds() >= 390*60)
        if full >= count:
            break
    return tuple(reversed(rows))


def history_plan(now, count=20, *, include_current=False):
    """20 full-length comparison sessions plus intervening short days; never skip gaps."""
    now = utc(now)
    day = now.astimezone(NEW_YORK).date()
    bounds = session_bounds(now)
    if not include_current or (bounds and now < bounds[1]):
        day -= timedelta(days=1)
    return _plan(day, count)


def record_id(symbol, day):
    symbol_valid(symbol)
    return f'toss:1m:raw:{symbol}:{day}'


def signature(bar):
    return (bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.source, bar.price_adjustment)


def session_quality(bars, opened, closed):
    versions = defaultdict(set)
    for bar in bars:
        if opened < bar.timestamp <= closed:
            versions[bar.timestamp].add(signature(bar)[1:])
    expected = {opened+timedelta(minutes=i) for i in range(1, int((closed-opened).total_seconds()/60)+1)}
    missing = expected - versions.keys()
    conflicts = sum(len(v) > 1 for v in versions.values())
    return {'expected_minutes': len(expected), 'received_minutes': len(expected & versions.keys()),
            'missing_minutes': len(missing), 'conflicting_minutes': conflicts,
            'complete': not missing and not conflicts}


def decode_history(symbol, row):
    bars = [parse_event({'kind':'bar','data':b}).data for b in row['bars']] if row else []
    if any(b.symbol != symbol or b.source != SOURCE or b.price_adjustment != 'raw'
           or b.received_at < b.timestamp for b in bars):
        raise ValueError('저장된 과거 분봉의 종목·출처·수신 시각이 다릅니다.')
    return bars


class HistoryStore:
    def __init__(self, repo):
        self.repo = repo
        self.cache = OrderedDict()
        self.seeded = {}

    def get(self, symbol, day):
        key = record_id(symbol, day)
        if key not in self.cache:
            row = self.repo.get_record(KIND, key)
            bars = decode_history(symbol,row)
            self.cache[key] = (row, bars)
        self.cache.move_to_end(key)
        while len(self.cache) > 512:
            self.cache.popitem(last=False)
        return self.cache[key]

    @timed('history_save')
    def save(self, symbol, day, bars, *, progress=None):
        old, previous = self.get(symbol, day)
        from .candle_revisions import grouped_minutes, retain_observation, ordered_observations, value_key
        groups = grouped_minutes(previous)
        changed = False
        for bar in ordered_observations(groups, bars):
            if (bar.symbol != symbol or bar.source != SOURCE or bar.price_adjustment != 'raw'
                    or bar.received_at < bar.timestamp or bar.timestamp.second or bar.timestamp.microsecond
                    or str(bar.timestamp.astimezone(NEW_YORK).date()) != day):
                raise ValueError('과거 분봉의 종목·거래일·출처·확정 시각을 확인하세요.')
            rows = groups[(bar.symbol, bar.timestamp)]
            if retain_observation(rows, bar):
                # An earlier receipt for an otherwise unchanged minute replaces
                # the ordinary duplicate, as before. Conflict evidence is kept.
                if rows and {value_key(b) for b in rows} == {value_key(bar)}:
                    rows.clear()
                rows.append(bar)
                changed = True
        values = sorted((b for rows in groups.values() for b in rows), key=lambda b:(b.timestamp,b.received_at))
        bounds = session_bounds(datetime.fromisoformat(day+'T12:00:00-05:00'))
        if not bounds:
            raise ValueError('거래일 달력을 확인할 수 없습니다.')
        if any(not bounds[0] < b.timestamp <= bounds[1] for b in values):
            raise ValueError('정규장 밖의 분봉은 비교 이력에 저장하지 않습니다.')
        if old is not None and not changed and (progress is None or progress == old.get('progress',{})):
            return old
        row = {'id':record_id(symbol,day), 'symbol':symbol, 'session':day, 'source':SOURCE,
               'price_adjustment':'raw', 'bars':jsonable(values),
               'quality':session_quality(values,*bounds),
               'progress':progress if progress is not None else (old or {}).get('progress',{}),
               'received_at':max((b.received_at for b in values),default=datetime.now(timezone.utc)).isoformat()}
        if old != row:
            self.repo.record(KIND,row)
        self.cache[row['id']] = (row,values)
        return row

    def ready(self, symbol, now):
        return all((self.get(symbol,day)[0] or {}).get('quality',{}).get('complete')
                   for day,_,_ in history_plan(now))

    def ingest(self, bars):
        grouped = defaultdict(list)
        for bar in bars:
            bounds = session_bounds(bar.timestamp)
            if bounds and bounds[0] < bar.timestamp <= bounds[1] and bar.source == SOURCE and bar.price_adjustment == 'raw':
                grouped[(bar.symbol,str(bar.timestamp.astimezone(NEW_YORK).date()))].append(bar)
        with self.repo.transaction():
            for (symbol,day), rows in grouped.items():
                self.save(symbol,day,rows)

    @timed('history_load')
    def load(self, symbol, now, *, record=False, include_current=False):
        days = [day for day,_,_ in history_plan(now)]
        today = str(utc(now).astimezone(NEW_YORK).date())
        if include_current:
            days.append(today)
        result = []
        for day in days:
            row,bars = self.get(symbol,day)
            visible = [b for b in bars if b.received_at <= now]
            result.extend(visible)
            if record and row and day != today:
                # Seed exactly the historical observations used in this recording run.
                # Original receipt times stay intact; availability in this run is now.
                from ..research.journal import append, CONTROL
                state = self.repo.get_control(CONTROL,{}) or {}
                digest = (state.get('run_id'),row['received_at'],len(bars),len(visible))
                if state.get('active') and self.seeded.get(row['id']) != digest:
                    if not visible:
                        continue
                    append(self.repo,KIND,{**row,'bars':jsonable(visible),
                        'received_at':max(b.received_at for b in visible).isoformat()},available_at=now)
                    self.seeded[row['id']] = digest
        # Bound memory across long-lived sessions and watchlist changes.
        cutoff = str((utc(now)-timedelta(days=100)).date())
        for key in list(self.cache):
            if key.rsplit(':',1)[-1] < cutoff:
                self.cache.pop(key,None); self.seeded.pop(key,None)
        return result

    def report(self, symbol, now):
        from .quality import rvol_report
        now = utc(now)
        bars = self.load(symbol,now,include_current=True)
        bounds = session_bounds(now)
        through = min(now.replace(second=0,microsecond=0),bounds[1]) if bounds and now > bounds[0] else None
        result = rvol_report(bars,symbol,through,now)
        result['symbol'] = symbol
        latest = max((b.timestamp for b in bars if b.timestamp <= now and b.received_at <= now),default=None)
        result['last_bar_at'] = latest.isoformat() if latest else None
        return result

    def publish(self, symbols, now, *, updated=()):
        previous = self.repo.get_control(VIEW,{}) or {}
        rows = {row['symbol']:row for row in previous.get('items',[]) if row['symbol'] in symbols}
        for symbol in updated:
            if symbol in symbols:
                rows[symbol] = self.report(symbol,now)
        self.repo.set_control(VIEW,{'observed_at':utc(now).isoformat(), 'scope':'watchlist',
            'symbols':list(symbols), 'items':[rows.get(s,{'symbol':s,'status':'UNKNOWN','calculated_value':None,
                'value':None,'reasons':['symbol_not_yet_collected']}) for s in symbols]})


def current_view(view, now=None):
    """Old per-symbol observations must not inherit the newest collector heartbeat."""
    from copy import deepcopy
    view = deepcopy(view or {})
    now = utc(now or datetime.now(timezone.utc))
    bounds = session_bounds(now)
    expected = min(now.replace(second=0,microsecond=0),bounds[1]) if bounds and now > bounds[0] else None
    for row in view.get('items',[]):
        try:
            cutoff, observed = utc(row.get('cutoff_at')), utc(row.get('observed_at'))
            valid = utc(row.get('calculated_valid_until'))
            fresh = bool(expected and cutoff == expected and cutoff <= observed <= now <= valid)
        except (ValueError, TypeError, OverflowError):
            fresh = False
        row['fresh'] = fresh
        if not fresh:
            row.update(value=None,calculated_value=None,status='UNKNOWN',calculation_status='UNKNOWN')
            row['reasons'] = list(dict.fromkeys([*row.get('reasons',[]),'rvol_observation_stale_or_incomplete']))
    return view

"""Small, sampled delay evidence. No raw candles, bodies or unbounded journal."""
from datetime import timedelta
import json
import logging
import math
import re

from ..models import utc
from .bars import session_bounds

CONTROL = 'candle_delay_incidents_v1'
INTERVAL = 300
MAX_EVENTS = 96
MAX_EVENT_BYTES = 3072
MAX_BYTES = 256 * 1024


def encoded(value):
    # Match the repository's JSON spacing so the storage bound includes it.
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def stamp(value):
    try:
        return utc(value).isoformat() if value is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def number(value):
    return round(value, 3) if type(value) in (int, float) and math.isfinite(value) and 0 <= value < 1e9 else None


def code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_:-]{1,64}', value) else None


class CandleIncidents:
    def __init__(self, repo):
        self.repo = repo
        self.loaded = False
        self.events = []
        self.next_at = None

    def observe(self, state, performance, now, *, priority=()):
        """Normal cycles do no extra DB work; failed recording never stops trading."""
        now = utc(now)
        if self.next_at is not None and now < self.next_at:
            return
        try:
            event = self._event(state, performance, now, priority)
            if event is None:
                return
            # Also throttle failures: diagnostics must not hammer an unhealthy DB.
            self.next_at = now + timedelta(seconds=INTERVAL)
            if not self.loaded:
                saved = self.repo.get_control(CONTROL, {}) or {}
                self.events = [e for e in saved.get('events', [])[-MAX_EVENTS:]
                    if isinstance(e, dict) and stamp(e.get('at')) and len(encoded(e)) <= MAX_EVENT_BYTES]
                last = stamp(saved.get('last_saved_at'))
                self.loaded = True
                if last and utc(last) + timedelta(seconds=INTERVAL) > now:
                    self.next_at = utc(last) + timedelta(seconds=INTERVAL)
                    return
            # One small control lookup only on a sampled anomaly, never per tick.
            frame = self.repo.get_control('portfolio_selection_v1', {}) or {}
            rows = {r.get('symbol'): r for r in frame.get('rows', []) if isinstance(r, dict)}
            for item in event['symbols']:
                row = rows.get(item['symbol'], {})
                item['decision_at'] = stamp(frame.get('evaluated_at')) if row else None
                item['decision_bar_at'] = stamp((row.get('indicators') or {}).get('last_closed_at'))
                item['blockers'] = [c for v in row.get('blockers', [])[:3] if (c := code(v))]
            while len(encoded(event)) > MAX_EVENT_BYTES and event['symbols']:
                event['symbols'].pop()
            events = [*self.events, event][-MAX_EVENTS:]
            saved = {'version': 1, 'last_saved_at': now.isoformat(), 'events': events}
            while len(encoded(saved)) > MAX_BYTES:
                events.pop(0)
            self.repo.set_control(CONTROL, saved)
            self.events = events
        except Exception:
            self.next_at = now + timedelta(seconds=INTERVAL)
            logging.getLogger(__name__).warning('분봉 지연 진단 저장 실패 · 5분 후 다시 시도합니다.')

    @staticmethod
    def _event(state, performance, now, priority):
        bounds = session_bounds(now)
        if not bounds or not bounds[0] + timedelta(minutes=1) <= now < bounds[1]:
            return None
        active = set(priority) | {r['ticker'] for r in state.get('focus', []) if isinstance(r, dict) and r.get('ticker')}
        active = {s for s in active if isinstance(s, str) and re.fullmatch(r'[A-Z0-9.-]{1,16}', s)}
        refresh = state.get('candle_refresh_v1', {})
        reads = [r for r in state.get('read_measurements', [])
                 if r.get('symbol') in active and r.get('source') in {'toss_candles','toss_history_backfill','toss_startup_backfill'}]
        delayed = set()
        for symbol in active:
            item = refresh.get(symbol, {})
            target, latest = stamp(item.get('target_at')), stamp(item.get('latest_closed_at'))
            if target and bounds[0] < utc(target) <= now and (
                (latest and bounds[0] < utc(latest) <= now and (now-utc(latest)).total_seconds() > 90)
                or (item.get('status') == 'AWAITING_PUBLICATION' and (now-utc(target)).total_seconds() >= 30)):
                delayed.add(symbol)
        failed = {r['symbol'] for r in reads if r.get('status') != 'ok'}
        reasons = []
        if delayed:
            reasons.append('CANDLE_LATE')
        if failed:
            reasons.append('CANDLE_REQUEST_DELAY')
        cycle = number(performance.get('elapsed_seconds'))
        if active and cycle is not None and cycle >= 30:
            reasons.append('SLOW_CYCLE')
        if not reasons:
            return None
        targets = sorted(delayed | failed) or sorted(active)
        symbols = []
        for symbol in targets[:3]:
            item = refresh.get(symbol, {})
            read = next((r for r in reversed(reads) if r.get('symbol') == symbol), {})
            symbols.append({'symbol': symbol, 'target_at': stamp(item.get('target_at')),
                'latest_closed_at': stamp(item.get('latest_closed_at')), 'received_at': stamp(item.get('received_at')),
                'attempts': number(item.get('attempts')), 'request_started_at': stamp(read.get('started_at')),
                'request_finished_at': stamp(read.get('observed_at')), 'request_seconds': number(read.get('duration_seconds')),
                'request_status': code(read.get('status'))})
        phases = {r.get('phase'): number(r.get('seconds')) for r in performance.get('phases', [])}
        return {'at': now.isoformat(), 'reasons': reasons, 'cycle_seconds': cycle,
            'calls': number(state.get('last_cycle_calls')), 'call_budget': number(state.get('cycle_call_budget')),
            'affected_count': len(targets), 'symbols': symbols,
            'phases': {k: phases.get(k) for k in ('api_http','api_pacing','api_retry_wait','db_query','db_commit',
                'history_save','engine_batch','indicators')},
            'history_persistence_seconds': number(state.get('history_persistence_seconds')),
            'candidate_delivery_seconds': number(state.get('candidate_delivery_seconds'))}

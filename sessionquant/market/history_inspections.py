"""Persistent coverage checks, bound to exact stored content and exchange bounds."""
from datetime import timedelta

from ..models import utc
from .history import KIND, record_id, decode_history, session_quality

CONTROL = 'completed_history_inspections_v1'
# Bump whenever coverage/source/availability validation rules change.
VERSION = 1
INCOMPLETE_RETRY_DELAY = timedelta(hours=6)


class HistoryInspections:
    def __init__(self, repo):
        self.repo = repo
        saved = repo.get_control(CONTROL,{}) or {}
        self.entries = (dict(saved['entries']) if isinstance(saved,dict) and saved.get('version') == VERSION
                        and isinstance(saved.get('entries'),dict) else {})
        self.deferred_entries = (dict(saved['deferred_entries']) if isinstance(saved,dict)
                                 and saved.get('version') == VERSION
                                 and isinstance(saved.get('deferred_entries'),dict) else {})
        self.last_incomplete_checks = {}
        self.fingerprints = {}
        self.dirty = False

    def refresh(self, symbols, plan):
        ids = [record_id(symbol,day) for day,_,_ in plan for symbol in symbols]
        self.fingerprints = self.repo.record_fingerprints(KIND,ids)

    def reusable(self, symbol, day, opened, closed, now):
        key = record_id(symbol,day)
        entry = self.entries.get(key)
        fingerprint = self.fingerprints.get(key,{}).get('fingerprint')
        try:
            expected = int((closed-opened).total_seconds()//60)
            quality = entry['quality']
            if (fingerprint and entry['fingerprint'] == fingerprint
                    and entry['opened'] == opened.isoformat() and entry['closed'] == closed.isoformat()
                    and closed <= utc(entry['checked_at']) <= utc(now)
                    and quality == {'expected_minutes':expected,'received_minutes':expected,
                                    'missing_minutes':0,'conflicting_minutes':0,'complete':True}):
                return dict(quality)
        except (KeyError,TypeError,ValueError,AttributeError):
            pass
        return None

    def deferred(self, symbol, day, opened, closed, now):
        """Recently exhausted history stays incomplete; this only defers API work."""
        key = record_id(symbol,day)
        entry = self.deferred_entries.get(key)
        fingerprint = self.fingerprints.get(key,{}).get('fingerprint')
        try:
            quality = entry['quality']
            checked = utc(entry['checked_at'])
            if (fingerprint and entry['fingerprint'] == fingerprint
                    and entry['opened'] == opened.isoformat() and entry['closed'] == closed.isoformat()
                    and closed <= checked <= utc(now) < utc(entry['retry_after'])
                    and utc(entry['retry_after']) <= checked+INCOMPLETE_RETRY_DELAY
                    and quality.get('complete') is False
                    and entry['reason'] == 'history_minutes_missing_or_conflicting'):
                return {**entry,'quality':dict(quality)}
        except (KeyError,TypeError,ValueError,AttributeError):
            pass
        return None

    def defer_retry(self, symbol, day):
        # Only the last atomic payload/digest inspection can be reused. Callers
        # invoke this after reaching the end of a successful backfill, never on
        # HTTP errors, budget waits, stalled cursors or a page-limit cutoff.
        key = record_id(symbol,day)
        entry = self.last_incomplete_checks.get(key)
        if entry is None:
            return
        self.deferred_entries[key] = {**entry,
            'retry_after':(utc(entry['checked_at'])+INCOMPLETE_RETRY_DELAY).isoformat()}
        self.dirty = True

    def inspect(self, store, symbol, day, opened, closed, now):
        now = utc(now)
        reused = self.reusable(symbol,day,opened,closed,now)
        if reused is not None:
            return reused, None
        deferred = self.deferred(symbol,day,opened,closed,now)
        if deferred is not None:
            return deferred['quality'], deferred['reason']
        key = record_id(symbol,day)
        # Read payload and digest in the same SELECT. Never sign a different row
        # if another writer changed it between a quality check and hashing.
        found = self.repo.record_fingerprints(KIND,[key],include_payload=True).get(key,{})
        row = found.get('payload')
        if key in self.entries:
            self.entries.pop(key)
            self.dirty = True
        if key in self.deferred_entries:
            self.deferred_entries.pop(key)
            self.dirty = True
        self.last_incomplete_checks.pop(key,None)
        self.fingerprints[key] = {'fingerprint':found.get('fingerprint')}
        bars = decode_history(symbol,row)
        store.cache.pop(key,None)
        store.cache[key] = (row,bars)
        while len(store.cache) > 512:
            store.cache.popitem(last=False)
        visible = [bar for bar in bars if bar.received_at <= now]
        quality = session_quality(visible,opened,closed)
        invalid = any(not opened < bar.timestamp <= closed or bar.timestamp.second or bar.timestamp.microsecond
                      for bar in visible)
        reason = ('history_session_missing' if not row else 'history_minute_boundary_invalid' if invalid
                  else 'history_minutes_missing_or_conflicting' if not quality['complete'] else None)
        if invalid:
            quality['complete'] = False
        if reason is None and closed <= now and all(bar.received_at <= now for bar in bars):
            self.entries[key] = {'fingerprint':found['fingerprint'],'opened':opened.isoformat(),
                'closed':closed.isoformat(),'checked_at':now.isoformat(),'quality':quality}
            self.dirty = True
        elif (row and reason == 'history_minutes_missing_or_conflicting'
              and closed <= now and all(bar.received_at <= now for bar in bars)):
            self.last_incomplete_checks[key] = {'fingerprint':found['fingerprint'],
                'opened':opened.isoformat(),'closed':closed.isoformat(),'checked_at':now.isoformat(),
                'quality':dict(quality),'reason':reason}
        return quality, reason

    def flush(self, now):
        if not self.dirty:
            return
        cutoff = str((utc(now)-timedelta(days=90)).date())
        entries = {key:value for key,value in self.entries.items() if key.rsplit(':',1)[-1] >= cutoff}
        # Bound cache size across changing watchlists; eviction only causes rechecking.
        self.entries = dict(sorted(entries.items(),key=lambda row:row[0].rsplit(':',1)[-1])[-5000:])
        deferred = {key:value for key,value in self.deferred_entries.items()
                    if key.rsplit(':',1)[-1] >= cutoff}
        self.deferred_entries = dict(sorted(deferred.items(),key=lambda row:row[0].rsplit(':',1)[-1])[-5000:])
        self.repo.set_control(CONTROL,{'version':VERSION,'entries':self.entries,
                                     'deferred_entries':self.deferred_entries})
        self.dirty = False

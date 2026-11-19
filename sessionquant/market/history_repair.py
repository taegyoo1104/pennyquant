"""Offline reconciliation of closed sessions; never pick a conflicting value by arrival order."""
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from ..models import jsonable, utc
from .history import KIND, SOURCE, signature, session_quality, record_id


async def _snapshot(client, budget, symbol, opened, closed):
    cursor, bars = closed, []
    for _ in range(10):
        budget.take()
        fetched, following = await client.candles(symbol, count=200, before=cursor)
        if any(b.timestamp > cursor for b in fetched):
            return None, 'page_above_cursor'
        selected = [b for b in fetched if opened < b.timestamp <= closed]
        if any(b.symbol != symbol or b.source != SOURCE or b.price_adjustment != 'raw'
               or b.timestamp.second or b.timestamp.microsecond or b.received_at < b.timestamp
               for b in selected):
            return None, 'invalid_bar'
        bars.extend(selected)
        # Validate pagination before accepting even an otherwise complete page.
        if following and utc(following) >= cursor:
            return None, 'cursor_stalled'
        if session_quality(bars, opened, closed)['complete']:
            return bars, None
        if not fetched or not following or min(b.timestamp for b in fetched) <= opened:
            return None, 'incomplete_snapshot'
        cursor = utc(following)
    return None, 'page_limit'


async def repair_conflict(store, symbol, day, opened, closed, client, budget, *, now):
    """Caller holds the engine lease. Two fresh complete reads must agree exactly."""
    old, _ = store.get(symbol, day)
    result = {'symbol':symbol, 'session':day, 'status':'UNRESOLVED'}
    if closed >= utc(now):
        return {**result, 'reason':'session_not_closed'}
    if not old or not old['quality']['conflicting_minutes']:
        return {**result, 'status':'SKIPPED', 'reason':'no_conflict'}
    first, reason = await _snapshot(client, budget, symbol, opened, closed)
    if reason:
        return {**result, 'reason':reason}
    second, reason = await _snapshot(client, budget, symbol, opened, closed)
    if reason:
        return {**result, 'reason':reason}
    if {signature(b) for b in first} != {signature(b) for b in second}:
        return {**result, 'reason':'snapshots_disagree'}
    # Reconciled data was not known when the old conflicting observations arrived.
    known = max(datetime.now(timezone.utc), utc(now), *(b.received_at for b in first+second))
    bars = sorted({b.timestamp:replace(b, received_at=known) for b in second}.values(), key=lambda b:b.timestamp)
    audit_id = 'history_repair_v1:'+str(uuid4())
    row = {**old, 'bars':jsonable(bars), 'quality':session_quality(bars, opened, closed),
           'received_at':known.isoformat(), 'progress':{'status':'RECONCILED', 'cursor':None},
           'repair':{'audit_control':audit_id, 'verified_at':known.isoformat(), 'matching_reads':2}}
    # Retain the entire previous payload, atomically with the replacement. No schema change.
    with store.repo.transaction():
        if store.repo.get_record(KIND, record_id(symbol, day)) != old:
            return {**result, 'reason':'history_changed_during_repair'}
        store.repo.set_control(audit_id, {'before':old, 'after':row, 'policy':'two_complete_matching_reads_v1'})
        store.repo.record(KIND, row)
    store.cache.pop(record_id(symbol, day), None)
    return {**result, 'status':'REPAIRED', 'verified_at':known.isoformat(), 'audit_control':audit_id}

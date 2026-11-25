"""Resolve raw Toss minute revisions from repeated, time-ordered observations.

Original versions remain stored. A resolution is visible only after its second
confirmation; this is provider consistency evidence, not exchange finality.
"""
from collections import defaultdict
from datetime import timedelta

SOURCE = 'toss:/api/v1/candles:1m'
SETTLE_SECONDS = 60
CONFIRM_SECONDS = 5


def value_key(bar):
    return (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.source, bar.price_adjustment)


def selected_version(rows, now):
    visible = [b for b in rows if b.timestamp <= b.received_at <= now]
    if not visible:
        return None
    values = {value_key(b) for b in visible}
    if len(values) == 1:
        return min(visible, key=lambda b: b.received_at)
    if any(b.source != SOURCE or b.price_adjustment != 'raw' for b in visible):
        return None
    latest_at = max(b.received_at for b in visible)
    latest = [b for b in visible if b.received_at == latest_at]
    if len({value_key(b) for b in latest}) != 1:
        return None
    candidate = latest[0]
    different_at = max(b.received_at for b in visible if value_key(b) != value_key(candidate))
    confirmations = {b.received_at for b in visible if value_key(b) == value_key(candidate)
        and b.received_at > different_at
        and b.received_at >= b.timestamp + timedelta(seconds=SETTLE_SECONDS)}
    if len(confirmations) >= 2 and (max(confirmations)-min(confirmations)).total_seconds() >= CONFIRM_SECONDS:
        return candidate
    return None


def retain_observation(rows, bar):
    """Normal duplicates cost no storage; retain bounded evidence for conflicts."""
    same = [b for b in rows if value_key(b) == value_key(bar)]
    if not same:
        return True
    if any(b.received_at == bar.received_at for b in same):
        return False
    if bar.received_at < min(b.received_at for b in same):
        return True
    if len({value_key(b) for b in rows}) < 2 or bar.source != SOURCE or bar.price_adjustment != 'raw':
        return False
    last = max(rows, key=lambda b: b.received_at)
    if bar.received_at < last.received_at:
        return False
    if value_key(last) != value_key(bar):
        return True
    if selected_version(rows, bar.received_at) is not None:
        return False
    return (bar.received_at-last.received_at).total_seconds() >= CONFIRM_SECONDS


def grouped_minutes(bars):
    groups = defaultdict(list)
    for bar in bars:
        groups[(bar.symbol, bar.timestamp)].append(bar)
    return groups


def ordered_observations(groups, bars):
    """Keep contradictory rows from the SAME response; they are not confirmations.

Forward changed values first so a formerly duplicate value at the same receipt
is retained as contradictory evidence by downstream ingestion too.
"""
    last = {key:value_key(max(rows,key=lambda b:b.received_at)) for key,rows in groups.items() if rows}
    return sorted(bars, key=lambda b:(b.received_at,b.symbol,b.timestamp,
                                      value_key(b)==last.get((b.symbol,b.timestamp))))


def conflict_details(bars, now):
    conflicts = []
    for (_, stamp), rows in grouped_minutes(b for b in bars if b.timestamp <= b.received_at <= now).items():
        if len({value_key(b) for b in rows}) > 1 and selected_version(rows, now) is None:
            conflicts.append(stamp)
    return sorted(conflicts)

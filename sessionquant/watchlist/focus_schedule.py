"""Opening-bell focus slots; membership is never permission to place an order."""
from datetime import timedelta

from ..market.bars import session_bounds
from ..models import utc


def choose_scheduled(manager, metrics, now):
    from .selection import FocusSelector
    state, config = manager.state, manager.config
    bounds = session_bounds(now)
    if bounds is None:
        state['focus'] = []
        state['focus_schedule'] = {'status':'NO_SESSION','next_at':None}
        return False
    opened, closed = bounds
    session = opened.isoformat()
    schedule = state.get('focus_schedule',{})
    if schedule.get('session') != session:
        state['focus'] = []
        schedule = {'session':session}
    previous = state['focus']
    step = timedelta(minutes=config.focus_rebalance_minutes)
    preopen = now < opened
    boundary = opened if preopen else min(opened + ((now-opened)//step)*step,closed)
    slot = session+':preopen' if preopen else boundary.isoformat()
    next_at = opened if preopen else min(boundary+step,closed)
    # A restart can expire every member while today's preopen slot stays marked
    # selected. Retry an empty preparation list using fresh inputs; intraday
    # membership still waits for the next opening-bell boundary.
    due = now < closed and (schedule.get('slot') != slot or not schedule.get('selected_once')
                           or (preopen and not previous))
    protected, pending = set(), False
    if due and not preopen and manager.pattern_provider:
        five = now.replace(second=0,microsecond=0)-timedelta(minutes=now.minute%5)
        fifteen = now.replace(second=0,microsecond=0)-timedelta(minutes=now.minute%15)
        for row in previous:
            p = manager.pattern_provider(row['ticker'],now)
            if p.get('stage') not in {'CORRECTION','REBOUND'}:continue
            try:
                f, u = utc(p['five_at']), utc(p['fifteen_at'])
            except (KeyError,ValueError,TypeError,AttributeError):continue
            if f == five and u == fifteen:
                protected.add(row['ticker'])
            elif (now < boundary+timedelta(seconds=config.focus_refresh_grace_seconds)
                    and five-timedelta(minutes=5) <= f <= five
                    and fifteen-timedelta(minutes=15) <= u <= fifteen):
                # The boundary candle can arrive after the timer. Wait briefly,
                # rather than evicting a correction before evaluating that candle.
                pending = True
    select = due and not pending
    selector = FocusSelector(config.focus_size,config.watchlist_data_max_age_seconds,
        0 if select else 86400,config.focus_refresh_grace_seconds,config.focus_selection)
    inputs = metrics if select else [r for r in metrics if r['ticker'] in {p['ticker'] for p in previous}]
    chosen = selector.choose(inputs,previous,now,pinned_symbols=protected if select else ())
    old = {r['ticker']:r for r in previous}
    for row in chosen:
        row['pattern_retained'] = row['ticker'] in protected if select else old[row['ticker']].get('pattern_retained',False)
        if row['pattern_retained']:
            row['selection_reason'] = '재선정 시 유효한 조정·반등 패턴 유지 · 주문 조건은 별도 검사'
    state['focus'] = chosen
    if select:
        schedule.update(slot=slot,selected_once=bool(chosen),selected_at=now.isoformat())
    schedule.update(status='CLOSED' if now>=closed else 'BOUNDARY_DATA_WAIT' if pending else 'PREOPEN' if preopen else 'ACTIVE',
        next_at=None if now>=closed else next_at.isoformat(),interval_minutes=config.focus_rebalance_minutes,
        pending_boundary_at=boundary.isoformat() if pending else None,
        pending_until=(boundary+timedelta(seconds=config.focus_refresh_grace_seconds)).isoformat() if pending else None)
    state['focus_schedule'] = schedule
    return select

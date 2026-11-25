from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sessionquant.models import Bar
from sessionquant.market.bars import available_minutes, indicators, session_bounds
from sessionquant.market.candle_revisions import SOURCE, selected_version
from sessionquant.market.engine import MarketEngine
from sessionquant.market.history import HistoryStore
from sessionquant.market.quality import bar_quality
from sessionquant.market.simple_pullback import progress
from sessionquant.market.source import TossSessionSource
from sessionquant.database.repository import Repository

END = datetime(2026, 11, 24, 16, 15, tzinfo=timezone.utc)


def original():
    t = END-timedelta(minutes=1)
    return Bar('TSLA',t,t+timedelta(seconds=2),100,102,99,101,100,source=SOURCE,price_adjustment='raw')


def observations():
    a = original()
    b = replace(a,volume=101,received_at=END+timedelta(seconds=5))
    c = replace(b,received_at=END+timedelta(seconds=10))
    return a,b,c


def test_resolution_is_as_of_second_confirmation_and_preserves_originals():
    a,b,c = observations()
    rows = [a,b,c]
    assert selected_version(rows,a.received_at) == a
    assert selected_version(rows,b.received_at) is None
    assert selected_version(rows,c.received_at-timedelta(microseconds=1)) is None
    assert selected_version(rows,c.received_at) == c
    assert rows == [a,b,c]
    d = replace(c,close=100,received_at=c.received_at+timedelta(seconds=10))
    assert selected_version([*rows,d],d.received_at) is None


@pytest.mark.parametrize('fault',['same_receipt','too_close','unsettled','other_source','adjusted','ambiguous_response'])
def test_unverified_repeats_never_resolve(fault):
    a,b,c = observations()
    if fault == 'same_receipt': c=replace(c,received_at=b.received_at)
    if fault == 'too_close': c=replace(c,received_at=b.received_at+timedelta(seconds=4))
    if fault == 'unsettled':
        b=replace(b,received_at=a.timestamp+timedelta(seconds=10))
        c=replace(c,received_at=a.timestamp+timedelta(seconds=20))
    if fault == 'other_source': b=replace(b,source='OTHER')
    if fault == 'adjusted': b=replace(b,price_adjustment='adjusted')
    rows=[a,b,c]
    if fault == 'ambiguous_response': rows.append(replace(a,received_at=c.received_at))
    assert selected_version(rows,END+timedelta(minutes=1)) is None


def test_source_engine_restart_and_history_recover_without_growing_normal_duplicates(tmp_path):
    source=TossSessionSource(client=object())
    engine=MarketEngine(None,strategy='pullback_simple')
    opened,_=session_bounds(END)
    rows=[Bar('TSLA',opened+timedelta(minutes=i),opened+timedelta(minutes=i,seconds=2),
              100,102,99,101,100,source=SOURCE,price_adjustment='raw')
          for i in range(1,int((END-opened).total_seconds()/60)+1)]
    for b in source._new_bar_observations('TSLA',rows): engine.on_bar(b)
    a,b,c=observations()
    for x in source._new_bar_observations('TSLA',[b]): engine.on_bar(x)
    before=indicators(engine.bars['TSLA'],b.received_at)
    assert before.five[-1].timestamp == END-timedelta(minutes=5)
    assert bar_quality(engine.bars['TSLA'],'TSLA',b.received_at)['conflicting_minutes']==1
    restored=MarketEngine(None,strategy='pullback_simple')
    restored.restore(engine.to_state())
    source=TossSessionSource(client=object())
    source.restore_seen(restored.bars)
    # Cached/original observations cannot masquerade as new confirmations.
    assert source._new_bar_observations('TSLA',[b,b])==[]
    for x in source._new_bar_observations('TSLA',[c]): restored.on_bar(x)
    after=indicators(restored.bars['TSLA'],c.received_at)
    assert after.five[-1].timestamp == END and after.fifteen[-1].timestamp == END
    assert after.atr14 is not None and progress(after)['stage']!='DATA_WAIT'
    assert bar_quality(restored.bars['TSLA'],'TSLA',c.received_at)['conflicting_minutes']==0
    assert len(available_minutes(restored.bars['TSLA'],c.received_at,'TSLA'))==len(rows)
    for i in range(1,101):
        assert source._new_bar_observations('TSLA',[replace(c,received_at=c.received_at+timedelta(seconds=i*5))])==[]
    repo=Repository(tmp_path/'history.db',backend='sqlite')
    try:
        store=HistoryStore(repo)
        store.ingest(rows+[b])
        store.ingest([c])
        stored=HistoryStore(repo).get('TSLA','2026-11-24')[1]
        assert len(stored)==len(rows)+2
        assert indicators(stored,b.received_at).five[-1].timestamp==END-timedelta(minutes=5)
        assert indicators(stored,c.received_at).five[-1].timestamp==END
        store.ingest([replace(c,received_at=c.received_at+timedelta(seconds=60))])
        assert len(store.get('TSLA','2026-11-24')[1])==len(stored)
        assert not repo.all('orders')
    finally:
        repo.close()


def test_conflict_only_incident_is_saved_and_throttled():
    from test_candle_incidents import Repo, inputs, NOW
    from sessionquant.market.candle_incidents import CandleIncidents, CONTROL
    state,perf=inputs(late=False)
    state['candle_conflicts_v1']={'AAPL':{'count':1,'minutes':[NOW.replace(second=0).isoformat()]}}
    repo=Repo(); recorder=CandleIncidents(repo)
    recorder.observe(state,perf,NOW)
    event=repo.saved[CONTROL]['events'][0]
    assert 'CANDLE_CONFLICT' in event['reasons']
    assert event['symbols'][0]['conflicting_minutes']==1
    recorder.observe(state,perf,NOW+timedelta(seconds=5))
    assert len(repo.writes)==1


@pytest.mark.parametrize('reverse',[False,True])
def test_mixed_response_cannot_count_as_confirmation(reverse, tmp_path):
    a,b,c=observations()
    source=TossSessionSource(client=object())
    engine=MarketEngine(None)
    repo=Repository(tmp_path/'mixed.db',backend='sqlite')
    store=HistoryStore(repo)
    try:
        for item in (a,b,c):
            store.ingest([item])
            for accepted in source._new_bar_observations('TSLA',[item]): engine.on_bar(accepted)
        t=c.received_at+timedelta(seconds=10)
        mixed=[replace(c,received_at=t),replace(a,received_at=t)]
        if reverse: mixed.reverse()
        store.ingest(mixed)
        for accepted in source._new_bar_observations('TSLA',mixed): engine.on_bar(accepted)
        assert selected_version(engine.bars['TSLA'],t) is None
        assert selected_version(store.get('TSLA','2026-11-24')[1],t) is None
        retry=replace(a,received_at=t+timedelta(seconds=10))
        for accepted in source._new_bar_observations('TSLA',[retry]): engine.on_bar(accepted)
        # The ambiguous response cannot serve as the first matching response.
        assert selected_version(engine.bars['TSLA'],retry.received_at) is None
        assert a.timestamp not in source._usable_seen_minutes('TSLA',retry.received_at)
    finally:
        repo.close()


def test_card_explains_current_conflict_instead_of_generic_wait():
    from test_home_cards import snapshots, NOW
    from sessionquant.dashboard.home_cards import focus_cards
    state,assessment=snapshots()
    row=assessment['rows'][0]
    row['bar_conflicts']=1
    row['progress']['five_at']=(NOW-timedelta(minutes=6)).isoformat()
    card=focus_cards(state,assessment,now=NOW)[0]
    assert card['title']=='분봉 값 충돌' and not card['stage_fresh']
    row['evaluated_at']='invalid'
    assert focus_cards(state,assessment,now=NOW)[0]['title']!='분봉 값 충돌'

"""Performance changes must preserve money, receipt times, corrections and pacing."""
import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
import json

import httpx
import pytest

from sessionquant.database.serialization import dumps
from sessionquant.market.history import HistoryStore, history_plan
from sessionquant.performance import ACTIVE, capture, measure
from test_watchlist_history import NOW, minute_rows
from watchlist_fixtures import context


def test_nested_money_dataclasses_keep_exact_precision_and_do_not_mutate_input():
    @dataclass(frozen=True)
    class Entry:
        amount: Decimal
        at: object
        detail: dict
    data = Entry(Decimal('0.123456789012345678901234567890'),NOW,{'수량':(Decimal('0.00000001'),)})
    assert json.loads(dumps({7:[data]})) == {'7':[{'amount':str(data.amount),'at':NOW.isoformat(),
        'detail':{'수량':['1E-8']}}]}
    assert data.detail['수량']==(Decimal('0.00000001'),)
    for bad in (Decimal('NaN'),Decimal('Infinity'),float('nan')):
        with pytest.raises(ValueError):dumps({'nested':[{'money':bad}]})


def test_unchanged_history_avoids_writes_but_progress_earlier_receipts_and_conflicts_persist(tmp_path):
    repo,*_=context(tmp_path)
    try:
        store=HistoryStore(repo)
        day,opened,_=history_plan(NOW)[-1]
        original=minute_rows('AAPL',opened,count=2)
        store.save('AAPL',day,original)
        changes=repo.db.total_changes
        store.save('AAPL',day,[replace(b,received_at=NOW+timedelta(seconds=10)) for b in original])
        assert repo.db.total_changes==changes
        with pytest.raises(ValueError):store.save('AAPL',day,[replace(original[0],symbol='MSFT')])
        store.save('AAPL',day,original,progress={'cursor':'next'})
        assert repo.db.total_changes>changes
        early=replace(original[0],received_at=NOW-timedelta(seconds=5))
        store.save('AAPL',day,[early])
        row,bars=store.get('AAPL',day)
        assert bars[0].received_at==early.received_at and row['progress']=={'cursor':'next'}
        store.save('AAPL',day,[replace(original[0],volume=999)])
        row,bars=HistoryStore(repo).get('AAPL',day)
        assert row['quality']['conflicting_minutes']==1 and len(bars)==3
    finally:repo.close()


def test_indicator_single_scan_still_excludes_late_corrections(monkeypatch):
    import sessionquant.market.bars as module
    from test_session_market import minutes, NOW as end
    rows=minutes()
    correction=replace(rows[-1],close=rows[-1].close+.01,received_at=end+timedelta(seconds=1))
    rows.append(correction)
    real=module.available_minutes
    calls=[]
    def checked(*args,**kwargs):
        calls.append(True)
        return real(*args,**kwargs)
    monkeypatch.setattr(module,'available_minutes',checked)
    before=module.indicators(rows,end)
    assert len(calls)==1 and len(before.five)==15 and len(before.fifteen)==5
    after=module.indicators(rows,end+timedelta(seconds=1))
    assert len(calls)==2 and len(after.five)==14 and len(after.fifteen)==4
    assert before.calculated_at != after.calculated_at


async def test_timing_scopes_are_task_local_and_reset_on_cancellation():
    async def collect(name):
        with capture() as timings:
            with measure(name):await asyncio.sleep(0)
            return timings.snapshot()
    a,b=await asyncio.gather(collect('api_http'),collect('db_query'))
    assert [r['phase'] for r in a['phases']]==['api_http']
    assert [r['phase'] for r in b['phases']]==['db_query']
    with pytest.raises(asyncio.CancelledError):
        with capture():
            with measure('api_http'):raise asyncio.CancelledError()
    assert ACTIVE.get() is None


async def test_http_pacing_and_retry_wait_are_measured_without_credentials(monkeypatch):
    from sessionquant.collectors import common
    import sessionquant.performance as perf
    elapsed=[0.0];attempts=[]
    monkeypatch.setattr(perf,'perf_counter',lambda:elapsed[0])
    class Limiter:
        async def wait(self):elapsed[0]+=3
    def handler(request):
        attempts.append(request.method);elapsed[0]+=2
        return httpx.Response(503 if len(attempts)==1 else 200)
    async def pause(seconds):elapsed[0]+=seconds
    monkeypatch.setattr(common.asyncio,'sleep',pause)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with capture() as timings:
            await common.get_readonly(client,'https://fake.test?token=PRIVATE',limiter=Limiter())
    rows={r['phase']:r for r in timings.snapshot()['phases']}
    assert rows['api_pacing']['seconds']==6 and rows['api_http']['seconds']==4
    assert rows['api_retry_wait']['seconds']==.25 and attempts==['GET','GET']
    assert 'PRIVATE' not in str(rows)

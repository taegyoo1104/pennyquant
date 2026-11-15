"""Completed history survives restart; content changes always invalidate coverage."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.history import HistoryStore, KIND, history_plan, record_id
from sessionquant.market.history_inspections import HistoryInspections, CONTROL, VERSION
from sessionquant.market.warmup import StartupWarmup
from sessionquant.trading_controls import begin_session
from sessionquant.watchlist.history_selection import DailyHistorySelection
from test_history_selection import prepare
from test_startup_warmup import Provider, finish
from test_watchlist_history import minute_rows

SUNDAY = datetime(2026,11,15,12,tzinfo=timezone.utc)


def worker(repo, symbols=('AAPL',), now=SUNDAY):
    clock=SimpleNamespace(now=now)
    control=begin_session(repo,mode='paper',now=now)
    selection=DailyHistorySelection(repo,startup_pending=True)
    result=StartupWarmup(repo,HistoryStore(repo),selection,control,clock=lambda:clock.now)
    manager=SimpleNamespace(allow_synthetic=False,base_symbols=lambda _:list(symbols),
        store=SimpleNamespace(status=lambda *args,**kwargs:{'status':'VALID'}))
    return result,manager,Provider(clock)


async def test_restart_skips_payload_parsing_in_warmup_and_final_selection(tmp_path,monkeypatch):
    path=tmp_path/'cache.db'
    repo=Repository(path,backend='sqlite')
    for symbol in ('AAPL','MSFT','NVDA'):
        prepare(HistoryStore(repo),symbol,SUNDAY)
    first,manager,provider=worker(repo,('AAPL','MSFT','NVDA'))
    await finish(first,manager,provider)
    assert first.report['required_tasks']==60 and not provider.calls
    repo.close()
    repo=Repository(path,backend='sqlite')
    try:
        def no_decode(*_):pytest.fail('unchanged completed history must not be parsed again')
        monkeypatch.setattr('sessionquant.market.history_inspections.decode_history',no_decode)
        second,manager,provider=worker(repo,('AAPL','MSFT','NVDA'))
        await finish(second,manager,provider)
        assert second.report['reused_history_tasks']==60
        assert second.report['required_tasks']==second.report['completed_tasks']==0
        assert second.selection.allowed(['AAPL','MSFT','NVDA'],SUNDAY)==['AAPL','MSFT','NVDA']
        assert second.report['status']=='READY' and not provider.calls
    finally:repo.close()


@pytest.mark.parametrize('change',['missing','conflict','deleted'])
async def test_direct_db_changes_invalidate_only_changed_day(tmp_path,change):
    repo=Repository(tmp_path/'cache.db',backend='sqlite')
    try:
        prepare(HistoryStore(repo),'AAPL',SUNDAY)
        first,manager,provider=worker(repo)
        await finish(first,manager,provider)
        day,opened,_=history_plan(SUNDAY)[-1]
        key=record_id('AAPL',day)
        row=repo.get_record(KIND,key)
        if change=='missing':row['bars'].pop(10)
        if change=='conflict':row['bars'].append({**row['bars'][10],'volume':999})
        # Bypass HistoryStore and Repository mutations, preserving stored quality flags.
        if change=='deleted':repo.db.execute('DELETE FROM market_history_sessions WHERE record_id=?',(key,))
        else:repo.db.execute('UPDATE market_history_sessions SET payload=? WHERE record_id=?',(json.dumps(row),key))
        repo.db.commit()
        second,manager,provider=worker(repo)
        await finish(second,manager,provider)
        assert second.report['reused_history_tasks']==19 and second.report['required_tasks']==1
        assert second.report['status']==('PARTIAL' if change=='conflict' else 'READY')
        if change=='missing':assert provider.calls==[('AAPL',opened+timedelta(minutes=11))]
        if change=='conflict':assert not provider.calls and second.selection.allowed(['AAPL'],SUNDAY)==[]
    finally:repo.close()


async def test_next_trading_day_checks_only_new_comparison_day(tmp_path):
    repo=Repository(tmp_path/'cache.db',backend='sqlite')
    try:
        prepare(HistoryStore(repo),'AAPL',SUNDAY)
        first,manager,provider=worker(repo)
        await finish(first,manager,provider)
        tuesday=datetime(2026,11,17,14,tzinfo=timezone.utc)
        second,manager,provider=worker(repo,now=tuesday)
        await finish(second,manager,provider)
        assert second.report['reused_history_tasks']==19 and second.report['required_tasks']==1
        assert len(provider.calls)==2 and all(cursor.date().isoformat()=='2026-11-16' for _,cursor in provider.calls)
        assert second.report['status']=='READY'
    finally:repo.close()


async def test_new_candidate_is_checked_and_policy_version_invalidates_cache(tmp_path):
    repo=Repository(tmp_path/'cache.db',backend='sqlite')
    try:
        for symbol in ('AAPL','MSFT'):prepare(HistoryStore(repo),symbol,SUNDAY)
        first,manager,provider=worker(repo)
        await finish(first,manager,provider)
        second,manager,provider=worker(repo,('AAPL','MSFT'))
        await finish(second,manager,provider)
        assert second.report['reused_history_tasks']==20 and second.report['required_tasks']==20
        saved=repo.get_control(CONTROL)
        repo.set_control(CONTROL,{**saved,'version':VERSION-1})
        third,manager,provider=worker(repo,('AAPL','MSFT'))
        await finish(third,manager,provider)
        assert third.report['reused_history_tasks']==0 and third.report['required_tasks']==40
        assert not provider.calls
    finally:repo.close()


def test_future_receipt_clock_and_exchange_bounds_never_reuse_bad_proof(tmp_path):
    repo=Repository(tmp_path/'cache.db',backend='sqlite')
    try:
        store=HistoryStore(repo)
        day,opened,closed=history_plan(SUNDAY)[-1]
        rows=minute_rows('AAPL',opened,known=SUNDAY)
        future=replace(rows[0],volume=999,received_at=SUNDAY+timedelta(hours=1))
        store.save('AAPL',day,[*rows,future])
        cache=HistoryInspections(repo)
        quality,reason=cache.inspect(store,'AAPL',day,opened,closed,SUNDAY)
        assert quality['complete'] and reason is None
        assert not cache.entries  # A hidden future correction could later invalidate coverage.
        quality,_=cache.inspect(store,'AAPL',day,opened,closed,SUNDAY+timedelta(hours=1))
        assert not quality['complete']
        repo.record(KIND,{**repo.get_record(KIND,record_id('AAPL',day)),'bars':[
            {**row} for row in repo.get_record(KIND,record_id('AAPL',day))['bars'] if row['volume']!=999]})
        cache.inspect(store,'AAPL',day,opened,closed,SUNDAY)
        assert cache.reusable('AAPL',day,opened,closed,SUNDAY)
        assert cache.reusable('AAPL',day,opened,closed,SUNDAY-timedelta(seconds=1)) is None
        assert cache.reusable('AAPL',day,opened,closed-timedelta(minutes=1),SUNDAY) is None
    finally:repo.close()

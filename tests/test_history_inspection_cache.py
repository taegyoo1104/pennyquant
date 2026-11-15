"""Completed history survives restart; content changes always invalidate coverage."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.history import HistoryStore, KIND, history_plan, record_id
from sessionquant.market.history_inspections import HistoryInspections, CONTROL, VERSION, INCOMPLETE_RETRY_DELAY
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


async def missing_history(repo, now=SUNDAY):
    store=HistoryStore(repo)
    prepare(store,'AAPL',now,omit_last=True)
    day,opened,closed=history_plan(now)[-1]
    first,manager,provider=worker(repo,now=now)
    provider.omit['AAPL']={closed}
    await finish(first,manager,provider)
    assert first.report['status']=='PARTIAL' and provider.calls
    return day,opened,closed


async def test_recently_exhausted_missing_history_survives_restart_without_becoming_eligible(tmp_path,monkeypatch):
    path=tmp_path/'incomplete.db'
    repo=Repository(path,backend='sqlite')
    day,_,_=await missing_history(repo)
    deadline=repo.get_control(CONTROL)['deferred_entries'][record_id('AAPL',day)]['retry_after']
    repo.close()
    repo=Repository(path,backend='sqlite')
    try:
        def no_decode(*_):pytest.fail('unchanged inspected history should not be decoded again')
        monkeypatch.setattr('sessionquant.market.history_inspections.decode_history',no_decode)
        for elapsed in (1,300):
            now=SUNDAY+timedelta(minutes=elapsed)
            second,manager,provider=worker(repo,now=now)
            await finish(second,manager,provider)
            assert not provider.calls
            assert second.report['reused_history_tasks']==19
            assert second.report['deferred_history_tasks']==1
            assert second.report['required_tasks']==second.report['received_pages']==0
            assert second.report['status']=='PARTIAL' and second.control['data_ready']
            assert second.selection.allowed(['AAPL'],now)==[]
            issue=second.report['issues'][0]
            assert issue['missing_minutes']==1 and issue['complete'] is False
            assert issue['retry_after']==deadline and issue['retry_deferred'] is True
            assert repo.get_control(CONTROL)['deferred_entries'][record_id('AAPL',day)]['retry_after']==deadline
    finally:repo.close()


async def test_incomplete_retry_expiry_allows_provider_correction(tmp_path):
    repo=Repository(tmp_path/'expire.db',backend='sqlite')
    try:
        day,_,closed=await missing_history(repo)
        now=SUNDAY+INCOMPLETE_RETRY_DELAY
        second,manager,provider=worker(repo,now=now)
        await finish(second,manager,provider)
        assert provider.calls==[('AAPL',closed)]
        assert second.report['deferred_history_tasks']==0
        assert second.report['status']=='READY'
        assert second.selection.allowed(['AAPL'],now)==['AAPL']
        assert record_id('AAPL',day) not in repo.get_control(CONTROL)['deferred_entries']
    finally:repo.close()


@pytest.mark.parametrize('change',['fill','delete','conflict','version','bounds'])
async def test_incomplete_retry_invalidates_on_db_or_rules_change(tmp_path,change):
    repo=Repository(tmp_path/'changed.db',backend='sqlite')
    try:
        day,opened,closed=await missing_history(repo)
        key=record_id('AAPL',day)
        if change=='fill':
            HistoryStore(repo).save('AAPL',day,minute_rows('AAPL',opened,known=SUNDAY))
        elif change=='delete':
            repo.db.execute('DELETE FROM market_history_sessions WHERE record_id=?',(key,))
            repo.db.commit()
        elif change=='conflict':
            row=repo.get_record(KIND,key)
            row['bars'].append({**row['bars'][0],'volume':999})
            repo.record(KIND,row)
        else:
            saved=repo.get_control(CONTROL)
            if change=='version':saved['version']=VERSION-1
            else:saved['deferred_entries'][key]['closed']=(closed-timedelta(minutes=1)).isoformat()
            repo.set_control(CONTROL,saved)
        second,manager,provider=worker(repo,now=SUNDAY+timedelta(minutes=1))
        await finish(second,manager,provider)
        assert second.report['deferred_history_tasks']==0
        if change=='fill':assert not provider.calls
        else:assert provider.calls
        assert second.report['status']==('PARTIAL' if change=='conflict' else 'READY')
    finally:repo.close()


async def test_conflict_only_is_deferred_but_remains_excluded(tmp_path):
    repo=Repository(tmp_path/'conflict.db',backend='sqlite')
    try:
        store=HistoryStore(repo);prepare(store,'AAPL',SUNDAY)
        day,_,_=history_plan(SUNDAY)[-1]
        original=store.get('AAPL',day)[1][0]
        store.save('AAPL',day,[replace(original,volume=999)])
        first,manager,provider=worker(repo)
        await finish(first,manager,provider)
        assert not provider.calls and first.report['status']=='PARTIAL'
        second,manager,provider=worker(repo,now=SUNDAY+timedelta(minutes=1))
        await finish(second,manager,provider)
        assert not provider.calls and second.report['deferred_history_tasks']==1
        assert second.report['issues'][0]['conflicting_minutes']>0
        assert second.report['history_ready_count']==0
    finally:repo.close()


@pytest.mark.parametrize('failure',['unavailable','stalled','page_limit'])
async def test_failed_or_unfinished_download_is_not_deferred(tmp_path,failure):
    repo=Repository(tmp_path/'failed.db',backend='sqlite')
    try:
        store=HistoryStore(repo);prepare(store,'AAPL',SUNDAY)
        day,opened,closed=history_plan(SUNDAY)[-1]
        row=repo.get_record(KIND,record_id('AAPL',day))
        repo.record(KIND,{**row,'bars':row['bars'][:-100]})
        first,manager,provider=worker(repo)
        calls=[]
        async def fetch(symbol,cursor):
            calls.append(cursor)
            if failure=='unavailable':return None
            count=int((cursor-opened).total_seconds()//60)-1
            return minute_rows(symbol,opened,count=count,known=SUNDAY)[-1:], (
                cursor if failure=='stalled' else cursor-timedelta(minutes=2))
        for _ in range(10):
            await first.advance(manager,fetch,provider.accept,provider.seen)
            if first.control['data_ready'] or failure=='unavailable':break
        assert calls
        assert not repo.get_control(CONTROL,{}).get('deferred_entries')
        second,manager,provider=worker(repo)
        await finish(second,manager,provider)
        assert second.report['deferred_history_tasks']==0 and provider.calls
        assert second.report['status']=='READY'
    finally:repo.close()


def test_incomplete_future_receipt_and_backdated_check_cannot_be_reused(tmp_path):
    repo=Repository(tmp_path/'future.db',backend='sqlite')
    try:
        store=HistoryStore(repo);day,opened,closed=history_plan(SUNDAY)[-1]
        rows=minute_rows('AAPL',opened,known=SUNDAY)
        store.save('AAPL',day,[*rows[:-2],replace(rows[-1],received_at=SUNDAY+timedelta(hours=1))])
        cache=HistoryInspections(repo)
        cache.inspect(store,'AAPL',day,opened,closed,SUNDAY)
        cache.defer_retry('AAPL',day)
        assert not cache.deferred_entries
        cache.inspect(store,'AAPL',day,opened,closed,SUNDAY+timedelta(hours=1))
        cache.defer_retry('AAPL',day)
        assert cache.deferred('AAPL',day,opened,closed,SUNDAY+timedelta(hours=1))
        assert cache.deferred('AAPL',day,opened,closed,SUNDAY) is None
        assert cache.reusable('AAPL',day,opened,closed,SUNDAY+timedelta(hours=1)) is None
    finally:repo.close()


async def test_deferred_history_does_not_skip_current_bars_for_existing_holdings(tmp_path):
    repo=Repository(tmp_path/'holding.db',backend='sqlite')
    try:
        before_open=datetime(2026,11,16,14,tzinfo=timezone.utc)
        await missing_history(repo,now=before_open)
        during_session=before_open+timedelta(minutes=40)
        second,manager,provider=worker(repo,now=during_session)
        for _ in range(20):
            await second.advance(manager,provider.fetch,provider.accept,provider.seen,priority=('AAPL',))
            if second.control['data_ready']:break
        assert second.control['data_ready'] and second.report['deferred_history_tasks']==1
        assert len(provider.seen('AAPL'))==10
        assert {symbol for symbol,_ in provider.calls}=={'AAPL','SPY','QQQ'}
        assert all(cursor.date()==during_session.date() for _,cursor in provider.calls)
        assert second.selection.allowed(['AAPL'],during_session)==[]
    finally:repo.close()

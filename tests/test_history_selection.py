"""Operating subset uses complete historical sessions; risk scope is independent."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sessionquant.market.history import HistoryStore, history_plan, KIND, record_id
from sessionquant.watchlist.history_selection import DailyHistorySelection, inspect_history, operating_session, CONTROL
from sessionquant.watchlist.selection import WatchlistManager, VIEW
from watchlist_fixtures import context, observe, NOW
from test_watchlist_history import minute_rows


def prepare(store, symbol, now=NOW, *, omit_last=False, known=None):
    plan = history_plan(operating_session(now)[1])
    for index, (day, opened, closed) in enumerate(plan):
        count = int((closed-opened).total_seconds()/60)
        rows = minute_rows(symbol, opened, count=count, known=known or now)
        if omit_last and index == len(plan)-1:
            rows = rows[:-1]
        store.save(symbol, day, rows)


def attach(repo, universe, config, provider):
    return WatchlistManager(repo, universe, config, provider=provider, allow_synthetic=True,
                            history_selection=DailyHistorySelection(repo))


async def test_exclusion_before_focus_and_dashboard_preserves_base_and_reasons(tmp_path):
    repo, universe, config, provider, original = context(tmp_path, size=3)
    try:
        await original.refresh(NOW)
        store = HistoryStore(repo)
        prepare(store, 'T499')
        prepare(store, 'T498', omit_last=True)
        manager = attach(repo, universe, config, provider)
        after = observe(manager)
        assert manager.base_symbols(after) == ['T499', 'T498', 'T497']
        assert manager.light_symbols(after) == ['T499']
        assert manager.focus(after) == ['T499']
        assert manager.entry_blockers('T498', after) == ['history_not_in_daily_operating_watchlist']
        assert manager.entry_blockers('T001', after) == ['outside_market_cap_watchlist']
        manager.publish(after, priority_symbols=['T498'])
        view = repo.get_control(VIEW)
        assert len(view['items']) == 3 and view['operating_count'] == 1
        assert view['history_selection']['excluded_count'] == 2
        assert view['items'][1]['history_selection']['problems'][0]['missing_minutes'] == 1
        assert view['news_contract']['analysis_symbols'] == ['T498', 'T499']
        assert len(manager.store.at(after)['items']) == 3
    finally:
        repo.close()


async def test_restart_freezes_repairs_and_new_members_until_next_session(tmp_path):
    repo, universe, config, provider, manager = context(tmp_path, size=3)
    try:
        await manager.refresh(NOW)
        store = HistoryStore(repo)
        prepare(store, 'T499')
        policy = DailyHistorySelection(repo)
        assert policy.allowed(['T499', 'T498'], NOW) == ['T499']
        prepare(store, 'T498', known=NOW+timedelta(seconds=10))
        restored = DailyHistorySelection(repo)
        assert restored.allowed(['T499', 'T498'], NOW+timedelta(seconds=20)) == ['T499']
        assert restored.allowed(['T498', 'T497'], NOW+timedelta(seconds=20)) == []
        monday = datetime(2026,11,16,15,tzinfo=timezone.utc)
        prepare(store, 'T499', monday)
        prepare(store, 'T498', monday)
        assert restored.allowed(['T499', 'T498'], monday) == ['T499', 'T498']
        assert repo.get_control(CONTROL)['session'] == '2026-11-16'
    finally:
        repo.close()


async def test_weekend_preview_rechecks_at_open_without_freezing_readonly_preview(tmp_path):
    repo, _, _, _, _ = context(tmp_path, size=2)
    try:
        sunday = datetime(2026,11,15,12,tzinfo=timezone.utc)
        opened = datetime(2026,11,16,14,30,tzinfo=timezone.utc)
        store = HistoryStore(repo)
        prepare(store, 'T499', sunday)
        preview = inspect_history(store, ['T499','T498'], sunday)
        assert preview['session'] == '2026-11-16' and preview['included_count'] == 1
        assert repo.get_control(CONTROL) is None
        policy = DailyHistorySelection(repo)
        assert policy.allowed(['T499','T498'], sunday) == ['T499']
        assert not policy.frame['frozen']
        prepare(store, 'T498', sunday+timedelta(minutes=1))
        assert policy.allowed(['T499','T498'], opened) == ['T499','T498']
        assert policy.frame['frozen']
        assert policy.frame['checked_at'] == opened.isoformat()
    finally:
        repo.close()


async def test_future_receipt_conflicts_and_invalid_rows_do_not_pass_cached_quality(tmp_path):
    repo, _, _, _, _ = context(tmp_path, size=3)
    try:
        store = HistoryStore(repo)
        prepare(store, 'T499', known=NOW+timedelta(seconds=1))
        prepare(store, 'T498')
        day, _, _ = history_plan(NOW)[0]
        bar = store.get('T498', day)[1][0]
        store.save('T498', day, [replace(bar, volume=999)])
        row = repo.get_record(KIND, record_id('T498',day))
        row['quality']['complete'] = True
        repo.record(KIND,row)
        row = {**row, 'id':record_id('T497',day), 'symbol':'T497'}
        repo.record(KIND,row)  # Bars still belong to T498; corrupt record must fail closed.
        report = inspect_history(HistoryStore(repo), ['T499','T498','T497'], NOW)
        assert report['included_symbols'] == []
        assert report['items'][0]['complete_sessions'] == 0
        assert report['items'][1]['problems'][0]['conflicting_minutes'] == 1
        assert report['items'][2]['problems'][0]['reasons'] == ['history_record_invalid']
    finally:
        repo.close()


async def test_partial_cap_is_not_promoted_by_complete_history(tmp_path):
    repo, universe, config, provider, original = context(tmp_path, size=3)
    try:
        provider.batch['observations'] = provider.batch['observations'][:-1]
        await original.refresh(NOW)
        store = HistoryStore(repo)
        prepare(store, 'T498')
        manager = attach(repo, universe, config, provider)
        manager.publish(NOW)
        view = repo.get_control(VIEW)
        assert view['status'] == 'PARTIAL' and view['operating_count'] == 0
        assert view['history_selection']['included_symbols'] == ['T498']
        assert manager.focus(NOW) == []
        assert manager.entry_blockers('T498',NOW) == ['watchlist_partial']
    finally:
        repo.close()


async def test_excluded_candles_stop_but_held_and_pending_risk_quotes_continue(tmp_path):
    from sessionquant.market.source import TossSessionSource
    from test_watchlist_source import Market
    repo, universe, config, provider, original = context(tmp_path, size=4)
    stamp = NOW+timedelta(seconds=60)

    class Client(Market):
        async def candles(self, symbol, **kwargs):
            self.calls.append(('candles',symbol))
            return [], None

    try:
        await original.refresh(NOW)
        prepare(HistoryStore(repo), 'T499')
        manager = attach(repo, universe, config, provider)
        observe(manager)
        client = Client(lambda:stamp)
        source = TossSessionSource(client, watchlist=manager, clock=lambda:stamp,
                                   history_store=HistoryStore(repo))
        delivered = []
        source.on_priority_batch = lambda batch:delivered.extend(q.symbol for q in batch.quotes)
        await source.poll([], priority_symbols=['T498','T497'])
        assert delivered[:4] == ['T498','T497','SPY','QQQ']
        assert next(v for k,v in client.calls if k=='prices') == ['T499','SPY','QQQ']
        assert 'T496' not in {v for k,v in client.calls if k in {'quote','candles'}}
        assert {'T498','T497'} <= {v for k,v in client.calls if k=='candles'}
        assert repo.get_control('watchlist_rvol_v1')['symbols'] == ['T499']
        assert repo.all('orders') == []
    finally:
        repo.close()


def test_early_close_and_holidays_use_next_actual_session():
    friday = datetime(2026,11,27,17,59,tzinfo=timezone.utc)
    assert operating_session(friday)[0] == '2026-11-27'
    assert operating_session(friday+timedelta(minutes=1))[0] == '2026-11-30'
    assert operating_session(datetime(2026,11,26,15,tzinfo=timezone.utc))[0] == '2026-11-27'
    plan = history_plan(operating_session(friday+timedelta(minutes=1))[1])
    assert len(plan)==21 and plan[-1][0]=='2026-11-27'


async def test_no_history_keeps_risk_management_and_no_normal_candidates(tmp_path):
    from sessionquant.market.source import TossSessionSource
    from test_watchlist_source import Market
    repo, universe, config, provider, original = context(tmp_path, size=2)
    try:
        await original.refresh(NOW)
        manager = attach(repo, universe, config, provider)
        client = Market(lambda:NOW)
        await TossSessionSource(client, watchlist=manager, clock=lambda:NOW).poll([], priority_symbols=['T498'])
        assert manager.light_symbols(NOW) == [] and manager.focus(NOW) == []
        assert next(v for k,v in client.calls if k=='prices') == ['SPY','QQQ']
        assert 'T498' in {v for k,v in client.calls if k=='quote'}
        assert 'T499' not in {v for k,v in client.calls if k in {'quote','candles'}}
        assert repo.get_control(CONTROL)['included_count'] == 0
    finally:
        repo.close()


async def test_preopen_restart_rechecks_and_frozen_selection_is_recorded(tmp_path, monkeypatch):
    from sessionquant.research import journal
    repo, _, _, _, _ = context(tmp_path, size=2)
    sunday = datetime(2026,11,15,12,tzinfo=timezone.utc)
    try:
        monkeypatch.setattr(journal,'clock',lambda:sunday)
        repo.set_control(journal.CONTROL,{'active':True,'run_id':'history-selection-fixture','source':'toss','synthetic':True})
        policy = DailyHistorySelection(repo)
        assert policy.allowed(['T499'],sunday) == []
        prepare(HistoryStore(repo),'T499',sunday)
        restored = DailyHistorySelection(repo)
        assert restored.allowed(['T499'],sunday+timedelta(seconds=1)) == ['T499']
        events = [r for r in repo.all('research_events') if r['kind']==CONTROL]
        assert len(events)==2 and events[-1]['data']['included_symbols']==['T499']
        assert events[-1]['available_at'] >= events[-1]['data']['checked_at']
    finally:
        repo.close()


def test_dashboard_shows_prepared_subset_and_exclusion_reason():
    from streamlit.testing.v1 import AppTest
    now = datetime.now(timezone.utc).isoformat()
    state = {'status':'VALID', 'updated_at':now, 'watchlist_limit':50, 'focus_limit':20,
             'operating_count':1, 'items':[
                 {'ticker':'AAPL','operating':True,'history_ready':True},
                 {'ticker':'MSFT','operating':False,'history_ready':False,
                  'history_selection':{'problems':[{'session':'2026-11-13','reasons':['history_minutes_missing_or_conflicting']}]}}],
             'history_selection':{'session':'2026-11-16','checked_at':now,'frozen':False,
                                  'base_count':2,'included_count':1,'excluded_count':1}}
    app = AppTest.from_string('from sessionquant.dashboard.watchlist_panel import render_watchlist\n'
                              f'render_watchlist({state!r}, [])').run()
    assert not app.exception
    assert any(m.label=='유효 운영 종목' and m.value=='1 / 50' for m in app.metric)
    assert any('분봉 준비 1개' in c.value and '제외 1개' in c.value for c in app.caption)
    table = app.dataframe[0].value
    assert '분봉 누락·값 충돌' in table.loc[1,'분봉 제외 사유']

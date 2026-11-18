"""Slow collection diagnostics must not leak SQL or promote stale RVOL."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import RLock

import pytest

from sessionquant.performance import capture, database_query
from sessionquant.dashboard.market_quality_panel import database_rows, watchlist_rvol_rows
from sessionquant.dashboard.labels import display_rows


def test_database_categories_count_calls_without_retaining_query_values(monkeypatch):
    import sessionquant.performance as perf
    now=[0.0]
    monkeypatch.setattr(perf,'perf_counter',lambda:now[0])
    with capture() as timings:
        for sql in ["SELECT payload FROM pq_controls WHERE name='PRIVATE_TOKEN'",
                    "SELECT payload FROM pq_controls WHERE name='OTHER_SECRET'"]:
            with database_query(sql,('market_bars',)):now[0]+=1
        with database_query("INSERT INTO secret_table VALUES ('password')",('market_bars',)):now[0]+=2
        with pytest.raises(ValueError):
            with database_query('SELECT payload FROM pq_market_bars',('market_bars',)):
                now[0]+=3
                raise ValueError('failure')
        snapshot=timings.snapshot()
    rows={(r['operation'],r['table']):r for r in snapshot['database']}
    assert rows['SELECT','pq_controls']['calls']==2
    assert rows['SELECT','pq_controls']['seconds']==2
    assert rows['SELECT','pq_market_bars']['seconds']==3
    assert rows['INSERT','other']['seconds']==2
    assert not any(secret in str(snapshot) for secret in ('PRIVATE','OTHER_SECRET','secret_table','password','payload'))
    display=database_rows(snapshot)
    assert display[0]['자료']=='분봉'
    assert display[0]['DB 작업']=='조회'


def test_database_categories_are_bounded_and_capture_does_not_cross_scopes():
    with capture() as timings:
        for i in range(1000):
            with database_query(f'SELECT * FROM pq_private{i}',(f'private{i}',)):pass
    assert len(timings.snapshot()['database'])<=65
    assert sum(r['calls'] for r in timings.snapshot()['database'])==1000
    with capture() as other:
        assert other.snapshot()['database']==[]


def test_mysql_execute_reports_categories_without_changing_result():
    from sessionquant.database.mysql_repository import MySQLRepository
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,params):self.params=params
        def fetchone(self):return (1,)
    class DB:
        def cursor(self):return Cursor()
    repo=MySQLRepository.__new__(MySQLRepository)
    repo.kinds=();repo.db=DB();repo._mutex=RLock();repo._driver_errors=(RuntimeError,)
    with capture() as timing:
        assert repo._execute('SELECT payload FROM pq_controls WHERE name=%s',('PRIVATE',),fetch='one')==(1,)
    assert timing.snapshot()['database'][0]['table']=='pq_controls'
    assert 'PRIVATE' not in str(timing.snapshot())


def test_stale_rvol_history_stays_visible_but_current_and_trading_values_stay_empty():
    cutoff=datetime(2026,11,17,15,0,tzinfo=timezone.utc)
    observed=cutoff+timedelta(seconds=10)
    row={'symbol':'AAPL','cutoff_at':cutoff.isoformat(),'observed_at':observed.isoformat(),
         'calculated_valid_until':(observed+timedelta(seconds=30)).isoformat(),
         'calculated_value':1.25,'value':1.25,'status':'VERIFIED','calculation_status':'CALCULATED',
         'complete_sessions':20,'reasons':[]}
    source={'items':[row]};original=deepcopy(source)
    fresh=watchlist_rvol_rows(source,observed)[0]
    assert fresh['현재성']=='최신' and fresh['RVOL 계산값']==1.25
    stale=watchlist_rvol_rows(source,cutoff+timedelta(minutes=3))[0]
    assert stale['마지막 계산값·참고']==1.25
    assert stale['RVOL 계산값'] is None and stale['비교 근거 검증값'] is None
    assert stale['기준 시각 경과(초)']==180
    assert stale['현재성']=='이전 계산 · 갱신 대기'
    translated,unknown=display_rows([stale])
    assert not unknown and '상세 확인 필요' not in translated[0]['확인 필요']
    assert source==original
    future=watchlist_rvol_rows(source,cutoff-timedelta(seconds=1))[0]
    assert future['마지막 계산값·참고'] is None and future['RVOL 계산값'] is None


def test_incomplete_rvol_does_not_invent_a_ratio_from_partial_cumulative_volume():
    now=datetime(2026,11,17,15,0,20,tzinfo=timezone.utc)
    rows=watchlist_rvol_rows({'items':[{'symbol':'AAPL','cutoff_at':now.replace(second=0).isoformat(),
        'observed_at':now.isoformat(),'calculated_value':None,'current_cumulative_volume':200,
        'historical_mean_cumulative_volume':100,'reasons':['current_session_coverage_incomplete']},
        {'symbol':'KO','reasons':['symbol_not_yet_collected']}]},now)
    assert all(r['마지막 계산값·참고'] is None and r['RVOL 계산값'] is None for r in rows)
    assert rows[0]['현재성']=='계산 자료 부족'


def test_entry_explanation_distinguishes_pattern_wait_from_stale_approval():
    from sessionquant.dashboard.selection_panel import entry_rows, selection_view
    now=datetime(2026,11,17,17,0,tzinfo=timezone.utc)
    frame={'evaluated_at':now.isoformat(),'valid_until':(now+timedelta(seconds=10)).isoformat(),
        'rows':[{'symbol':'AAPL','evaluated_at':now.isoformat(),'regime':'BULLISH',
            'portfolio_status':'BLOCKED','blockers':['technical_signal_absent'],
            'indicators':{'relative_volume':1.4,'strategy_status':'WAITING',
                'strategy_diagnostics':['strategy_rebound_volume_missing']}}]}
    row=entry_rows(selection_view(frame,now))[0]
    assert row['전략 판단']=='전략 조건 대기' and row['전략 RVOL']==1.4
    assert 'technical_signal_absent' not in row['대기·차단 이유']
    assert row['현재성']=='현재 평가'
    translated,_=display_rows([row])
    assert '상세 확인 필요' not in translated[0]['대기·차단 이유']
    old=entry_rows(selection_view(frame,now+timedelta(seconds=11)))[0]
    assert old['현재성']=='이전 평가 · 갱신 대기'


def test_etf_search_progress_does_not_claim_coverage_verified():
    from sessionquant.dashboard.market_quality_panel import history_progress_rows
    rows=history_progress_rows({'SPY':{'status':'SESSION_WINDOW_REACHED','pages':186,
        'cursor':'2026-11-17T14:29:00+00:00','done':True}})
    assert rows[0]['보충 상태']=='당일 조회 범위 도달'
    assert rows[0]['추가 조회 횟수']==186
    assert rows[0]['다음 조회 기준']!='—'
    assert not display_rows(rows)[1]

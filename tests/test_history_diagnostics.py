from copy import deepcopy
from dataclasses import replace
import json

import pytest

from sessionquant.database.repository import Repository
from sessionquant.market.history import HistoryStore,history_plan,KIND,record_id
from sessionquant.market.history_commands import PROGRESS,command
from sessionquant.market.history_diagnostics import diagnose_history
from sessionquant.session_config import SessionConfig
from test_watchlist_history import NOW,minute_rows


def data(repo):
    day,opened,closed=history_plan(NOW)[-1]
    store=HistoryStore(repo)
    full=minute_rows('AAPL',opened)
    store.save('AAPL',day,full)
    rows=minute_rows('MSFT',opened)
    rows=[b for i,b in enumerate(rows,1) if i not in {100,101,390}]
    rows.append(replace(rows[199],volume=999))
    store.save('MSFT',day,rows,progress={'status':'SESSION_START_REACHED','pages':2,'cursor':opened.isoformat()})
    plan={'symbols':['AAPL','MSFT'],'session_dates':[day],'complete_symbol_days':1}
    repo.set_control(PROGRESS,plan)
    return plan,day


def test_exact_gaps_conflicts_prefixes_and_no_rewrites(tmp_path):
    repo=Repository(tmp_path/'data.db',backend='sqlite')
    try:
        plan,day=data(repo)
        before=deepcopy(repo.all(KIND))
        result=diagnose_history(repo,plan)
        summary=result['summary']
        assert summary['complete_symbol_days']==1 and summary['incomplete_symbol_days']==1
        assert summary['expected_minutes']==780 and summary['received_unique_minutes']==777
        assert summary['missing_minutes']==3 and summary['conflicting_minutes']==1
        assert summary['fully_complete_symbols']==1
        row=result['incomplete_sessions'][0]
        assert row['symbol']=='MSFT' and row['missing_elapsed_minute_ranges']==[[100,101],[390,390]]
        assert row['first_problem_elapsed_minute']==100
        assert result['symbols'][1]['complete_prefix_sessions']['75']==1
        assert result['symbols'][1]['complete_prefix_sessions']['120']==0
        assert repo.all(KIND)==before and repo.get_control(PROGRESS)==plan
    finally:repo.close()


def test_missing_record_and_invalid_record_are_distinct(tmp_path):
    repo=Repository(tmp_path/'data.db',backend='sqlite')
    try:
        plan,day=data(repo)
        plan['symbols']+=['MISSING']
        row=repo.get_record(KIND,record_id('AAPL',day))
        row['bars'][0]['source']='UNKNOWN'
        repo.record(KIND,row)
        result=diagnose_history(repo,plan)
        assert result['summary']['unreadable_sessions']==1
        assert result['summary']['missing_minutes']==393
        assert any(r.get('stop_reason')=='NO_RECORD' for r in result['incomplete_sessions'])
        assert any(r['status']=='STORED_DATA_INVALID' for r in result['incomplete_sessions'])
    finally:repo.close()


async def test_cli_output_is_new_file_with_details_and_no_secret_or_api(tmp_path,monkeypatch):
    # CLI sets process-wide runtime settings; isolate them from subsequent MySQL tests.
    monkeypatch.setenv('SQ_ENVIRONMENT','PAPER_LOCAL')
    monkeypatch.setenv('SQ_DB_BACKEND','sqlite')
    config=SessionConfig(database_backend='sqlite',db_path=tmp_path/'data.db',company_rss_catalog=False,
                         universe_path=tmp_path/'universe.json')
    repo=Repository(config.db_path,backend='sqlite')
    plan,_=data(repo)
    repo.close()
    out=tmp_path/'report.json'
    result=await command(config,'history-status',output=out)
    assert result['provider_calls']==0 and result['history_quality']['incomplete_symbol_days']==1
    assert json.loads(out.read_text())['backfill']==plan
    assert len(json.loads(out.read_text())['history_quality']['incomplete_sessions'])==1
    with pytest.raises(FileExistsError):await command(config,'history-status',output=out)


def test_no_plan_is_not_reported_as_complete(tmp_path):
    repo=Repository(tmp_path/'data.db',backend='sqlite')
    try:assert diagnose_history(repo,{})['status']=='NO_BACKFILL_PLAN'
    finally:repo.close()

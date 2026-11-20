"""Credit estimates read relevant small fields without losing legacy uncertainty."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from sessionquant.database.repository import Repository
from sessionquant.llm.costs import credit_summary, set_manual_credit

NOW = datetime(2026, 11, 20, 16, tzinfo=timezone.utc)
CHECKED = NOW-timedelta(hours=1)


@pytest.fixture
def repo(tmp_path):
    value=Repository(tmp_path/'credit.db',backend='sqlite')
    set_manual_credit(value,'10',CHECKED,now=NOW)
    yield value
    value.close()


def test_credit_projection_excludes_old_payloads_and_preserves_decimal_cost(repo,monkeypatch):
    for index in range(35):
        repo.record('llm_usage',{'id':str(index),'timestamp':(CHECKED-timedelta(days=1)).isoformat(),
            'cost_usd':'1.5','unused_large_body':'x'*20000})
    repo.record('llm_usage',{'id':'recent','timestamp':NOW.isoformat(),
        'request_started_at':CHECKED.isoformat(),'cost_usd':'0.123456789012345678901',
        'cost_status':'TOKEN_BASED_ESTIMATE','unused_large_body':'x'*20000})
    rows=list(repo.iter_credit_usage(CHECKED,batch_size=2))
    assert len(rows)==1
    assert set(rows[0])=={'timestamp','request_started_at','cost_usd','cost_status'}
    assert len(str(rows))<500
    monkeypatch.setattr(repo,'iter_records',lambda *_args,**_kwargs:pytest.fail('full usage scan'))
    summary=credit_summary(repo,NOW)
    assert summary['local_cost_since_check_usd']==Decimal('0.123456789012345678901')
    assert summary['estimated_credit_usd']==Decimal('9.876543210987654321099')


@pytest.mark.parametrize('updates,uncertain,cost',[
    ({'timestamp':None},True,'0'),
    ({'timestamp':'not-a-time'},True,'0'),
    ({'timestamp':'2026-02-30T12:00:00+00:00'},True,'0'),
    ({'timestamp':'2026-11-19T25:00:00+00:00'},True,'0'),
    ({'timestamp':'0000-01-01T12:00:00+00:00'},True,'0'),
    ({'timestamp':{'bad':'time'}},True,'0'),
    ({'timestamp':'2026-11-19T12:00:00'},True,'0'),
    ({'request_started_at':'not-a-time'},True,'0'),
    ({'request_started_at':(CHECKED-timedelta(seconds=1)).isoformat()},True,'0'),
    ({'timestamp':(NOW+timedelta(seconds=1)).isoformat(),'request_started_at':'bad'},False,'0'),
    ({'timestamp':(CHECKED-timedelta(days=1)).isoformat(),'request_started_at':'bad'},True,'0'),
    ({'timestamp':'2026-11-20T16:00:00+01:00','request_started_at':'2026-11-20T16:00:00+01:00'},False,'0.25'),
    ({'request_started_at':None},False,'0.25'),
    ({'request_started_at':''},False,'0.25'),
    ({'request_started_at':False},False,'0.25'),
    ({'request_started_at':[]},False,'0.25'),
    ({'cost_usd':'invalid'},True,'0'),
    ({'cost_usd':False},True,'0'),
    ({'cost_usd':True},True,'0'),
])
def test_credit_projection_keeps_legacy_and_crossing_time_semantics(repo,updates,uncertain,cost):
    row={'id':'test','timestamp':NOW.isoformat(),'request_started_at':CHECKED.isoformat(),'cost_usd':'0.25',**updates}
    repo.record('llm_usage',row)
    summary=credit_summary(repo,NOW)
    assert summary['local_cost_since_check_usd']==Decimal(cost)
    assert (summary['estimated_credit_usd'] is None)==uncertain
    if uncertain:
        assert 'request_crossed_manual_check_or_legacy_time_unknown' in summary['credit_uncertainty']


def test_credit_projection_boundaries_fractional_times_and_pending_reservations(repo):
    checked=CHECKED.replace(microsecond=500000)
    set_manual_credit(repo,'10',checked,now=NOW)
    repo.record('llm_usage',{'id':'crossed','timestamp':checked.isoformat(),
        'request_started_at':CHECKED.isoformat(),'cost_usd':'0.1'})
    repo.record('llm_usage',{'id':'unconfirmed','timestamp':NOW.isoformat(),
        'request_started_at':checked.isoformat(),'cost_usd':'0.2','cost_status':'UNCONFIRMED_RESERVED_ESTIMATE'})
    repo.set_control('session_llm_budget_v1',{'2026-11':{'spent':'0.3','reservations':{
        'pending':{'amount':'0.4','created_at':checked.isoformat()}}}})
    rows=list(repo.iter_credit_usage(checked,batch_size=1))
    assert len(rows)==2
    summary=credit_summary(repo,NOW)
    assert summary['local_cost_since_check_usd']==Decimal('0.2')
    assert summary['estimated_credit_usd'] is None
    assert {'pending_reservations','unconfirmed_call_cost',
        'request_crossed_manual_check_or_legacy_time_unknown'} <= set(summary['credit_uncertainty'])


@pytest.mark.parametrize('batch_size',[0,2001,True])
def test_credit_projection_requires_bounded_pages(repo,batch_size):
    with pytest.raises(ValueError):list(repo.iter_credit_usage(CHECKED,batch_size=batch_size))


def test_mysql_credit_projection_is_namespace_scoped_and_does_not_select_full_payload():
    import json
    from sessionquant.database.mysql_repository import MySQLRepository
    repo=object.__new__(MySQLRepository);repo.namespace='paper-test';calls=[]
    projected={'timestamp':NOW.isoformat(),'request_started_at':CHECKED.isoformat(),
               'cost_usd':'0.123456789012345678901','cost_status':'TOKEN_BASED_ESTIMATE'}
    def execute(sql,params,*,fetch):
        calls.append((sql,params,fetch))
        return [(10,json.dumps(projected))] if len(calls)==1 else []
    repo._execute=execute
    assert list(repo.iter_credit_usage(CHECKED,batch_size=1))==[projected]
    assert len(calls)==2
    for sql,params,fetch in calls:
        assert 'SELECT seq,JSON_OBJECT(' in sql and 'SELECT seq,payload' not in sql
        assert 'FORCE INDEX (ix_namespace_seq)' in sql and 'namespace=%s AND seq>%s' in sql
        assert params[0]=='paper-test' and params[-1]==1 and fetch=='all'
    assert calls[0][1][1]==0 and calls[1][1][1]==10

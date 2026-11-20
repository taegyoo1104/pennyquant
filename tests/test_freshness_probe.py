from datetime import datetime, timedelta, timezone
import io
import json

import pytest

from scripts import check_market_freshness as probe
from scripts.check_market_freshness import KEYS, read_controls, summarize


@pytest.mark.parametrize('configured,explicit,expected',[
    (None,None,'config/sessionquant.realtime-paper.example.yaml'),
    ('',None,'config/sessionquant.realtime-paper.example.yaml'),
    ('config/sessionquant.strategy-lab-websocket.example.yaml',None,'config/sessionquant.strategy-lab-websocket.example.yaml'),
    ('config/sessionquant.strategy-lab-websocket.example.yaml','selected.yaml','selected.yaml'),
])
def test_cli_uses_active_profile_unless_explicitly_selected(monkeypatch,capsys,configured,explicit,expected):
    if configured is None:monkeypatch.delenv('SQ_SESSION_CONFIG',raising=False)
    else:monkeypatch.setenv('SQ_SESSION_CONFIG',configured)
    paths=[]
    def read(path):
        paths.append(path)
        return {}
    monkeypatch.setattr(probe,'read_controls',read)
    assert probe.main(['--config',explicit] if explicit else [])==0
    assert paths==[expected]
    assert json.loads(capsys.readouterr().out)['order_writes']==0


def test_probe_exports_timing_evidence_without_freeform_payloads():
    now=datetime(2026,11,18,14,47,23,tzinfo=timezone.utc)
    controls={KEYS[0]:{'items':[{'symbol':'PG','observed_at':'2026-11-18T14:46:52Z',
        'quote_at':'2026-11-18T14:33:23Z','quote_received_at':'2026-11-18T14:46:51Z',
        'rvol':{'complete_sessions':20},'private':'SECRET'}]},
        KEYS[1]:{'last_quote_success':{'PG':'2026-11-18T14:46:51Z'},'private':'SECRET'}}
    result=summarize(controls,now)
    assert result['provider_calls']==result['order_writes']==0
    row=result['symbols'][0]
    assert row['evaluation_age_seconds']==31
    assert row['quote_age_on_receipt_seconds']==808
    assert row['seconds_since_quote_received']==32
    assert row['rvol_at_last_evaluation'] is None
    assert 'SECRET' not in json.dumps(result)


def test_probe_reads_only_named_controls_in_read_only_transaction(monkeypatch):
    import pymysql
    monkeypatch.setenv('SQ_MYSQL_PASSWORD','synthetic-test-only')
    statements=[]
    class Connection:
        closed=False
        def cursor(self):return self
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,params=None):statements.append((sql,params))
        def fetchall(self):return [(KEYS[0],'{}')]
        def close(self):self.closed=True
    connection=Connection()
    monkeypatch.setattr(pymysql,'connect',lambda **kwargs:connection)
    result=read_controls('config/sessionquant.realtime-paper.example.yaml')
    assert result=={KEYS[0]:{}} and connection.closed
    assert statements[0][0]=='START TRANSACTION READ ONLY'
    assert statements[1][0].startswith('SELECT /*+ MAX_EXECUTION_TIME(5000) */')
    assert statements[1][1][1:]==KEYS and len(statements)==2
    assert statements[1][0].count('%s')==len(KEYS)+1


def test_snapshot_preserves_different_producer_times_and_rvol_boundaries():
    now=datetime(2026,11,18,15,42,tzinfo=timezone.utc)
    controls={KEYS[0]:{'items':[{'symbol':'PG','observed_at':'2026-11-18T15:41:20Z',
        'strategy_rvol':1.2,'strategy_rvol_status':'EXPERIMENTAL_UNVERIFIED',
        'rvol':{'cutoff_at':'2026-11-18T15:40:00Z','calculated_value':1.2,'status':'UNKNOWN',
            'calculation_status':'CALCULATED','reasons':['split_volume_comparability_unknown'],
            'current_missing_minutes':0}}]},
        KEYS[1]:{'focus':[{'ticker':'PG'},{'ticker':'NVDA'}]},
        KEYS[3]:{'heartbeat':'2026-11-18T15:41:59Z','status':'WAITING','data_ready':True,
                 'account_seq':'SECRET','message':'SECRET'},
        KEYS[4]:{'evaluated_at':'2026-11-18T15:41:20Z',
            'rows':[{'symbol':'PG','blockers':['candidate_quote_time_mismatch','SECRET']} ]},
        KEYS[5]:{'items':[{'symbol':'PG','cutoff_at':'2026-11-18T15:41:00Z',
            'current_missing_minutes':1,'current_missing_tail_minutes':1,
            'reasons':['current_session_coverage_incomplete']} ]}}
    result=summarize(controls,now)
    row=result['symbols'][0]
    assert row['evaluation_age_seconds']==40
    assert row['strategy_rvol_saved']==1.2  # Saved evidence, not renewed by this probe.
    assert row['strategy_rvol_status']=='EXPERIMENTAL_UNVERIFIED'
    assert row['strategy_volume']['cutoff_at']=='2026-11-18T15:40:00+00:00'
    assert row['minute_volume']['cutoff_at']=='2026-11-18T15:41:00+00:00'
    assert row['strategy_volume']['value'] is None
    assert row['minute_volume']['current_missing_tail_minutes']==1
    assert row['selection']['blockers']=={'codes':['candidate_quote_time_mismatch'],'other_count':1}
    assert result['engine']['heartbeat_age_seconds']==1
    assert result['symbols'][1]['symbol']=='NVDA'
    assert result['symbols'][1]['evaluated_at'] is None
    assert 'SECRET' not in json.dumps(result)


def test_malformed_optional_fields_remain_unknown_without_raw_export():
    controls={KEYS[0]:{'items':[None,{'symbol':'PG','rvol':{'current_missing_examples':5},
        'last_evaluation':True,'bars':'SECRET','strategy_bars':None,'blockers':['SECRET',{}]}]},
        KEYS[1]:{'last_quote_success':'SECRET','focus':{}},
        KEYS[2]:{'latest':{'phases':[{'phase':'SECRET','seconds':1},{'phase':'rvol','seconds':2}]}},
        KEYS[4]:{'rows':[{'symbol':'PG','regime':{},'blockers':True}]},KEYS[5]:None}
    result=summarize(controls,datetime.now(timezone.utc))
    assert result['symbols'][0]['strategy_volume']['missing_examples']==[]
    assert result['symbols'][0]['selection']['regime']=='UNKNOWN'
    assert result['unavailable_controls']==[KEYS[3],KEYS[5]]
    assert [row['phase'] for row in result['performance']['phases']]==['rvol']
    assert 'SECRET' not in json.dumps(result)


def test_series_retains_failed_sample_recovers_and_does_not_catch_up():
    now=datetime(2026,11,18,15,42,tzinfo=timezone.utc)
    elapsed=[0.0]
    starts=[]
    def read(config):
        starts.append(elapsed[0])
        elapsed[0]+={1:2,2:12,3:1}[len(starts)]
        if len(starts)==2:raise TimeoutError('SECRET host/password')
        return {KEYS[3]:{'heartbeat':(now+timedelta(seconds=elapsed[0])).isoformat()}}
    def sleep(seconds):elapsed[0]+=seconds
    output=io.StringIO()
    result=probe.capture('config',3,10,reader=read,clock=lambda:elapsed[0],
        wall=lambda:now+timedelta(seconds=elapsed[0]),sleeper=sleep,output=output)
    records=[json.loads(line) for line in output.getvalue().splitlines()]
    assert result==1 and starts==[0,10,22]
    assert [row['status'] for row in records[:-1]]==['OK','FAILED','OK']
    assert [row['db_read_seconds'] for row in records[:-1]]==[2,12,1]
    assert records[1]['error_type']=='TimeoutError'
    assert records[-1]['captured_samples']==2 and records[-1]['failed_samples']==1
    assert records[2]['read_finished_at']=='2026-11-18T15:42:23+00:00'
    assert 'SECRET' not in output.getvalue()


def test_series_uses_new_connection_after_read_error(monkeypatch):
    import pymysql
    monkeypatch.setenv('SQ_MYSQL_PASSWORD','synthetic-test-only')
    connections=[]
    class Connection:
        closed=False
        def cursor(self):return self
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,params=None):
            if params and len(connections)==1:raise TimeoutError('SECRET')
        def fetchall(self):return [(KEYS[0],'{}')]
        def close(self):self.closed=True
    def connect(**kwargs):
        connection=Connection()
        connections.append(connection)
        return connection
    monkeypatch.setattr(pymysql,'connect',connect)
    output=io.StringIO()
    result=probe.capture('config/sessionquant.realtime-paper.example.yaml',2,5,
        sleeper=lambda _:None,output=output)
    assert result==1 and len(connections)==2 and all(c.closed for c in connections)
    assert json.loads(output.getvalue().splitlines()[-1])['captured_samples']==1


def test_interrupt_retains_samples_and_marks_incomplete_capture():
    def sleep(_):raise KeyboardInterrupt
    output=io.StringIO()
    result=probe.capture('config',31,10,reader=lambda _: {},sleeper=sleep,output=output)
    records=[json.loads(line) for line in output.getvalue().splitlines()]
    assert result==130 and len(records)==2
    assert records[-1]['interrupted'] is True and records[-1]['captured_samples']==1


@pytest.mark.parametrize('args',[['--samples','0'],['--samples','62'],['--interval','4'],['--interval','61']])
def test_invalid_capture_bounds_do_not_connect(args,monkeypatch):
    monkeypatch.setattr(probe,'read_controls',lambda _:pytest.fail('Unexpected DB access'))
    with pytest.raises(SystemExit) as exc:probe.main(args)
    assert exc.value.code==2


def test_default_still_emits_one_json_object(monkeypatch,capsys):
    monkeypatch.setattr(probe,'read_controls',lambda _: {})
    assert probe.main([])==0
    result=json.loads(capsys.readouterr().out)
    assert result['record_type']=='sample' and result['sequence']==1

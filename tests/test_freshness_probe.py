from datetime import datetime, timezone
import json

from scripts.check_market_freshness import KEYS, read_controls, summarize


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

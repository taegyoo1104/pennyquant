from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

from sessionquant.market.candle_incidents import CandleIncidents, CONTROL, MAX_BYTES, MAX_EVENT_BYTES, encoded

NOW = datetime(2026, 11, 20, 16, 0, 40, tzinfo=timezone.utc)


class Repo:
    def __init__(self):
        self.saved = {}
        self.reads = []
        self.writes = []
        self.fail = False
    def get_control(self, name, default=None):
        self.reads.append(name)
        if self.fail:
            raise RuntimeError('PRIVATE connection')
        return deepcopy(self.saved.get(name, default))
    def set_control(self, name, value):
        self.writes.append((name, deepcopy(value)))
        self.saved[name] = deepcopy(value)


def inputs(late=True):
    latest = NOW.replace(second=0)-timedelta(minutes=2 if late else 0)
    return {'focus':[{'ticker':'AAPL'}], 'last_cycle_calls':10, 'cycle_call_budget':24,
        'candle_refresh_v1':{'AAPL':{'target_at':NOW.replace(second=0).isoformat(),
            'latest_closed_at':latest.isoformat(), 'received_at':NOW.isoformat(), 'attempts':2,
            'status':'AWAITING_PUBLICATION' if late else 'RECEIVED', 'private_body':'SECRET'}},
        'read_measurements':[{'source':'toss_candles','symbol':'AAPL','status':'ok',
            'started_at':(NOW-timedelta(seconds=2)).isoformat(),'observed_at':NOW.isoformat(),
            'duration_seconds':2, 'raw_response':'SECRET'}]}, {'elapsed_seconds':3,
                'phases':[{'phase':'api_http','seconds':2},{'phase':'db_query','seconds':.2}]}


def test_healthy_cycles_do_no_extra_database_work_and_closed_market_is_ignored():
    repo = Repo()
    recorder = CandleIncidents(repo)
    for _ in range(100):
        recorder.observe(*inputs(False), NOW)
    recorder.observe(*inputs(), NOW+timedelta(days=1))  # Saturday.
    assert not repo.reads and not repo.writes


def test_sample_preserves_timeline_and_limits_normal_suppressed_and_restart_io():
    repo = Repo()
    repo.saved['portfolio_selection_v1'] = {'evaluated_at':(NOW-timedelta(seconds=3)).isoformat(),
        'rows':[{'symbol':'AAPL','indicators':{'last_closed_at':(NOW-timedelta(minutes=5)).isoformat()},
                 'blockers':['minute_data_stale_or_missing'], 'private':'SECRET'}]}
    recorder = CandleIncidents(repo)
    state, perf = inputs()
    original = deepcopy(state)
    recorder.observe(state, perf, NOW)
    assert len(repo.writes) == 1 and len(repo.reads) == 2
    event = repo.saved[CONTROL]['events'][0]
    item = event['symbols'][0]
    assert item['request_started_at'] == (NOW-timedelta(seconds=2)).isoformat()
    assert item['decision_at'] == (NOW-timedelta(seconds=3)).isoformat()
    assert item['latest_closed_at'] == (NOW.replace(second=0)-timedelta(minutes=2)).isoformat()
    assert 'SECRET' not in json.dumps(event) and state == original
    for seconds in range(1,300):
        recorder.observe(state, perf, NOW+timedelta(seconds=seconds))
    assert len(repo.reads) == 2 and len(repo.writes) == 1
    restarted = CandleIncidents(repo)
    restarted.observe(state, perf, NOW+timedelta(seconds=299))
    assert len(repo.reads) == 3 and len(repo.writes) == 1
    restarted.observe(state, perf, NOW+timedelta(seconds=300))
    assert len(repo.writes) == 2 and len(repo.reads) == 4


def test_failed_recorder_is_throttled_without_propagating_or_logging_private_error(caplog):
    repo = Repo()
    repo.fail = True
    recorder = CandleIncidents(repo)
    for seconds in range(300):
        recorder.observe(*inputs(), NOW+timedelta(seconds=seconds))
    assert len(repo.reads) == 1 and not repo.writes
    assert 'PRIVATE' not in caplog.text


def test_archive_survives_restart_and_is_count_and_byte_bounded(monkeypatch):
    repo = Repo()
    event = {'at':NOW.isoformat(), 'symbols':[], 'padding':'x'*2800}
    monkeypatch.setattr(CandleIncidents, '_event', staticmethod(lambda s,p,n,r: {**event, 'at':n.isoformat()}))
    for i in range(400):
        CandleIncidents(repo).observe({}, {}, NOW+timedelta(minutes=5*i))
    saved = repo.saved[CONTROL]
    assert 1 < len(saved['events']) <= 96 and len(encoded(saved)) <= MAX_BYTES
    from sessionquant.database.serialization import dumps
    assert len(dumps(saved).encode('utf-8')) <= MAX_BYTES
    assert all(len(encoded(e)) <= MAX_EVENT_BYTES for e in saved['events'])
    assert saved['events'][-1]['at'] == (NOW+timedelta(minutes=5*399)).isoformat()
    assert len(repo.writes) == 400 and {name for name,_ in repo.writes} == {CONTROL}


def test_compact_diagnostic_is_not_copied_into_replay_and_loads_only_on_speed_tab():
    from sessionquant.research.journal import CONTROLS
    from sessionquant.dashboard.load_plan import read_plan
    assert CONTROL not in CONTROLS
    assert 'candle_incidents' in read_plan('시장·종목',panel='처리 속도')[0]
    for page,panel in [('대시보드','live'),('시장·종목','시세·품질'),('시장·종목','매수 판단')]:
        assert 'candle_incidents' not in read_plan(page,panel=panel)[0]


def test_incident_ui_shows_sample_and_download_without_reading_database():
    from streamlit.testing.v1 import AppTest
    repo = Repo()
    CandleIncidents(repo).observe(*inputs(), NOW)
    app = AppTest.from_string('from sessionquant.dashboard.candle_incidents import render_incidents\n'
                             f'render_incidents({repo.saved[CONTROL]!r})').run()
    assert not app.exception
    assert app.dataframe[0].value.iloc[0]['대표 종목'] == 'AAPL'
    assert len(app.get('download_button')) == 1

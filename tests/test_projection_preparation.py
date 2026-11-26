from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from sessionquant.database import llm_attempt_index as llm, pipeline_summary as pipeline
from sessionquant.database.mysql_repository import MySQLRepositoryError

DEPLOY = Path(__file__).parents[1] / 'deploy/gcp'
sys.path.insert(0, str(DEPLOY))
try:
    spec = importlib.util.spec_from_file_location('projection_preparation', DEPLOY / 'prepare_db_projections.py')
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
finally:
    sys.path.pop(0)
NOW = datetime(2026, 11, 26, 8, tzinfo=timezone.utc)


class Repo:
    backend = 'mysql'
    namespace = 'paper_fixture'
    config = SimpleNamespace(environment='PAPER_LOCAL')
    _in_transaction = False

    def __init__(self):
        self.controls = {'trade_control_state': {'mode': 'paper', 'running': False, 'status': 'OFFLINE', 'armed': False}}
        self.state = {'environment': 'PAPER_LOCAL', 'broker': {'positions': {}, 'orders': {}, 'exit_intents': {}}}
        self.active = False
        self.readonly = (0, 0)
        self.calls = []
        self.seconds = 0
        self.after = []
        self.page_fail = False

    def _execute(self, sql, *args, **kwargs):
        self.calls.append(sql)
        if sql.startswith('SET SESSION'):
            return
        assert self.active
        if '@@GLOBAL.read_only' in sql:
            return self.readonly
        if 'COUNT(*)' in sql:
            return (0,)
        if 'FROM pq_pipeline_status' in sql:
            return []
        raise AssertionError(sql)

    @contextmanager
    def engine_lease(self):
        self.active = True
        try:
            yield
        finally:
            self.active = False

    @contextmanager
    def transaction(self):
        assert self.active
        yield

    def get_control(self, key, default=None):
        assert self.active
        return self.controls.get(key, default)

    def set_control(self, key, value):
        assert self.active
        self.controls[key] = dict(value)

    def load_state(self):
        assert self.active
        return self.state


@pytest.fixture
def fixture(monkeypatch):
    repo = Repo()
    def install(r):
        assert r.active
        r.calls.append('install')
    monkeypatch.setattr(pipeline, 'install', install)
    monkeypatch.setattr(pipeline, 'available', lambda r: True)
    monkeypatch.setattr(llm, 'install', install)
    monkeypatch.setattr(llm, 'trigger_signature', lambda r: 'current')
    def page(r, *, after, limit):
        r.after.append(after)
        if r.page_fail:
            raise MySQLRepositoryError(Exception(3024, 'private'), 'SELECT')
        r.seconds += 1
        return {'after': after + 10, 'rows': 10, 'done': after >= 10}
    monkeypatch.setattr(llm, 'backfill_page', page)
    def prove(r, **kwargs):
        r.set_control(llm.CONTROL, {'version': 1, 'namespace': r.namespace,
            'trigger_signature': 'current', 'complete': True})
        return True
    monkeypatch.setattr(llm, 'prove_coverage', prove)
    return repo


def run(repo, **kwargs):
    return prep.prepare(repo, now=NOW, bounds=(), clock=lambda: repo.seconds, **kwargs)


def test_resumable_bounded_preparation_then_complete_proof_fastskip(fixture):
    repo = fixture
    first = run(repo, seconds=1)
    assert first['status'] == 'PENDING' and repo.after == [0]
    assert repo.controls[prep.CURSOR]['after'] == 10
    second = run(repo, seconds=10)
    assert second['status'] == 'COMPLETE' and repo.after == [0, 10]
    assert run(repo)['llm']['reused_proof'] is True
    assert repo.after == [0, 10] and not repo.active


@pytest.mark.parametrize('change', [{'namespace': 'other'}, {'trigger_signature': 'old'}, {'version': True}])
def test_cursor_identity_change_restarts_from_zero(fixture, change):
    repo = fixture
    repo.controls[prep.CURSOR] = {'version': 1, 'namespace': repo.namespace, 'trigger_signature': 'current',
                                  'after': 999, 'phase': 'VERIFY', **change}
    assert run(repo)['status'] == 'COMPLETE'
    assert repo.after[0] == 0


def test_restart_invalidates_old_proof_and_cursor_before_failing_first_page(fixture):
    repo = fixture
    assert run(repo)['status'] == 'COMPLETE'
    repo.after.clear()
    repo.page_fail = True
    failed = run(repo, restart=True)
    assert failed['status'] == 'PENDING'
    assert repo.controls[llm.CONTROL]['complete'] is False
    assert repo.controls[prep.CURSOR]['after'] == 0
    repo.page_fail = False
    assert run(repo)['status'] == 'COMPLETE'
    assert repo.after == [0, 0, 10]


def test_restart_invalidates_before_pipeline_timeout(fixture, monkeypatch):
    repo = fixture
    assert run(repo)['status'] == 'COMPLETE'
    execute = repo._execute
    def keys(sql, *args, **kwargs):
        if 'FROM pq_pipeline_status' in sql:
            return [(7,)]
        return execute(sql, *args, **kwargs)
    monkeypatch.setattr(repo, '_execute', keys)
    def timeout(*args, **kwargs):
        raise MySQLRepositoryError(Exception(3024, 'private'), 'SELECT')
    monkeypatch.setattr(pipeline, 'backfill_page', timeout)
    result = run(repo, restart=True)
    assert result['status'] == 'PENDING' and result['phase'] == 'PIPELINE'
    assert repo.controls[llm.CONTROL]['complete'] is False
    assert repo.controls[prep.CURSOR]['after'] == 0


@pytest.mark.parametrize('bad', ['readonly', 'positions', 'running'])
def test_guard_rejects_before_schema_or_cursor_writes(fixture, bad):
    repo = fixture
    if bad == 'readonly': repo.readonly = (1, 1)
    if bad == 'positions': repo.state['broker']['positions'] = {'AAPL': {}}
    if bad == 'running': repo.controls['trade_control_state']['running'] = True
    result = run(repo)
    assert result['status'] == 'FAILED'
    assert 'install' not in repo.calls and prep.CURSOR not in repo.controls
    assert not repo.active


def test_incomplete_proof_never_enables_fast_path(fixture, monkeypatch):
    monkeypatch.setattr(llm, 'prove_coverage', lambda *args, **kwargs: False)
    result = run(fixture)
    assert result['status'] == 'PENDING' and result['reason'] == 'PROOF_INCOMPLETE'
    assert fixture.controls[llm.CONTROL]['complete'] is False

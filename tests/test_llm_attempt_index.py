from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace

import pytest

from sessionquant.database import llm_attempt_index as index


def trigger_rows():
    return sorted([(name, event, 'AFTER', 'pq_llm_calls', body, datetime(2026, 11, 26),
        'STRICT_TRANS_TABLES', 'utf8mb4', 'utf8mb4_bin', 'utf8mb4_bin')
        for name, event, body in zip(index.TRIGGERS, ('UPDATE', 'INSERT'), index.bodies())])


@pytest.mark.parametrize('column,value', [(1, 'DELETE'), (2, 'BEFORE'), (3, 'other'), (4, 'BEGIN DO 1; END'), (5, None)])
def test_trigger_identity_requires_correct_body_and_installation(column, value):
    rows = trigger_rows()
    repo = SimpleNamespace(config=SimpleNamespace(database='fixture'), _execute=lambda *a, **kw: rows)
    original = index.trigger_signature(repo)
    assert original
    rows[0] = tuple(value if i == column else x for i, x in enumerate(rows[0]))
    assert index.trigger_signature(repo) is None


def test_recreated_trigger_invalidates_old_coverage():
    rows = trigger_rows()
    repo = SimpleNamespace(config=SimpleNamespace(database='fixture'), _execute=lambda *a, **kw: rows)
    signature = index.trigger_signature(repo)
    receipt = {'version': 1, 'namespace': 'a', 'complete': True, 'trigger_signature': signature}
    assert index.covered(receipt, 'a', signature)
    rows[0] = (*rows[0][:5], datetime(2026, 11, 27), *rows[0][6:])
    assert not index.covered(receipt, 'a', index.trigger_signature(repo))
    assert not index.covered(receipt, 'b', signature)
    assert not index.covered({**receipt, 'version': True}, 'a', signature)
    assert not index.covered({**receipt, 'complete': False}, 'a', signature)
    assert not index.covered(None, 'a', signature)


def test_install_permission_failure_keeps_legacy_usable():
    def denied(*args, **kwargs):
        error = RuntimeError('fixture permission')
        error.code = 1142
        raise error
    index.install(SimpleNamespace(_execute=denied))


def test_missing_coverage_keeps_untruncated_legacy_predicate(monkeypatch):
    calls = []
    def execute(sql, params, **kwargs):
        calls.append((sql, params))
        return [('second',), ('first',)]
    repo = SimpleNamespace(namespace='mine', transaction=nullcontext, get_control=lambda _: None,
                           _execute=execute, _decode=lambda v: v)
    monkeypatch.setattr(index, 'trigger_signature', lambda _: 'checked-signature')
    assert index.recent(repo, 500) == ['first', 'second']
    assert len(calls) == 1 and calls[0][1] == ('mine', 500)
    assert '5000' not in calls[0][0] and "JSON_EXTRACT(payload,'$.network_attempted')" in calls[0][0]


def test_missing_false_projection_cannot_issue_coverage(monkeypatch):
    receipts = []
    repo = SimpleNamespace(namespace='mine', set_control=lambda k, v: receipts.append(v))
    monkeypatch.setattr(index, 'trigger_signature', lambda _: 'checked-signature')
    def execute(sql, *args, **kwargs):
        if 'LEFT JOIN' in sql:
            return (17,)
        return [(17, datetime(2026, 11, 26))]
    repo._execute = execute
    assert index.prove_coverage(repo) is False
    assert all(row['complete'] is False for row in receipts)


def test_coverage_time_limit_never_enables_fast_path(monkeypatch):
    receipts = []
    repo = SimpleNamespace(namespace='mine', set_control=lambda k, v: receipts.append(v))
    monkeypatch.setattr(index, 'trigger_signature', lambda _: 'checked-signature')
    ticks = iter([0, 31])
    assert index.prove_coverage(repo, clock=lambda: next(ticks)) is False
    assert receipts == [{'namespace': 'mine', 'version': 1, 'complete': False}]

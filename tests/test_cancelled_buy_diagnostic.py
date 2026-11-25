from datetime import datetime, timedelta, timezone
import json

from scripts.diagnose_cancelled_buys import compare, diagnose

AT = datetime(2026, 11, 23, 16, 25, 43, 207452, tzinfo=timezone.utc)
ORDER = {'order_id': 'sq-buy-example', 'symbol': 'SPCX', 'side': 'buy', 'status': 'cancelled',
         'submitted_at': '2026-11-23T16:25:32.755296+00:00', 'cancel_at': AT.isoformat(),
         'unfilled_reason': 'cancelled'}


def frame(at=AT, allowed=False, **extra):
    return {'kind': 'engine_batch', 'decision_at': at.isoformat(), 'quote_allowed': allowed,
            'manual_halt': False, **extra}


def test_correlates_exact_cancel_frame_and_later_recovery():
    result = compare(ORDER, [frame(AT + timedelta(seconds=2.029699), True), frame(),
                             frame(AT - timedelta(seconds=3), True)])
    assert result['wait_seconds'] == 10.452
    assert result['evidence'] == 'MATCHED' and result['quote_allowed'] is False
    assert result['recovered_after_seconds'] == 2.03


def test_missing_or_nearby_frame_is_not_evidence_of_the_cancellation():
    for frames in ([], [frame(AT + timedelta(microseconds=1))], [frame(kind='news_analysis')]):
        assert compare(ORDER, frames)['evidence'] == 'NO_MATCHING_FRAME'
    assert compare(ORDER, [frame(), frame()])['evidence'] == 'AMBIGUOUS_FRAMES'


def test_keeps_null_checks_and_concurrent_halt_distinct_from_false():
    missing = compare(ORDER, [frame(allowed=None)])
    assert missing['quote_allowed'] is None and 'recovered_after_seconds' not in missing
    halted = compare(ORDER, [frame(manual_halt=True, news_risk={'block_entries': True})])
    assert halted['manual_halt'] is True and halted['news_risk']['block_entries'] is True
    assert halted['recovered_after_seconds'] is None


class Repo:
    namespace = 'paper_test'

    def __init__(self, fail=False):
        self.queries = []
        self.fail = fail

    def _execute(self, sql, params=(), *, fetch=None):
        self.queries.append((sql, params))
        assert sql.lstrip().startswith('SELECT') and fetch == 'all'
        assert 'MAX_EXECUTION_TIME(1500)' in sql and self.namespace in params
        if 'FROM pq_orders' in sql:
            return [(1,)] if 'JSON_OBJECT' not in sql else [(json.dumps(ORDER),)]
        if 'JSON_OBJECT' not in sql:
            assert 'captured_at >= %s AND captured_at < %s' in sql
            return [(2,), (3,)]
        if self.fail:
            raise RuntimeError('do not expose a driver message or credentials')
        assert 'FORCE INDEX (PRIMARY)' in sql and 'seq IN' in sql
        return [(json.dumps(frame()),), (json.dumps(frame(AT + timedelta(seconds=2), True)),)]


def test_queries_only_bounded_keys_and_projected_fields_and_returns_read_only_report():
    repo = Repo()
    result = diagnose(repo)
    assert result['read_only'] is True and result['status'] == 'COMPLETE'
    assert len(repo.queries) == 4
    assert result['orders'][0]['recovered_after_seconds'] == 2
    assert result['cancelled_in_scanned_orders'] == 1


def test_query_failure_is_unknown_not_clear_and_does_not_leak_driver_text():
    result = diagnose(Repo(fail=True))
    assert result['status'] == 'PARTIAL'
    assert result['orders'][0]['evidence'] == 'NO_MATCHING_FRAME'
    assert result['orders'][0]['scan_error'] == 'RuntimeError'
    assert 'credentials' not in json.dumps(result)


def test_wall_budget_stops_without_starting_a_query():
    moments = iter((0, 31))
    repo = Repo()
    result = diagnose(repo, clock=lambda: next(moments))
    assert result['status'] == 'PARTIAL' and not repo.queries
    assert result['errors'][0]['type'] == 'TimeoutError'

"""Bounded, read-only comparison of cancelled Paper buys and recorded engine checks.

Run inside the existing app image with ``python -u - < this_file``. No market or
AI requests, writes, initialization, or full research-payload downloads occur.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

MAX_ORDERS = 100
MAX_CANCELLED = 30
MAX_FRAME_KEYS = 80
QUERY_MS = 1500
WALL_SECONDS = 30


def stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def decode(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def compare(order, frames):
    cancelled = stamp(order.get('cancel_at'))
    submitted = stamp(order.get('submitted_at'))
    result = {key: order.get(key) for key in ('order_id', 'symbol', 'cancel_at', 'unfilled_reason')}
    result['wait_seconds'] = round((cancelled - submitted).total_seconds(), 3) if cancelled and submitted else None
    exact = [f for f in frames if f.get('kind') == 'engine_batch'
             and cancelled is not None and stamp(f.get('decision_at')) == cancelled]
    result['evidence'] = 'NO_MATCHING_FRAME'
    if len(exact) != 1:
        if len(exact) > 1:
            result['evidence'] = 'AMBIGUOUS_FRAMES'
        return result
    frame = exact[0]
    result.update(evidence='MATCHED', quote_allowed=frame.get('quote_allowed'),
                  manual_halt=frame.get('manual_halt'), news_risk=frame.get('news_risk'),
                  reference_blocked=frame.get('reference_blocked'), operator_exit=frame.get('operator_exit'))
    if frame.get('quote_allowed') is False:
        recovered = [stamp(f.get('decision_at')) for f in frames
                     if f.get('kind') == 'engine_batch' and f.get('quote_allowed') is True
                     and stamp(f.get('decision_at')) is not None and stamp(f['decision_at']) > cancelled]
        result['recovered_after_seconds'] = round((min(recovered) - cancelled).total_seconds(), 3) if recovered else None
    return result


def diagnose(repo, *, clock=time.monotonic):
    deadline = clock() + WALL_SECONDS
    result = {'read_only': True, 'status': 'COMPLETE', 'order_limit': MAX_ORDERS,
              'cancelled_limit': MAX_CANCELLED, 'recovery_window_seconds': 10, 'orders': [], 'errors': []}

    def read(sql, params=()):
        if clock() >= deadline:
            raise TimeoutError('diagnostic_budget')
        return repo._execute(sql, params, fetch='all')

    try:
        keys = read(f'SELECT /*+ MAX_EXECUTION_TIME({QUERY_MS}) */ seq FROM pq_orders '
                    'FORCE INDEX (ix_namespace_seq) WHERE namespace=%s ORDER BY seq DESC LIMIT %s',
                    (repo.namespace, MAX_ORDERS + 1))
        result['order_scan_truncated'] = len(keys) > MAX_ORDERS
        orders = []
        for start in range(0, min(len(keys), MAX_ORDERS), 10):
            page = [row[0] for row in keys[start:min(start + 10, MAX_ORDERS)]]
            marks = ','.join(['%s'] * len(page))
            rows = read(f'''SELECT /*+ MAX_EXECUTION_TIME({QUERY_MS}) */ JSON_OBJECT(
                'order_id', record_id,
                'symbol', COALESCE(JSON_EXTRACT(payload,'$.intent.symbol'),JSON_EXTRACT(payload,'$.symbol')),
                'side', COALESCE(JSON_EXTRACT(payload,'$.intent.side'),JSON_EXTRACT(payload,'$.side')),
                'status', JSON_EXTRACT(payload,'$.status'),
                'submitted_at', JSON_EXTRACT(payload,'$.submitted_at'),
                'cancel_at', JSON_EXTRACT(payload,'$.cancel_requested_at'),
                'unfilled_reason', JSON_EXTRACT(payload,'$.unfilled_reason'))
                FROM pq_orders FORCE INDEX (PRIMARY) WHERE namespace=%s AND seq IN ({marks})''',
                (repo.namespace, *page))
            orders.extend(decode(row[0]) for row in rows)
        cancelled = sorted((o for o in orders if o.get('side') == 'buy' and o.get('status') == 'cancelled'),
                           key=lambda o: stamp(o.get('cancel_at')) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        result['cancelled_in_scanned_orders'] = len(cancelled)
        result['cancelled_scan_truncated'] = len(cancelled) > MAX_CANCELLED
        for order in cancelled[:MAX_CANCELLED]:
            frames = []
            item = compare(order, frames)
            result['orders'].append(item)
            at = stamp(order.get('cancel_at'))
            if at is None:
                item['evidence'] = 'CANCEL_TIME_NOT_RECORDED'
                continue
            try:
                keys = read(f'SELECT /*+ MAX_EXECUTION_TIME({QUERY_MS}) */ seq FROM pq_research_events '
                            'FORCE INDEX (ix_namespace_captured) WHERE namespace=%s '
                            'AND captured_at >= %s AND captured_at < %s ORDER BY captured_at,seq LIMIT %s',
                            (repo.namespace, (at - timedelta(seconds=1)).replace(tzinfo=None),
                             (at + timedelta(seconds=10)).replace(tzinfo=None), MAX_FRAME_KEYS + 1))
                item['frame_scan_truncated'] = len(keys) > MAX_FRAME_KEYS
                symbol = json.dumps(str(order.get('symbol')), ensure_ascii=False)
                for start in range(0, min(len(keys), MAX_FRAME_KEYS), 5):
                    page = [row[0] for row in keys[start:min(start + 5, MAX_FRAME_KEYS)]]
                    marks = ','.join(['%s'] * len(page))
                    rows = read(f'''SELECT /*+ MAX_EXECUTION_TIME({QUERY_MS}) */ JSON_OBJECT(
                        'kind', JSON_EXTRACT(payload,'$.kind'),
                        'decision_at', JSON_EXTRACT(payload,'$.data.decision_at'),
                        'quote_allowed', JSON_EXTRACT(payload,%s),
                        'manual_halt', JSON_EXTRACT(payload,'$.data.manual_halt'),
                        'news_risk', JSON_EXTRACT(payload,%s),
                        'reference_blocked', JSON_EXTRACT(payload,'$.data.portfolio_selection.macro_reference_risk.block_entries'),
                        'operator_exit', JSON_EXTRACT(payload,%s))
                        FROM pq_research_events FORCE INDEX (PRIMARY) WHERE namespace=%s AND seq IN ({marks})''',
                        (f'$.data.quote_entry_allowed.{symbol}', f'$.data.news_contexts.{symbol}.context',
                         f'$.data.operator_exits.{symbol}', repo.namespace, *page))
                    frames.extend(decode(row[0]) for row in rows)
                item.update(compare(order, frames))
            except Exception as exc:
                item.update(compare(order, frames))
                item['scan_error'] = type(exc).__name__
                result['errors'].append({'order_id': order.get('order_id'), 'type': type(exc).__name__,
                                         'code': getattr(exc, 'code', None)})
                result['status'] = 'PARTIAL'
                if isinstance(exc, TimeoutError):
                    break
        if len(result['orders']) < min(len(cancelled), MAX_CANCELLED):
            result['status'] = 'PARTIAL'
    except Exception as exc:
        result['status'] = 'PARTIAL'
        result['errors'].append({'type': type(exc).__name__, 'code': getattr(exc, 'code', None)})
    return result


def main():
    from sessionquant.session_config import load_session_config
    from sessionquant.database.repository import Repository
    config = load_session_config(os.environ.get('SQ_SESSION_CONFIG') or 'config/sessionquant.realtime-paper.example.yaml')
    os.environ['SQ_ENVIRONMENT'] = config.environment
    if config.database_backend != 'mysql':
        raise ValueError('이 진단은 운영 MySQL 전용입니다.')
    repo = Repository(config.db_path, backend=config.database_backend, initialize=False)
    try:
        report = diagnose(repo)
        report['configured_order_ttl_seconds'] = config.portfolio.get('order_ttl_seconds', 60)
        print('RESULT ' + json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        repo.close()


if __name__ == '__main__':
    main()

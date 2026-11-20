"""Review a logical Paper backup in an isolated disposable MySQL, without starting the app.

Only summary fields are exported. No credentials, raw news, account identifiers, SQL,
or unrestricted exception messages are included in the report. Requires Python 3.10+
and Docker. No third party Python packages or GCP VM access are needed.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
TABLES = ('pipeline_status', 'orders', 'order_intents', 'executions', 'signals', 'system_logs', 'llm_calls', 'research_events')
CONTROLS = ('startup_data_warmup_v1', 'watchlist_dashboard_v1', 'watchlist_history_selection_v1',
            'trade_control_state', 'watchlist_scan_state_v1')
ENTRY_CODES = frozenset('''quote_stale quote_missing quote_time_or_symbol_invalid quote_halted quote_depth_missing
    spread_too_wide closed_bars_missing_or_stale technical_history_incomplete insufficient_liquidity
    relative_volume_unknown_or_low minute_history_incomplete_or_stale market_bearish market_unknown market_neutral
    universe_unknown_stale_or_nonmember instrument_eligibility_unknown instrument_symbol_mismatch
    instrument_eligibility_stale_or_future instrument_eligibility_expired eligibility_unverified exchange_unverified
    paper_listing_evidence_missing paper_listing_evidence_stale_or_missing paper_listing_not_active
    paper_known_restriction_tradable paper_known_restriction_fractional paper_known_restriction_halted
    paper_known_restriction_corporate_action confirmed_news_entry_restriction market_reference_not_trading_candidate
    candidate_bar_time_mismatch candidate_quote_stale_or_unknown candidate_quote_time_mismatch technical_signal_absent
    signal_already_consumed trading_not_started_or_halted duplicate_symbol_signal focus_refresh_pending focus_time_expired
    focus_time_unverified focus_unknown_or_stale history_not_in_daily_operating_watchlist outside_market_cap_watchlist
    split_volume_comparability_unknown historical_same_minute_coverage_incomplete current_session_coverage_incomplete
    volume_source_or_adjustment_mismatch invalid_or_zero_history_volume current_closed_interval_missing rvol_calendar_unknown
    minute_gaps minute_data_stale_or_missing bar_revisions_unresolved no_completed_regular_minute
    final_candidate_limit preferred_position_target_reached add_conditions_not_met exit_stop_unverified
    outside_entry_window daily_loss_limit pending_order_or_exit position_limit position_exists position_count_exceeded
    risk_budget_insufficient allocation_or_cash_limit invalid_stop invalid_add_stop add_would_average_down
    account_equity_nonpositive account_order_state_unknown existing_risk_limit_breached gross_exposure_exceeded
    symbol_weight_exceeded symbol_risk_budget_exceeded high_index_correlation_limit same_sector_limit
    sector_diversification_unknown index_correlation_unknown market_not_bullish confirmed_adverse_news
    strategy_outside_regular_session strategy_market_direction_blocked strategy_bar_identity_or_time_invalid
    strategy_indicator_cutoff_mismatch strategy_indicators_unavailable strategy_five_minute_history_incomplete
    strategy_closed_bar_stale strategy_price_extended_or_invalid strategy_recent_volume_below_minimum
    strategy_fifteen_minute_uptrend_missing strategy_price_above_support_missing strategy_recent_high_breakout_missing
    strategy_breakout_volume_missing strategy_first_pullback_missing strategy_second_pullback_missing
    strategy_pullback_depth_invalid strategy_pullback_support_missing strategy_rebound_above_vwap_missing
    strategy_rebound_high_breakout_missing strategy_rebound_volume_missing strategy_stop_price_invalid
    strategy_opening_range_breakout_missing strategy_opening_range_incomplete strategy_opening_range_not_finished
    strategy_previous_vwap_unavailable strategy_price_above_ema_missing strategy_price_above_vwap_missing
    strategy_vwap_reclaim_missing'''.split())
ERROR_SOURCES = frozenset('''toss_stocks toss_prices toss_rankings toss_candles toss_orderbook
    market_cap market_provider startup_warmup gdelt sec rss fred'''.split())


def obj(value):
    return value if isinstance(value, dict) else {}


def entries(value):
    return [row for row in value[:100] if isinstance(row, dict)] if isinstance(value, list) else []


def numeric(value):
    try: return value if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError: return None


def entry_codes(value):
    return sorted({v if isinstance(v, str) and v in ENTRY_CODES else 'OTHER'
                   for v in value[:100]}) if isinstance(value, list) else []


def tally(target, values):
    for value in values: target[value] = target.get(value, 0) + 1


def entry_group():
    return {'evaluations': 0, 'signal_present': 0, 'eligible': 0, 'quality_present': 0,
            'strategy_rvol_present': 0, 'calculated_rvol_present': 0,
            'decision_blockers': {}, 'selection_blockers': {}, 'allocation_reasons': {}, 'strategy_wait': {}, 'volume_reasons': {},
            'regimes': {}, 'manual_halt': {}, 'running': {}, 'by_symbol': {}}


def entry_observation(decision, quality, selection, at):
    indicators = obj(decision.get('indicators'))
    volume, bars, strategy_bars = [obj(quality.get(k)) for k in ('rvol', 'bars', 'strategy_bars')]
    return {'at_kst': korean(at), 'eligible': decision.get('eligible') is True,
        'signal_present': bool(obj(decision.get('signal'))),
        'regime': decision.get('regime') if decision.get('regime') in ('BULLISH', 'BEARISH', 'NEUTRAL') else 'UNKNOWN',
        'quality_present': bool(quality),
        'decision_blockers': entry_codes(decision.get('blockers')),
        'selection_blockers': entry_codes(selection.get('blockers')),
        'allocation_reasons': entry_codes([selection['allocation_reason']]) if selection.get('allocation_reason') else [],
        'strategy_wait': entry_codes(indicators.get('strategy_diagnostics')),
        'volume_reasons': entry_codes(volume.get('reasons')),
        'strategy_rvol': numeric(quality.get('strategy_rvol', indicators.get('relative_volume'))),
        'calculated_rvol': numeric(volume.get('calculated_value')),
        'volume_cutoff_kst': korean(volume.get('cutoff_at')),
        'volume_observed_kst': korean(volume.get('observed_at')),
        'required_sessions': numeric(volume.get('required_sessions')),
        'complete_sessions': numeric(volume.get('complete_sessions')),
        **{k: numeric(volume.get(k)) for k in ('current_expected_minutes', 'current_observed_minutes',
            'current_missing_minutes', 'current_missing_interior_minutes', 'current_missing_tail_minutes')},
        'quote_at_kst': korean(quality.get('quote_at') or selection.get('quote_at')),
        'quote_received_at_kst': korean(quality.get('quote_received_at') or selection.get('quote_received_at')),
        'minute_last_closed_kst': korean(bars.get('last_closed_at')),
        'strategy_cutoff_kst': korean(strategy_bars.get('cutoff_at')),
        'minute_reasons': entry_codes(bars.get('reasons')),
        'strategy_bar_reasons': entry_codes(strategy_bars.get('reasons'))}


def add_entry(group, symbol, row, body):
    group['evaluations'] += 1
    for flag in ('signal_present', 'eligible', 'quality_present'): group[flag] += int(row[flag])
    group['strategy_rvol_present'] += int(row['strategy_rvol'] is not None)
    group['calculated_rvol_present'] += int(row['calculated_rvol'] is not None)
    for key in ('decision_blockers', 'selection_blockers', 'allocation_reasons', 'strategy_wait', 'volume_reasons'):
        tally(group[key], row[key])
    tally(group['regimes'], [row['regime']])
    tally(group['manual_halt'], [str(body.get('manual_halt')) if type(body.get('manual_halt')) is bool else 'UNKNOWN'])
    running = obj(body.get('control_state')).get('running')
    tally(group['running'], [str(running) if type(running) is bool else 'UNKNOWN'])
    if symbol not in group['by_symbol'] and len(group['by_symbol']) >= 100: symbol = 'OTHER'
    stock = group['by_symbol'].setdefault(symbol, {'evaluations': 0, 'first_kst': row['at_kst'],
        'last_kst': row['at_kst'], 'strategy_rvol_present': 0, 'decision_blockers': {},
        'selection_blockers': {}, 'allocation_reasons': {}, 'strategy_wait': {}, 'volume_reasons': {}, 'samples': []})
    stock['evaluations'] += 1
    stock['first_kst'] = min(stock['first_kst'], row['at_kst'])
    stock['last_kst'] = max(stock['last_kst'], row['at_kst'])
    stock['strategy_rvol_present'] += int(row['strategy_rvol'] is not None)
    for key in ('decision_blockers', 'selection_blockers', 'allocation_reasons', 'strategy_wait', 'volume_reasons'):
        tally(stock[key], row[key])
    stock['samples'].append(row)
    if len(stock['samples']) > 6: del stock['samples'][2]  # First two and latest four retained observations.


def stamp(value):
    try:
        value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def korean(value):
    value = stamp(value)
    return value.astimezone(KST).isoformat() if value else None


def code(value):
    value = str(value)
    return value if re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', value) and '://' not in value else 'REDACTED'


def symbols(values):
    return sorted({v for v in values if isinstance(v, str) and re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}', v)})


def reasons(values):
    counts = collections.Counter()
    for value in values:
        if isinstance(value, str): counts[code(value)] += 1
    return dict(counts)


def classify_error(row):
    text = str(row.get('message', '')) + ' ' + str(row.get('detail', ''))
    lowered = text.lower()
    for pattern, label in [('no space left', 'storage_full'), ('2006', 'mysql_disconnected'),
                           ('2013', 'mysql_connection_lost'), ('timeout', 'timeout'),
                           ('429', 'rate_limited'), ('403', 'access_denied')]:
        if pattern in lowered: return label
    return code(row.get('error_type') or 'message_omitted')


class Review:
    def __init__(self, since, until, tables, entry_since=None, entry_until=None):
        self.since, self.until = since, until
        self.entry_since, self.entry_until = entry_since, entry_until
        self.tables, self.spaces = set(tables), {}

    def space(self, name):
        key = code(name)
        return self.spaces.setdefault(key, {
            'counts_in_window': {}, 'records_without_time': {}, 'time_basis_counts': {}, 'latest_controls': {},
            'focus_snapshots': [], 'activity': {}, 'log_summary': {}, 'log_samples': [],
            'warnings_by_source': {},
            'entry_review': {'all_in_window': entry_group(),
                'selected_entry_window': entry_group() if self.entry_since else None,
                'batches_missing_decision_details': 0, 'risk_decisions_all_in_window': {}},
            'research': {'kinds': {}, 'engine_first_kst': None, 'engine_last_kst': None,
                         'engine_batches': 0, 'running_batches': 0, 'decision_count': 0,
                         'scope_blockers': {}},
        })

    def consume(self, row):
        item = self.space(row['namespace'])
        data = row.get('data') or {}
        kind = row['kind']
        if kind == 'controls':
            self.control(item, row['name'], data, row.get('captured_at'))
            return
        time_keys = {'research_events': ('available_at',), 'pipeline_status': ('known_at', 'timestamp', 'observed_at'),
                     'orders': ('created_at', 'timestamp'), 'order_intents': ('created_at', 'timestamp')}
        keys = time_keys.get(kind, ('timestamp', 'received_at', 'created_at', 'attempted_at', 'analyzed_at'))
        payload_at = next((stamp(data[k]) for k in keys if stamp(data.get(k))), None)
        at = payload_at or stamp(row.get('captured_at'))
        if at is None:
            item['records_without_time'][kind] = item['records_without_time'].get(kind, 0) + 1
            return
        if not self.since <= at < self.until: return
        basis = kind + (':payload_time' if payload_at else ':database_capture_time')
        item['time_basis_counts'][basis] = item['time_basis_counts'].get(basis, 0) + 1
        item['counts_in_window'][kind] = item['counts_in_window'].get(kind, 0) + 1
        if kind == 'pipeline_status' and data.get('kind') == 'watchlist_focus':
            names = symbols(x.get('ticker', x.get('symbol')) for x in data.get('items', []) if isinstance(x, dict))
            item['focus_snapshots'].append({'at_kst': korean(at), 'count': len(names), 'symbols': names,
                                            'data_status': code(data.get('data_status', 'UNKNOWN'))})
        elif kind == 'research_events':
            research = item['research']; subtype = code(data.get('kind'))
            research['kinds'][subtype] = research['kinds'].get(subtype, 0) + 1
            if subtype == 'engine_batch':
                body = data.get('data') or {}
                research['engine_batches'] += 1
                when = korean(at)
                research['engine_first_kst'] = min(research['engine_first_kst'] or when, when)
                research['engine_last_kst'] = max(research['engine_last_kst'] or when, when)
                research['running_batches'] += int((body.get('control_state') or {}).get('running') is True)
                research['decision_count'] += int(body.get('decision_count') or 0)
                for blockers in (body.get('scope_blockers') or {}).values():
                    for reason, count in reasons(blockers or []).items():
                        research['scope_blockers'][reason] = research['scope_blockers'].get(reason, 0) + count
                detail = item['entry_review']
                if not isinstance(body.get('decisions'), list): detail['batches_missing_decision_details'] += 1
                quality = {r['symbol']: r for r in entries(body.get('quality_rows')) if isinstance(r.get('symbol'), str)}
                selected = {r['symbol']: r for r in entries(obj(body.get('portfolio_selection')).get('rows'))
                            if isinstance(r.get('symbol'), str)}
                for decision in entries(body.get('decisions')):
                    ticker = symbols([decision.get('symbol')])
                    if not ticker: continue
                    symbol = ticker[0]
                    row = entry_observation(decision, {**obj(obj(decision.get('indicators')).get('data_quality')),
                                                       **quality.get(symbol, {})},
                                            selected.get(symbol, {}), at)
                    add_entry(detail['all_in_window'], symbol, row, body)
                    if self.entry_since and self.entry_since <= at < self.entry_until:
                        add_entry(detail['selected_entry_window'], symbol, row, body)
        elif kind == 'pipeline_status' and data.get('kind') == 'risk_decision':
            label = 'APPROVED' if data.get('approved') is True else 'REJECTED'
            tally(item['entry_review']['risk_decisions_all_in_window'], [label+':'+v for v in entry_codes([data.get('reason')])])
        elif kind == 'system_logs':
            level = code(data.get('level', 'UNKNOWN'))
            label = classify_error(data) if level in {'ERROR', 'WARNING', 'CRITICAL'} else 'informational'
            key = level + ':' + label
            item['log_summary'][key] = item['log_summary'].get(key, 0) + 1
            if level in {'ERROR', 'WARNING', 'CRITICAL'}:
                source = data.get('source')
                source = source if isinstance(source, str) and source in ERROR_SOURCES else 'UNKNOWN'
                tally(item['warnings_by_source'], [source+':'+label])
                item['log_samples'].append({'at_kst': korean(at), 'level': level, 'category': label,
                                            'source': source, 'symbol': symbols([data.get('symbol')]),
                                            'error_type': code(data.get('error_type', 'UNKNOWN'))})
                item['log_samples'] = item['log_samples'][-30:]
        elif kind in {'orders', 'executions', 'order_intents', 'signals', 'llm_calls'}:
            group = item['activity'].setdefault(kind, {'first_kst': None, 'last_kst': None, 'statuses': {}, 'samples': []})
            when = korean(at)
            group['first_kst'] = min(group['first_kst'] or when, when)
            group['last_kst'] = max(group['last_kst'] or when, when)
            status = code(data.get('status', 'UNKNOWN'))
            group['statuses'][status] = group['statuses'].get(status, 0) + 1
            intent = data.get('intent') or {}
            ticker = symbols([data.get('symbol', intent.get('symbol'))])
            group['samples'].append({'at_kst': when, 'symbol': ticker[0] if ticker else None,
                                     'side': code(data.get('side', intent.get('side', 'UNKNOWN'))), 'status': status,
                                     'action': data.get('action') if data.get('action') in ('start','stop_all','sell_symbol') else None})
            group['samples'] = group['samples'][-30:]

    def control(self, item, name, data, captured):
        if name not in CONTROLS: return
        saved = stamp(captured)
        view = {'saved_at_kst': korean(captured), 'latest_only': True,
                'saved_in_window': bool(saved and self.since <= saved < self.until)}
        for key in ('started_at', 'finished_at', 'updated_at', 'heartbeat', 'checked_at', 'last_cycle_at'):
            if data.get(key): view[key + '_kst'] = korean(data[key])
        for key in ('status', 'phase', 'session'):
            if key in data: view[key] = code(data[key])
        for key in ('completed_tasks', 'complete_tasks', 'required_tasks', 'received_pages',
                    'reused_history_tasks', 'history_total_tasks', 'history_ready_count', 'history_excluded_count',
                    'included_count', 'excluded_count', 'operating_count', 'focus_count', 'last_cycle_calls',
                    'cycle_call_budget', 'last_cycle_duration_seconds', 'budget_exhausted', 'running', 'data_ready'):
            if isinstance(data.get(key), (bool, int, float)): view[key] = data[key]
        if name == 'startup_data_warmup_v1':
            view['issues_by_reason'] = reasons(x.get('reason') for x in data.get('issues', []) if isinstance(x, dict))
        if name == 'watchlist_dashboard_v1':
            view['entry_blockers'] = reasons(reason for x in data.get('items', []) for reason in x.get('entry_blockers', []))
            view['light_data_status'] = dict(collections.Counter(code(x.get('light_data_status')) for x in data.get('items', [])))
        if name == 'watchlist_scan_state_v1':
            view['read_measurements'] = [{'source': code(x.get('source')), 'symbol': symbols([x.get('symbol')]),
                 'status': code(x.get('status')), 'duration_seconds': x.get('duration_seconds'),
                 'at_kst': korean(x.get('received_at') or x.get('timestamp'))} for x in data.get('read_measurements', [])[-60:]]
        item['latest_controls'][name] = view

    def finish(self):
        for item in self.spaces.values():
            for kind in TABLES:
                if 'pq_' + kind in self.tables: item['counts_in_window'].setdefault(kind, 0)
            snapshots = sorted(item.pop('focus_snapshots'), key=lambda x: x['at_kst'])
            changes = []
            for snap in snapshots:
                if not changes or (snap['symbols'], snap['data_status']) != (changes[-1]['symbols'], changes[-1]['data_status']):
                    changes.append(snap)
            item['focus'] = {'snapshot_count': len(snapshots), 'first': snapshots[0] if snapshots else None,
                             'last': snapshots[-1] if snapshots else None,
                             'max_count': max((s['count'] for s in snapshots), default=None),
                             'first_nonzero': next((s for s in snapshots if s['count']), None),
                             'changes': changes[:100], 'changes_total': len(changes)}
        return {'since_kst': korean(self.since), 'until_kst_exclusive': korean(self.until),
                'entry_since_kst': korean(self.entry_since), 'entry_until_kst_exclusive': korean(self.entry_until),
                'report_version': 3,
                'missing_tables': sorted({'pq_' + x for x in TABLES} - self.tables), 'namespaces': self.spaces,
                'limitations': ['Controls are latest snapshots, not a complete warmup timeline.',
                    'Order rows describe retained order records, not every status transition.',
                    'Missing tables or unparseable timestamps do not prove no activity.',
                    'Errors absent from DB (including OS/container logs) require separate journal inspection.',
                    'No engine batches can mean recording was disabled; it does not prove the engine never ran.',
                    'Entry counts are repeated evaluations, not unique signals, symbols or trades; blockers overlap.',
                    'RVOL values are saved observations, not recalculated or renewed by this review.',
                    'The selected entry window is supplied by the caller, not inferred from the runtime schedule.',
                    'A missing signals-table row does not prove no technical signal was produced before selection gates.']}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


RESEARCH_FIELDS = ('kind', 'available_at', 'data.control_state', 'data.scope_blockers', 'data.manual_halt',
                   'data.decisions', 'data.quality_rows', 'data.portfolio_selection', 'data.decision_count')


class RowDecodeError(ValueError):
    """Only location/size metadata; never retain the failed JSON or decoder message."""
    def __init__(self, field, error_type, encoded_bytes, position=None):
        self.details = {'field': field, 'error_type': error_type, 'encoded_bytes': encoded_bytes,
                        'json_position': position}
        super().__init__('review_row_decode_failed')


def read_query(kind):
    """Hex-encoded TSV columns avoid rebuilding a large nested JSON_OBJECT in MySQL."""
    def hex_json(expression): return f'HEX(CONVERT({expression} USING utf8mb4))'
    if kind == 'controls':
        names = ','.join("'"+key+"'" for key in CONTROLS)
        return ('SELECT HEX(name),HEX(namespace),HEX(CAST(updated_at AS CHAR)),'+hex_json('payload')+
                f' FROM pq_controls WHERE name IN ({names}) ORDER BY name;')
    if kind not in TABLES: raise ValueError('unknown_review_table')
    payload = [hex_json(f"payload->'$.{key}'") for key in RESEARCH_FIELDS] if kind == 'research_events' else [hex_json('payload')]
    return ('SELECT seq,HEX(namespace),HEX(CAST(captured_at AS CHAR)),'+','.join(payload)+
            f' FROM pq_{kind} ORDER BY seq;')


def decode_columns(line, kind):
    cells = line.rstrip(b'\r\n').split(b'\t')
    expected = 3+len(RESEARCH_FIELDS) if kind == 'research_events' else 4
    if len(cells) != expected: raise RowDecodeError('columns', 'column_count_mismatch', len(line))

    def cell(index, field, *, as_json=False):
        raw = cells[index]
        if raw == b'NULL': return None
        try:
            text = bytes.fromhex(raw.decode('ascii')).decode('utf-8')
            return json.loads(text) if as_json else text
        except json.JSONDecodeError as exc:
            raise RowDecodeError(field, 'JSONDecodeError', len(raw), exc.pos) from None
        except (UnicodeError, ValueError):
            raise RowDecodeError(field, 'InvalidHexOrUTF8', len(raw)) from None

    row = {'kind': kind, 'namespace': cell(1, 'namespace'), 'captured_at': cell(2, 'captured_at')}
    if not isinstance(row['namespace'], str): raise RowDecodeError('namespace', 'missing_namespace', len(cells[1]))
    if kind == 'controls': row['name'] = cell(0, 'name')
    if kind == 'research_events':
        fields = {key: cell(index+3, key, as_json=True) for index, key in enumerate(RESEARCH_FIELDS)}
        body = {key.removeprefix('data.'): value for key, value in fields.items() if key.startswith('data.')}
        decisions = body.get('decisions')
        # Older/compact records may retain a total without all decision details.
        # Preserve that saved total rather than replacing it with zero/list size.
        if type(body.get('decision_count')) is not int or body['decision_count'] < 0:
            body['decision_count'] = len(decisions) if isinstance(decisions, list) else 0
        row['data'] = {'kind': fields['kind'], 'available_at': fields['available_at'], 'data': body}
    else:
        row['data'] = cell(3, 'payload', as_json=True)
        if not isinstance(row['data'], dict): raise RowDecodeError('payload', 'expected_object', len(cells[3]))
    return row


def consume_stream(lines, kind, review, transport):
    counters = transport['tables'].setdefault(kind, {'rows_seen': 0, 'rows_decoded': 0, 'rows_failed': 0})
    for line in lines:
        if not line.strip(): continue
        counters['rows_seen'] += 1
        try:
            row = decode_columns(line, kind)
        except RowDecodeError as exc:
            counters['rows_failed'] += 1
            transport['rows_failed'] += 1
            if len(transport['samples']) < 20:
                first = line.split(b'\t', 1)[0]
                # seq is numeric; controls use an ordinal so an arbitrary name cannot leak.
                seq = int(first) if kind != 'controls' and first.isdigit() and len(first) < 20 else None
                transport['samples'].append({'table': kind, 'row_ordinal': counters['rows_seen'],
                                             'seq': seq, **exc.details})
            continue
        review.consume(row)
        counters['rows_decoded'] += 1


def review_backup(backup, since, until, entry_since=None, entry_until=None):
    name = 'sq-review-' + uuid4().hex[:12]
    created = False
    with tempfile.TemporaryDirectory(prefix='sq-review-') as directory:
        env = Path(directory)/'db.env'; env.write_text('MYSQL_ROOT_PASSWORD='+secrets.token_urlsafe(32)+'\nMYSQL_DATABASE=sessionquant\n'); env.chmod(0o600)
        try:
            run('docker', 'run', '-d', '--name', name, '--network', 'none', '--memory', '1536m', '--cpus', '1',
                '--env-file', str(env), 'mysql:8.4', '--skip-log-bin', '--max-allowed-packet=64M',
                '--innodb-buffer-pool-size=128M', '--performance-schema=OFF')
            created = True
            free_kib = int(run('docker', 'exec', name, 'sh', '-c',
                              "df -Pk /var/lib/mysql | awk 'NR==2 {print $4}'", timeout=30).stdout.strip())
            if free_kib < 2*1024*1024: raise RuntimeError('temporary_disk_needs_2GiB_free')
            mysql = ['docker', 'exec', '-i', name, 'sh', '-c',
                     'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root --default-character-set=utf8mb4 --max-allowed-packet=128M --quick --batch --raw --skip-column-names sessionquant']
            deadline = time.monotonic()+180
            while True:
                try:
                    run(*mysql, input=b'SELECT 1;', timeout=10); break
                except subprocess.SubprocessError:
                    if time.monotonic() >= deadline: raise RuntimeError('temporary_mysql_start_failed') from None
                    time.sleep(1)
            print('임시 DB 준비 완료. 백업 복원 중…', flush=True)
            # Read only the SQL member; never extract arbitrary archive paths or app secrets.
            with tarfile.open(backup, 'r:gz') as archive:
                members = [m for m in archive.getmembers() if m.name in {'database.sql.gz', 'database.sql'}]
                if len(members) != 1 or not members[0].isfile(): raise ValueError('backup_sql_member_invalid')
                member = members[0]
                stream = archive.extractfile(member)
                if member.name.endswith('.gz'): stream = gzip.GzipFile(fileobj=stream)
                with tempfile.TemporaryFile() as errors:
                    proc = subprocess.Popen(mysql, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
                    try:
                        with stream:
                            while chunk := stream.read(1024*1024): proc.stdin.write(chunk)
                        proc.stdin.close()
                        if proc.wait(timeout=600): raise RuntimeError('backup_import_failed')
                    finally:
                        if proc.poll() is None: proc.kill(); proc.wait()
            tables = run(*mysql, input=b'SHOW TABLES;', timeout=30).stdout.decode('utf-8').splitlines()
            review = Review(since, until, tables, entry_since, entry_until)
            kinds = [kind for kind in TABLES if 'pq_'+kind in tables]
            if 'pq_controls' in tables: kinds.append('controls')
            transport = {'rows_failed': 0, 'samples': [], 'tables': {}}
            print('기록 요약 중…', flush=True)
            for kind in kinds:
                print('표 읽기: pq_'+kind, flush=True)
                sql_path = Path(directory)/'review.sql'; sql_path.write_text(read_query(kind), encoding='utf-8')
                with sql_path.open('rb') as source, tempfile.TemporaryFile() as errors:
                    proc = subprocess.Popen(mysql, stdin=source, stdout=subprocess.PIPE, stderr=errors)
                    try:
                        consume_stream(proc.stdout, kind, review, transport)
                        if proc.wait(timeout=60): raise RuntimeError('backup_read_failed')
                    finally:
                        if proc.poll() is None: proc.kill(); proc.wait()
            result = review.finish()
            result['transport'] = transport
            result['report_status'] = 'PARTIAL' if transport['rows_failed'] else 'COMPLETE'
            if transport['rows_failed']:
                result['limitations'].append('Some records could not be read; totals are incomplete and zero does not establish no activity.')
            result['read_only_production'] = True
            result['application_started'] = False
            result['provider_calls'] = result['llm_calls_by_reviewer'] = result['order_writes_by_reviewer'] = 0
            result['backup_file'] = backup.name
            with backup.open('rb') as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
            result['backup_sha256'] = digest.hexdigest()
            return result
        finally:
            if created:
                # Unique container and its anonymous volume only. No global prune.
                cleanup = subprocess.run(['docker', 'rm', '-fv', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if cleanup.returncode: print('임시 컨테이너 정리 실패: '+name, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--since', required=True)
    parser.add_argument('--until', required=True)
    parser.add_argument('--entry-since', help='Optional start of the entry window to inspect, not an inferred exchange schedule')
    parser.add_argument('--entry-until', help='Exclusive end of the explicitly selected entry window')
    args = parser.parse_args()
    since, until = stamp(args.since), stamp(args.until)
    if not since or not until or since >= until: parser.error('기간을 ISO 형식으로 지정하세요.')
    entry_since, entry_until = stamp(args.entry_since), stamp(args.entry_until)
    if (args.entry_since is not None or args.entry_until is not None) and not (
            entry_since and entry_until and since <= entry_since < entry_until <= until):
        parser.error('진입 분석 시작·종료를 모두 지정하고 전체 분석 기간 안으로 설정하세요.')
    if not args.backup.is_file(): parser.error('백업 파일이 없습니다.')
    if args.output.exists(): parser.error('결과 파일이 이미 있습니다. 새 이름을 지정하세요.')
    try:
        result = review_backup(args.backup, since, until, entry_since, entry_until)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        if result['report_status'] == 'PARTIAL':
            print('일부 기록 읽기 실패: '+str(result['transport']['rows_failed'])+'건 · 결과를 정상 완료로 해석하지 마세요.')
        print('점검 결과 저장: '+str(args.output))
        for namespace, data in result['namespaces'].items():
            print(namespace, json.dumps({'counts':data['counts_in_window'], 'focus_max':data['focus']['max_count']}, ensure_ascii=False))
        if result['report_status'] == 'PARTIAL': raise SystemExit(2)
    except Exception as exc:
        # Do not print Docker command env values, SQL, or server exception text.
        known = {'temporary_mysql_start_failed', 'backup_sql_member_invalid', 'backup_import_failed',
                 'backup_read_failed', 'temporary_disk_needs_2GiB_free'}
        reason = str(exc) if str(exc) in known else type(exc).__name__
        raise SystemExit('점검 실패: '+reason+' · 운영 DB는 변경하지 않았습니다.') from None


if __name__ == '__main__':
    main()

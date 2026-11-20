"""Capture saved market diagnostics; no engine startup, API requests or DB writes.

Run inside the app container, including via ``python - < this_file.py`` on an
older image. Export only bounded, explicit diagnostic fields, never credentials.
"""
import argparse
from datetime import datetime, timezone
import json
import os
import re
import sys
import time

KEYS = ('market_data_quality_v1', 'watchlist_scan_state_v1', 'market_performance_v1',
        'trade_control_state', 'portfolio_selection_v1', 'watchlist_rvol_v1')
REASONS = frozenset('''quality_observation_stale symbol_not_yet_evaluated quote_stale quote_missing
    minute_history_incomplete_or_stale relative_volume_unknown_or_low split_volume_comparability_unknown
    current_session_coverage_incomplete historical_same_minute_coverage_incomplete
    volume_source_or_adjustment_mismatch invalid_or_zero_history_volume current_closed_interval_missing
    symbol_not_yet_collected new_closed_minute_not_evaluated focus_refresh_pending focus_unknown_or_stale
    market_bearish market_unknown market_neutral eligibility_unverified trading_not_started_or_halted
    startup_data_not_ready market_provider_backoff technical_signal_absent confirmed_news_entry_restriction
    outside_market_cap_watchlist history_not_in_daily_operating_watchlist preferred_position_target_reached
    final_candidate_limit candidate_bar_time_mismatch candidate_quote_stale_or_unknown
    candidate_quote_time_mismatch signal_already_consumed duplicate_symbol_signal minute_gaps
    bar_revisions_unresolved no_completed_regular_minute minute_data_stale_or_missing
    market_closed_or_calendar_unknown rvol_calendar_unknown quote_time_or_symbol_invalid
    quote_halted quote_depth_missing spread_too_wide universe_unknown_stale_or_nonmember
    instrument_eligibility_unknown instrument_symbol_mismatch instrument_eligibility_stale_or_future
    instrument_eligibility_expired exchange_unverified market_reference_not_trading_candidate
    paper_listing_evidence_missing paper_listing_evidence_stale_or_missing paper_listing_not_active
    paper_known_restriction_tradable paper_known_restriction_fractional paper_known_restriction_halted
    paper_known_restriction_corporate_action'''.split())
PHASES = frozenset('''api_pacing api_http api_retry_wait db_query db_commit json_encode engine_batch
    engine_persist history_save history_load indicators rvol bar_quality warmup watchlist_publish'''.split())


def mapping(value):
    return value if isinstance(value, dict) else {}


def rows(value):
    return [row for row in value[:50] if isinstance(row, dict)] if isinstance(value, list) else []


def reasons(values):
    # 임의의 메시지나 URL을 진단 자료에 복사하지 않는다.
    values = values[:50] if isinstance(values, list) else []
    known = sorted({code for code in values if isinstance(code, str) and code in REASONS})
    unknown = sum(not isinstance(code, str) or code not in REASONS for code in values)
    return {'codes': known, 'other_count': unknown}


def status(value):
    allowed = {'UNKNOWN', 'VALID', 'VERIFIED', 'COMPLETE', 'INCOMPLETE', 'STALE', 'CALCULATED',
               'WAITING', 'RUNNING', 'STOPPED', 'STOPPING', 'BLOCKED', 'SIGNAL', 'APPROVED',
               'OBSERVED_PAPER', 'STRICT', 'REJECTED', 'NOT_SELECTED', 'SIMULATION_ASSUMPTIONS',
               'WARMING', 'LIQUIDATING', 'CLOSED', 'EXPERIMENTAL_UNVERIFIED'}
    return value if isinstance(value, str) and value in allowed else 'UNKNOWN'


def stamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(timezone.utc) if result.utcoffset() is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def iso(value):
    value = stamp(value)
    return value.isoformat() if value else None


def seconds(later, earlier):
    first, last = stamp(earlier), stamp(later)
    return round((last-first).total_seconds(), 3) if first and last and last >= first else None


def number(value):
    import math
    try:
        return value if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None


def volume_fields(volume):
    volume = mapping(volume)
    missing = volume.get('current_missing_examples')
    missing = missing[:5] if isinstance(missing, list) else []
    return {**{key: number(volume.get(key)) for key in (
        'calculated_value', 'value', 'complete_sessions', 'current_expected_minutes', 'current_observed_minutes',
        'current_missing_minutes', 'current_missing_interior_minutes', 'current_missing_tail_minutes',
        'current_cumulative_volume', 'historical_mean_cumulative_volume')},
        **{key: iso(volume.get(key)) for key in ('observed_at', 'cutoff_at', 'calculated_valid_until',
                                               'valid_until', 'current_last_bar_at', 'current_last_received_at')},
        'status': status(volume.get('status')), 'calculation_status': status(volume.get('calculation_status')),
        'reasons': reasons(volume.get('reasons')), 'missing_examples': [iso(value) for value in missing if iso(value)]}


def bar_fields(bars):
    bars = mapping(bars)
    return {**{key: number(bars.get(key)) for key in ('expected_minutes', 'observed_minutes',
        'missing_minutes', 'conflicting_minutes', 'five_count', 'fifteen_count', 'five_coverage', 'fifteen_coverage')},
        **{key: iso(bars.get(key)) for key in ('observed_at', 'cutoff_at', 'last_closed_at', 'last_received_at')},
        'status': status(bars.get('status')), 'reasons': reasons(bars.get('reasons'))}


def summarize(controls, now):
    quality, source, perf, engine, selection, history = [mapping(controls.get(key)) for key in KEYS]
    output = {'schema_version':2, 'checked_at':now.isoformat(), 'read_only':True, 'provider_calls':0, 'order_writes':0,
        'quality_published_at':iso(quality.get('observed_at')), 'quality_captured_at':iso(quality.get('captured_at')),
        'engine':{'status':status(engine.get('status')), 'heartbeat':iso(engine.get('heartbeat')),
                  'heartbeat_age_seconds':seconds(now.isoformat(),engine.get('heartbeat')),
                  'data_ready':engine.get('data_ready') if type(engine.get('data_ready')) is bool else None},
        'selection_evaluated_at':iso(selection.get('evaluated_at')),
        'selection_valid_until':iso(selection.get('valid_until')),
        'rvol_published_at':iso(history.get('observed_at')),
        'unavailable_controls':[key for key in KEYS if not isinstance(controls.get(key),dict)],
        'last_cycle_at':iso(source.get('last_cycle_at')),
        'last_cycle_seconds':number(source.get('last_cycle_duration_seconds')),
        'candidate_delivered_at':iso(source.get('candidate_delivered_at')),
        'backoff_until':iso(source.get('backoff_until')), 'symbols':[]}
    focus = {r['ticker']:r for r in rows(source.get('focus')) if isinstance(r.get('ticker'),str)}
    ranking = {r['symbol']:r for r in rows(selection.get('rows')) if isinstance(r.get('symbol'),str)}
    volumes = {r['symbol']:r for r in rows(history.get('items')) if isinstance(r.get('symbol'),str)}
    evaluated = {r['symbol']:r for r in rows(quality.get('items')) if isinstance(r.get('symbol'),str)}
    # Include unevaluated focus/watchlist members so a missing evaluation stays visible.
    symbols = list(dict.fromkeys([*evaluated, *focus, *ranking, *volumes]))[:50]
    for symbol in symbols:
        if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,14}',symbol):
            continue
        row = evaluated.get(symbol,{})
        saved = mapping(row.get('last_evaluation'))
        if saved.get('observed_at') != row.get('observed_at'):
            saved = {}
        volume = mapping(row.get('rvol'))
        membership = focus.get(symbol, {})
        regime = ranking.get(symbol,{}).get('regime')
        output['symbols'].append({'symbol':symbol, 'in_focus':symbol in focus,
            'evaluated_at':iso(row.get('observed_at')),
            'evaluation_age_seconds':seconds(now.isoformat(),row.get('observed_at')),
            'quote_at':iso(row.get('quote_at')), 'quote_received_at':iso(row.get('quote_received_at')),
            'quote_age_on_receipt_seconds':seconds(row.get('quote_received_at'),row.get('quote_at')),
            'seconds_since_quote_received':seconds(now.isoformat(),row.get('quote_received_at')),
            'last_quote_attempt':iso(mapping(source.get('last_candidate_quote_attempt')).get(symbol)),
            'last_quote_success':iso(mapping(source.get('last_quote_success')).get(symbol)),
            'last_candle_attempt':iso(mapping(source.get('last_candle_attempt')).get(symbol)),
            'focus_since':iso(membership.get('focus_since')),
            'refresh_wait_until':iso(membership.get('refresh_wait_until')),
            'bars':bar_fields(row.get('bars')), 'strategy_bars':bar_fields(row.get('strategy_bars')),
            'blockers':reasons(row.get('blockers')),
            'strategy_rvol_saved':number(row.get('strategy_rvol')),
            'strategy_rvol_status':status(row.get('strategy_rvol_status')),
            'strategy_volume':volume_fields(volume), 'minute_volume':volume_fields(volumes.get(symbol)),
            'selection':{'evaluated_at':iso(ranking.get(symbol,{}).get('evaluated_at')),
                'blockers':reasons(ranking.get(symbol,{}).get('blockers')),
                'regime':regime if isinstance(regime,str) and regime in {'BULLISH','BEARISH','NEUTRAL'} else 'UNKNOWN'},
            'rvol_at_last_evaluation':number(saved.get('rvol_calculated_value',volume.get('calculated_value'))),
            'history_sessions':number(volume.get('complete_sessions'))})
    latest = mapping(perf.get('latest'))
    phases = []
    for row in rows(latest.get('phases'))[:30]:
        phase = row.get('phase')
        if isinstance(phase,str) and phase in PHASES:
            phases.append({'phase':phase,'calls':number(row.get('calls')),'seconds':number(row.get('seconds')),
                           'max_seconds':number(row.get('max_seconds'))})
    output['performance'] = {'observed_at':iso(latest.get('observed_at')),
        'elapsed_seconds':number(latest.get('elapsed_seconds')),
        'process_cpu_seconds':number(latest.get('process_cpu_seconds')), 'phases':phases}
    return output


def read_controls(config_path):
    import pymysql
    from sessionquant.database.config import MySQLConfig, namespace_for
    from sessionquant.session_config import load_session_config
    cfg, db = load_session_config(config_path), MySQLConfig.from_env()
    namespace = namespace_for(cfg.db_path, db.namespace, cfg.environment)
    connection = pymysql.connect(host=db.host,port=db.port,user=db.user,password=db.password,
        database=db.database,charset='utf8mb4',connect_timeout=5,read_timeout=10,write_timeout=10)
    try:
        with connection.cursor() as cursor:
            cursor.execute('START TRANSACTION READ ONLY')
            cursor.execute('SELECT /*+ MAX_EXECUTION_TIME(5000) */ name,payload FROM pq_controls '
                'WHERE namespace=%s AND name IN ('+','.join(['%s']*len(KEYS))+')', (namespace,*KEYS))
            return {name:json.loads(payload) for name,payload in cursor.fetchall()}
    finally:
        connection.close()


def capture(config_path, samples, interval, *, reader=None, clock=None, wall=None, sleeper=None, output=None):
    """Bounded observations; a slow read never schedules a burst of catch-up reads."""
    reader, clock = reader or read_controls, clock or time.monotonic
    wall, sleeper = wall or (lambda: datetime.now(timezone.utc)), sleeper or time.sleep
    output = output if output is not None else sys.stdout
    started, began = wall(), clock()
    captured = failed = 0
    interrupted = False

    def emit(record):
        print(json.dumps(record, ensure_ascii=False, indent=2 if samples == 1 else None), file=output, flush=True)

    try:
        for sequence in range(1, samples+1):
            read_started, tick = wall(), clock()
            base = {'record_type':'sample', 'sequence':sequence, 'read_only':True, 'provider_calls':0,
                    'order_writes':0, 'llm_generation_calls':0, 'read_started_at':read_started.isoformat(),
                    'elapsed_seconds':round(tick-began,3)}
            try:
                controls = reader(config_path)
            except Exception as exc:
                elapsed = clock()-tick
                finished = wall()
                # Do not print exception text: connection errors can contain a host or user.
                record = {**base, 'status':'FAILED', 'error_type':type(exc).__name__}
                failed += 1
            else:
                elapsed = clock()-tick
                finished = wall()
                record = {**summarize(controls,finished), **base, 'status':'OK'}
                captured += 1
            emit({**record, 'read_finished_at':finished.isoformat(), 'db_read_seconds':round(elapsed,3)})
            if sequence < samples:
                sleeper(max(0, interval-(clock()-tick)))
    except KeyboardInterrupt:
        interrupted = True
    if samples > 1 or interrupted:
        emit({'record_type':'summary', 'schema_version':2, 'read_only':True, 'provider_calls':0,
              'order_writes':0, 'llm_generation_calls':0, 'started_at':started.isoformat(),
              'finished_at':wall().isoformat(), 'requested_samples':samples, 'captured_samples':captured,
              'failed_samples':failed, 'interrupted':interrupted})
    return 130 if interrupted else 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=os.getenv('SQ_SESSION_CONFIG') or 'config/sessionquant.realtime-paper.example.yaml')
    parser.add_argument('--samples',type=int,default=1,help='1–61 samples; multiple samples produce JSON Lines')
    parser.add_argument('--interval',type=int,default=10,help='5–60 seconds between read starts (default: 10)')
    args = parser.parse_args(argv)
    if not 1 <= args.samples <= 61 or not 5 <= args.interval <= 60:
        parser.error('--samples must be 1–61 and --interval must be 5–60')
    return capture(args.config,args.samples,args.interval)


if __name__ == '__main__':
    raise SystemExit(main())

"""Read three saved controls; no engine startup, provider requests, or DB writes.

Run inside the app container, including via ``python - < this_file.py`` on an
older image. Export only bounded, explicit diagnostic fields, never credentials.
"""
import argparse
from datetime import datetime, timezone
import json
import re

KEYS = ('market_data_quality_v1', 'watchlist_scan_state_v1', 'market_performance_v1')


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
    return value if type(value) in (int, float) and math.isfinite(value) else None


def summarize(controls, now):
    quality, source, perf = [controls.get(key, {}) for key in KEYS]
    output = {'checked_at':now.isoformat(), 'read_only':True, 'provider_calls':0, 'order_writes':0,
        'last_cycle_at':iso(source.get('last_cycle_at')),
        'last_cycle_seconds':number(source.get('last_cycle_duration_seconds')),
        'candidate_delivered_at':iso(source.get('candidate_delivered_at')),
        'backoff_until':iso(source.get('backoff_until')), 'symbols':[]}
    focus = {r.get('ticker'):r for r in source.get('focus', [])}
    for row in quality.get('items', [])[:50]:
        symbol = row.get('symbol')
        if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,14}',symbol):
            continue
        saved = row.get('last_evaluation') or {}
        volume = row.get('rvol') or {}
        membership = focus.get(symbol, {})
        output['symbols'].append({'symbol':symbol, 'evaluated_at':iso(row.get('observed_at')),
            'evaluation_age_seconds':seconds(now.isoformat(),row.get('observed_at')),
            'quote_at':iso(row.get('quote_at')), 'quote_received_at':iso(row.get('quote_received_at')),
            'quote_age_on_receipt_seconds':seconds(row.get('quote_received_at'),row.get('quote_at')),
            'seconds_since_quote_received':seconds(now.isoformat(),row.get('quote_received_at')),
            'last_quote_attempt':iso(source.get('last_candidate_quote_attempt',{}).get(symbol)),
            'last_quote_success':iso(source.get('last_quote_success',{}).get(symbol)),
            'last_candle_attempt':iso(source.get('last_candle_attempt',{}).get(symbol)),
            'focus_since':iso(membership.get('focus_since')),
            'refresh_wait_until':iso(membership.get('refresh_wait_until')),
            'rvol_at_last_evaluation':number(saved.get('rvol_calculated_value',volume.get('calculated_value'))),
            'history_sessions':number(volume.get('complete_sessions'))})
    latest = perf.get('latest') or {}
    phases = []
    for row in latest.get('phases', [])[:30]:
        phase = row.get('phase')
        if isinstance(phase,str) and re.fullmatch(r'[a-z_]{1,40}',phase):
            phases.append({'phase':phase,'calls':number(row.get('calls')),'seconds':number(row.get('seconds'))})
    output['performance'] = {'observed_at':iso(latest.get('observed_at')),
        'elapsed_seconds':number(latest.get('elapsed_seconds')), 'phases':phases}
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
                'WHERE namespace=%s AND name IN (%s,%s,%s)', (namespace,*KEYS))
            return {name:json.loads(payload) for name,payload in cursor.fetchall()}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config/sessionquant.realtime-paper.example.yaml')
    args = parser.parse_args()
    try:
        controls = read_controls(args.config)
        print(json.dumps(summarize(controls,datetime.now(timezone.utc)),ensure_ascii=False,indent=2))
    except Exception as exc:
        print(json.dumps({'status':'FAILED','error_type':type(exc).__name__,'read_only':True}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

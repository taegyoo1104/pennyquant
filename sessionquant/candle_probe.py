"""Local, read-only Toss minute history probe; no database or trading runtime."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import exchange_calendars as xcals
import httpx

from .collectors.toss_market import TossMarketClient, symbols_parameter
from .local_env import load_local_env
from .models import utc


def completed_sessions(now: datetime, count: int):
    now = utc(now)
    calendar = xcals.get_calendar('XNYS', start=(now - timedelta(days=150)).date(), end=now.date())
    sessions = [(str(day.date()), calendar.session_open(day).to_pydatetime(),
                 calendar.session_close(day).to_pydatetime()) for day in calendar.sessions]
    return [row for row in sessions if row[2] <= now][-count:]


async def probe_session(client, symbol, day, opened, closed, max_pages=10):
    expected = {opened + timedelta(minutes=i) for i in range(1, int((closed-opened).total_seconds()/60)+1)}
    seen = {}
    duplicates = conflicts = 0
    cursor = closed
    stop = 'PAGE_LIMIT'
    pages = 0
    for _ in range(max_pages):
        bars, following = await client.candles(symbol, count=200, before=cursor)
        pages += 1
        if any(bar.timestamp > cursor for bar in bars):
            stop = 'INVALID_PAGE_BOUNDARY'
            break
        for bar in bars:
            if bar.timestamp not in expected:
                continue
            values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
            if bar.timestamp in seen:
                duplicates += 1
                conflicts += seen[bar.timestamp] != values
            seen[bar.timestamp] = values
        if bars and min(bar.timestamp for bar in bars) <= min(expected):
            stop = 'SESSION_START_REACHED'
            break
        if not bars or following is None:
            stop = 'HISTORY_ENDED'
            break
        following = utc(following)
        if following >= cursor:
            stop = 'NON_PROGRESSING_CURSOR'
            break
        cursor = following
    missing = sorted(expected - seen.keys())
    return {'session': day, 'expected_minutes': len(expected), 'received_minutes': len(seen),
            'missing_minutes': len(missing), 'missing_sample_utc': [t.isoformat() for t in missing[:10]],
            'duplicate_rows': duplicates, 'conflicting_rows': conflicts,
            'zero_volume_minutes': sum(v[-1] == 0 for v in seen.values()),
            'first_bar_utc': min(seen).isoformat() if seen else None,
            'last_bar_utc': max(seen).isoformat() if seen else None,
            'volume_sum': sum(v[-1] for v in seen.values()), 'pages': pages, 'stop_reason': stop,
            'complete': not missing and conflicts == 0 and stop == 'SESSION_START_REACHED'}


async def run(args):
    load_local_env()
    if not all(os.environ.get(key) for key in ('TOSS_CLIENT_ID', 'TOSS_CLIENT_SECRET')):
        print('TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 환경변수가 필요합니다. 키 값은 출력하지 않습니다.')
        return 2
    now = datetime.now(timezone.utc)
    sessions = completed_sessions(now, args.sessions)
    report = {'symbol': args.symbol, 'checked_at_utc': now.isoformat(), 'requested_sessions': args.sessions,
              'source': 'toss:/api/v1/candles:1m', 'adjusted': False, 'sessions': [],
              'rvol_comparability_verified': False, 'error': None}
    # No account number is needed for market data, even if a local value is configured.
    client = TossMarketClient(account_seq=0)
    try:
        for day, opened, closed in reversed(sessions):
            row = await probe_session(client, args.symbol, day, opened, closed)
            report['sessions'].append(row)
            print(f"{day}: {row['received_minutes']}/{row['expected_minutes']}분, "
                  f"누락 {row['missing_minutes']}, 충돌 {row['conflicting_rows']} ({row['stop_reason']})", flush=True)
    except Exception as exc:
        # Do not print exceptions, request objects, tokens, or provider response bodies.
        report['error'] = {'type': type(exc).__name__}
        if isinstance(exc, httpx.HTTPStatusError):
            report['error']['http_status'] = exc.response.status_code
        print('조회가 중단됐습니다. 보고서의 error 유형/HTTP 상태를 확인하세요.', flush=True)
    finally:
        await client.aclose()
    report['history_complete'] = (len(report['sessions']) == args.sessions
                                  and report['error'] is None
                                  and all(row['complete'] for row in report['sessions']))
    out = Path('data') / ('toss-history-probe-' + args.symbol + '-' + now.strftime('%Y%m%dT%H%M%S%fZ') + '.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(f"보고서: {out}\n요청한 {args.sessions}거래일 전체 확보: {report['history_complete']}")
    print('누락 분봉은 0으로 보충하지 않았습니다. 집계 범위·분할 조정의 비교 가능성은 별도 확인해야 합니다.')
    return 0 if report['history_complete'] else 1


def main():
    parser = argparse.ArgumentParser(description='토스 최근 완료 거래일의 정규장 1분봉 확보율 검사 (주문/DB/LLM 없음)')
    parser.add_argument('--symbol', default='AAPL')
    parser.add_argument('--sessions', type=int, default=20, choices=range(1, 61), metavar='1..60')
    args = parser.parse_args()
    symbols_parameter([args.symbol])
    print('같은 토스 클라이언트를 사용하는 수집기를 먼저 종료하세요. 새 인증은 이전 토큰을 무효화할 수 있습니다.', flush=True)
    raise SystemExit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()

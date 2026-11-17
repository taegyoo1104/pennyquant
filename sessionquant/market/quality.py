"""보간하지 않은 봉의 실제 확보율과 동일 경과 분 RVOL 부족 입력을 보고한다."""
from collections import Counter, defaultdict
from datetime import timedelta
import math

from ..models import utc
from ..performance import timed
from ..scanners.relative_volume import NEW_YORK, _calendar
from .bars import _aggregate_available, available_minutes, calendar, session_bounds

QUALITY_KEY = 'market_data_quality_v1'


@timed('bar_quality')
def bar_quality(bars, symbol, now):
    now = utc(now)
    bounds = session_bounds(now)
    result = {'symbol':symbol, 'observed_at':now.isoformat(), 'status':'UNKNOWN',
              'source':sorted({b.source for b in bars if b.symbol == symbol and b.received_at <= now}),
              'price_adjustment':sorted({b.price_adjustment for b in bars if b.symbol == symbol and b.received_at <= now}),
              'expected_minutes':0, 'observed_minutes':0, 'missing_minutes':None,
              'missing_rate':None, 'five_count':0, 'fifteen_count':0, 'reasons':[]}
    if bounds is None:
        try:
            day = now.astimezone(NEW_YORK).date()
            result['status'] = 'CLOSED' if not calendar(day.year).is_session(str(day)) else 'UNKNOWN'
        except (ValueError, KeyError, ImportError):
            pass
        result['reasons'] = ['market_closed_or_calendar_unknown']
        return result
    opened, closed = bounds
    count = max(0, int((min(now,closed)-opened).total_seconds()//60))
    expected = {opened+timedelta(minutes=i) for i in range(1,count+1)}
    available = available_minutes(bars, now, symbol)
    valid = {b.timestamp:b for b in available if b.timestamp in expected}
    missing = sorted(expected-set(valid))
    raw = [b for b in bars if b.symbol == symbol and b.timestamp in expected and b.received_at <= now]
    versions = defaultdict(set)
    for b in raw:
        versions[b.timestamp].add((b.open,b.high,b.low,b.close,b.volume))
    conflicts = sum(len(values)>1 for values in versions.values())
    last = max(valid.values(), key=lambda b:b.timestamp) if valid else None
    five, fifteen = _aggregate_available(available,5,now,bounds), _aggregate_available(available,15,now,bounds)
    reasons = (['minute_gaps'] if missing else [])+(['bar_revisions_unresolved'] if conflicts else [])
    if not count:
        reasons.append('no_completed_regular_minute')
    age = (now-last.timestamp).total_seconds() if last else None
    if age is None or age > 90:
        reasons.append('minute_data_stale_or_missing')
    result.update(status='COMPLETE' if count and not reasons else 'INCOMPLETE',
        session_open=opened.isoformat(), session_close=closed.isoformat(),
        expected_minutes=count, observed_minutes=len(valid), missing_minutes=len(missing),
        missing_rate=len(missing)/count if count else None, missing_examples=[t.isoformat() for t in missing[:5]],
        explicit_zero_volume_minutes=sum(b.volume == 0 for b in valid.values()),
        identical_duplicates=max(0,len(raw)-sum(len(v) for v in versions.values())), conflicting_minutes=conflicts,
        five_count=len(five), five_expected=count//5, fifteen_count=len(fifteen), fifteen_expected=count//15,
        five_coverage=len(five)/(count//5) if count//5 else None,
        fifteen_coverage=len(fifteen)/(count//15) if count//15 else None,
        last_closed_at=last.timestamp.isoformat() if last else None,
        last_received_at=last.received_at.isoformat() if last else None,
        latest_receipt_lag_seconds=(last.received_at-last.timestamp).total_seconds() if last else None,
        age_seconds=age, reasons=reasons)
    return result


def volume_evidence_valid(evidence, bars, start, through, now):
    """분할 비교 가능성은 가격 모양·상장 상태·일반 확인 불리언으로 추측하지 않는다."""
    try:
        return bool(evidence.get('status') == 'VERIFIED' and evidence.get('source') not in {None,'','UNKNOWN'}
                    and evidence.get('split_evidence_source') not in {None,'','UNKNOWN'}
                    and utc(evidence['received_at']) <= now <= utc(evidence['valid_until'])
                    and utc(evidence['range_start']) <= utc(evidence['range_end']) <= utc(evidence['received_at'])
                    and utc(evidence['range_start']) <= start and utc(evidence['range_end']) >= through
                    and evidence.get('price_adjustment') in {'raw','adjusted'}
                    and evidence.get('volume_policy') in {'no_splits_verified','split_adjusted_verified'}
                    and (evidence['volume_policy'] != 'split_adjusted_verified' or evidence['price_adjustment'] == 'adjusted')
                    and all(b.source == evidence['source'] and b.price_adjustment == evidence['price_adjustment'] for b in bars))
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


@timed('rvol')
def rvol_report(bars, symbol, through, now, *, evidence=None, legacy_comparable=False, min_sessions=20):
    """최신 20개 비교 가능 거래일의 정확한 동일 경과 분을 검사한다. 더 오래된 날로 채우지 않는다."""
    if type(min_sessions) is not int or min_sessions < 1:
        raise ValueError('비교 거래일 수는 양의 정수여야 합니다.')
    now = utc(now)
    result = {'value':None, 'calculated_value':None, 'calculation_status':'UNKNOWN',
        'status':'UNKNOWN', 'required_sessions':min_sessions, 'complete_sessions':0,
        'cutoff_at':through.isoformat() if through else None, 'observed_at':now.isoformat(), 'reasons':[], 'sessions':[]}
    bounds = session_bounds(through) if through else None
    if not bounds or through > now or through.second or through.microsecond or not bounds[0] < through <= bounds[1]:
        result['reasons'] = ['current_closed_interval_missing']
        return result
    elapsed = int((through-bounds[0]).total_seconds()//60)
    day = through.astimezone(NEW_YORK).date()
    lookback = day-timedelta(days=max(90,min_sessions*3+30))
    try:
        cal = _calendar(lookback.year,day.year)
        selected = []
        for session in reversed(cal.sessions_in_range(str(lookback),str(day-timedelta(days=1)))):
            opened, closed = cal.session_open(session).to_pydatetime(), cal.session_close(session).to_pydatetime()
            if (closed-opened).total_seconds() >= elapsed*60:
                selected.append((session,opened))
                if len(selected) == min_sessions:
                    break
    except (ValueError,KeyError,ImportError,TypeError):
        result['reasons'] = ['rvol_calendar_unknown']
        return result
    all_relevant = []
    by_timestamp = defaultdict(list)
    for bar in bars:
        if bar.symbol == symbol and bar.timestamp <= bar.received_at <= now:
            by_timestamp[bar.timestamp].append(bar)
    totals = []
    for session, opened in selected:
        required = {opened+timedelta(minutes=i) for i in range(1,elapsed+1)}
        values = [b for stamp in required for b in by_timestamp.get(stamp,[])]
        all_relevant.extend(values)
        versions = defaultdict(set)
        for b in values:
            versions[b.timestamp].add((b.open,b.high,b.low,b.close,b.volume))
        missing = len(required-set(versions))
        conflicting = sum(len(v)>1 for v in versions.values())
        complete = not missing and not conflicting
        if complete:
            totals.append(sum(next(iter(versions[stamp]))[-1] for stamp in required))
        result['complete_sessions'] += int(complete)
        result['sessions'].append({'date':str(session.date()),'expected_minutes':elapsed,
                                  'missing_minutes':missing,'conflicting_minutes':conflicting})
    if len(selected) != min_sessions or result['complete_sessions'] != min_sessions:
        result['reasons'].append('historical_same_minute_coverage_incomplete')
    current = [b for b in available_minutes(bars,now,symbol) if b.timestamp <= through]
    all_relevant.extend(current)
    if len(current) != elapsed:
        result['reasons'].append('current_session_coverage_incomplete')
    provenance = {(b.source,b.price_adjustment) for b in all_relevant}
    if len(provenance) != 1 or any(source in {'','UNKNOWN'} or adjustment not in {'raw','adjusted'}
                                   for source,adjustment in provenance):
        result['reasons'].append('volume_source_or_adjustment_mismatch')
    if not result['reasons']:
        average = sum(totals)/min_sessions
        cumulative = sum(b.volume for b in current)
        value = cumulative/average if average > 0 else None
        if value is None or not math.isfinite(value):
            result['reasons'].append('invalid_or_zero_history_volume')
        else:
            result.update(calculated_value=value,calculation_status='CALCULATED',
                current_cumulative_volume=cumulative,historical_mean_cumulative_volume=average,
                elapsed_minutes=elapsed,calculated_valid_until=(now+timedelta(seconds=30)).isoformat())
    comparable = legacy_comparable or (bool(selected) and volume_evidence_valid(
        evidence or {}, all_relevant, selected[-1][1], through, now))
    if not comparable:
        result['reasons'].append('split_volume_comparability_unknown')
    if not result['reasons']:
        value = result['calculated_value']
        if value is None:
            result['reasons'].append('invalid_or_zero_history_volume')
        else:
            expires = utc((evidence or {}).get('valid_until',now+timedelta(seconds=30)))
            result.update(value=value,status='VERIFIED',valid_until=min(expires,now+timedelta(seconds=30)).isoformat())
    return result


def quality_summary(rows, now, *, source_state=None, synthetic=False):
    """대상과 분모를 명시하고 미평가·만료 항목을 확보 성공으로 세지 않는다."""
    counts = Counter(code for row in rows for code in set(row.get('blockers',[])))
    total = len(rows)
    report = {'observed_at':utc(now).isoformat(), 'synthetic':synthetic, 'evaluated_symbols':total,
        'blocker_counts':dict(counts), 'source_state':source_state or {}, 'items':rows,
        'live_orders_enabled':False, 'required_data_coverage':{}}
    for name, predicate in {
        'eligibility':lambda r:r['eligibility_verified'],
        'quote':lambda r:r['quote_fresh'],
        'minute_history':lambda r:r['bars']['status'] == 'COMPLETE',
        'five_bars':lambda r:r['bars'].get('five_coverage') == 1,
        'fifteen_bars':lambda r:r['bars'].get('fifteen_coverage') == 1,
        'rvol':lambda r:r['rvol']['status'] == 'VERIFIED',
    }.items():
        count = sum(bool(predicate(r)) for r in rows)
        report['required_data_coverage'][name] = {'verified':count,'required':total,'ratio':count/total if total else None}
    return report

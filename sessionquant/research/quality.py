"""최종 수신 커버리지와 판단 당시 확보율을 구분하는 데이터 품질 보고."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..models import utc
from ..market.bars import session_bounds

NY = ZoneInfo('America/New_York')


def quality_report(path):
    from .bundle import events
    kinds, blockers, latency, reference = Counter(), Counter(), [], Counter()
    days, frames, starts, sources, bars = set(), 0, [], defaultdict(set), defaultdict(set)
    late, quotes, eligibility, eligibility_good, rvol, rvol_good = 0, 0, 0, 0, 0, 0
    decisions, ready, news_total, news_verified, missing_news_versions, news_confirmed = 0, 0, 0, 0, 0, 0
    synthetic, last = False, None
    bounded = False
    frame_last, clock_gaps = None, 0
    session_frames = Counter()
    seen = set(); duplicate_count = 0
    for row in events(path):
        if row['id'] in seen: duplicate_count += 1; continue
        seen.add(row['id']); kinds[row['kind']] += 1
        synthetic = synthetic or row['synthetic']; last = utc(row['available_at'])
        data = row['data']
        bounded = bounded or data.get('recording_mode') == 'bounded_diagnostics'
        if row['kind'] == 'recording_start': starts.append(row)
        if row['kind'] == 'market_reference_observations': reference[data.get('quality_status','UNKNOWN')] += 1
        if row['kind'] in {'news_analysis','macro_analysis'}:
            news_total += 1
            news_verified += int(data.get('source_verified') is True or data.get('primary_verified') is True)
            verified = data.get('source_verified') is True or data.get('primary_verified') is True
            confirmed = data.get('status') == 'CONFIRMED' if row['kind']=='macro_analysis' else (
                isinstance(data.get('confidence'),(int,float)) and data['confidence']>=.75
                and any(item.get('status')=='CONFIRMED' for item in data.get('events',[])))
            news_confirmed += int(verified and confirmed and data.get('requires_verification') is False
                                  and not data.get('missing_information') and not data.get('error'))
            missing_news_versions += int(not data.get('model') or not data.get('prompt_version') or not data.get('analyzed_at'))
        if row['kind'] != 'engine_batch': continue
        frames += 1
        now = utc(data['decision_at']); bounds = session_bounds(now)
        if frame_last and bounds and bounds[0] <= frame_last < now <= bounds[1] and (now-frame_last).total_seconds()>5:clock_gaps += 1
        frame_last = now
        session_frames['NON_TRADING_DAY' if not bounds else 'BEFORE_OPEN' if now<bounds[0] else 'AFTER_CLOSE' if now>bounds[1] else 'REGULAR'] += 1
        if bounds and bounds[0] <= now <= bounds[1]:days.add(now.astimezone(NY).date().isoformat())
        for audit in data.get('decisions',[]):
            decisions += 1; ready += int(audit.get('eligible') is True)
            blockers.update(audit.get('blockers',[]))
        for sample in data.get('quality_rows',[]):
            rvol += 1; rvol_good += int((sample.get('rvol') or {}).get('status') == 'VERIFIED')
        for raw in data['events']:
            value = raw['data']; symbol = value.get('symbol','UNKNOWN')
            if raw['kind'] == 'bar':
                stamp = utc(value['timestamp']); session = session_bounds(stamp)
                sources[symbol].add(value.get('source','UNKNOWN'))
                if session and session[0] < stamp <= session[1]:
                    bars[(symbol,stamp.astimezone(NY).date().isoformat())].add(stamp)
            if raw['kind'] == 'quote':
                quotes += 1
                if bounds:bars.setdefault((symbol,now.astimezone(NY).date().isoformat()),set())
                lag = (utc(value['received_at'])-utc(value['timestamp'])).total_seconds()
                latency.append(lag); late += int(lag > 30)
            if raw['kind'] == 'eligibility':
                from types import SimpleNamespace
                from ..market.eligibility import field_verified
                eligibility += 1
                evidence = SimpleNamespace(**{'field_evidence':{},'account_scope':'UNKNOWN',**value})
                eligibility_good += int(all(field_verified(evidence,k,v,now) for k,v in
                    [('tradable',True),('fractional',True),('halted',False),('corporate_action',False)]))
    rows = []
    for (symbol,day), stamps in sorted(bars.items()):
        bounds = session_bounds(datetime.fromisoformat(day).replace(tzinfo=NY)+timedelta(hours=12))
        end = min(last,bounds[1])
        expected = max(0,int((end-bounds[0]).total_seconds()//60))
        missing = max(0,expected-len(stamps))
        rows.append({'symbol':symbol,'session_date':day,'expected_minutes':expected,'received_minutes':len(stamps),
            'missing_minutes':missing,'missing_rate':missing/expected if expected else None,
            'basis':'received_by_export_cutoff_not_historical_decision_readiness','sources':sorted(sources[symbol])})
    issues = []
    if bounded or any(row['data'].get('auxiliary_storage',{}).get('mode') == 'bounded_diagnostics' for row in starts):
        issues.append('bounded_diagnostic_recording_not_full_replay')
    if not starts or not frames: issues.append('no_causal_engine_recording')
    if any(row['data'].get('strict_market_data') is not True for row in starts):issues.append('strict_market_data_not_enabled')
    if starts and not starts[0]['data']['initial_flat']: issues.append('recording_started_with_untracked_positions_or_orders')
    if len({str(row['data'].get('settings')) for row in starts}) > 1:issues.append('settings_changed_between_runs')
    if any('UNKNOWN' in values or len(values)>1 for values in sources.values()):issues.append('market_source_unknown_or_mixed')
    if any(row['missing_minutes'] for row in rows):issues.append('minute_coverage_incomplete')
    if eligibility_good < eligibility or not eligibility:issues.append('eligibility_unverified')
    if rvol_good < rvol or not rvol:issues.append('historical_rvol_unverified')
    if late or not quotes:issues.append('quotes_delayed_or_missing')
    if missing_news_versions:issues.append('news_analysis_version_unknown')
    return {'trade_dates':sorted(days),'trading_days':len(days),'frames':frames,'event_counts':dict(kinds),
        'synthetic':synthetic,'session_frame_counts':dict(session_frames),'clock_gaps_over_5s':clock_gaps,'recording_starts':len(starts),'recording_stops':kinds['recording_stop'],'duplicate_events':duplicate_count,'symbol_session_coverage':rows,
        'quote_count':quotes,'quote_delay_over_30s':late,'quote_latency_mean_seconds':sum(latency)/len(latency) if latency else None,
        'eligibility_samples':eligibility,'eligibility_verified':eligibility_good,
        'rvol_decision_samples':rvol,'rvol_verified':rvol_good,'decision_samples':decisions,'ready_samples':ready,
        'decision_readiness_rate':ready/decisions if decisions else None,'blocker_counts':dict(blockers),
        'news_analysis_count':news_total,'news_source_verified_count':news_verified,
        'news_fact_confirmed_count':news_confirmed,'news_fact_confirmed_rate':news_confirmed/news_total if news_total else None,
        'news_rate_basis':'당시 저장한 분석 버전 중 출처·확정 사실·추가 검증 불필요 조건을 충족한 비율. 출처 검증률과 구분.',
        'news_missing_versions':missing_news_versions,'reference_quality_counts':dict(reference),
        'unliquidated_count':None,'unliquidated_note':'미청산은 재생 정책별 실제 모의 잔량으로 평가합니다.',
        'blocking_reasons':issues,'data_validation':'SYNTHETIC' if synthetic else 'UNVERIFIED_DATA'}

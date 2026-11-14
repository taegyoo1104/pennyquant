"""현재 감시 대상의 확보율과 차단 종류를 장부·화면·조회 명령에 공유한다."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

from ..models import utc
from .quality import QUALITY_KEY, bar_quality, quality_summary


def current_report(report, now=None):
    now = utc(now or datetime.now(timezone.utc))
    report = deepcopy(report or {})
    rows = report.get('items',[])
    for row in rows:
        try:
            stale = not 0 <= (now-utc(row['observed_at'])).total_seconds() <= 30
        except (TypeError,ValueError,KeyError,AttributeError):
            stale = True
        if stale:
            row.update(eligibility_verified=False,quote_fresh=False)
            row['bars'] = {**row['bars'],'status':'STALE','five_coverage':None,'fifteen_coverage':None}
            row['rvol'] = {**row['rvol'],'value':None,'status':'UNKNOWN'}
            row['blockers'] = list(dict.fromkeys([*row.get('blockers',[]),'quality_observation_stale']))
        else:
            bars = row['bars']
            if bars.get('session_open') and bars.get('session_close'):
                elapsed = max(0,int((min(now,utc(bars['session_close']))-utc(bars['session_open'])).total_seconds()//60))
                if elapsed > bars.get('expected_minutes',0):
                    row['bars'] = {**bars,'status':'STALE','five_coverage':None,'fifteen_coverage':None}
                    row['blockers'] = list(dict.fromkeys([*row.get('blockers',[]),'new_closed_minute_not_evaluated']))
            quote_at = row.get('quote_at')
            if not quote_at or (now-utc(quote_at)).total_seconds() > 30:
                row['quote_fresh'] = False
                row['blockers'] = list(dict.fromkeys([*row.get('blockers',[]),'quote_stale']))
            evidence = row.get('eligibility') or {}
            from .eligibility import field_verified
            try:
                verified = bool(evidence.get('valid_until') and utc(evidence['valid_until']) >= now
                    and 0 <= (now-utc(evidence['checked_at'])).total_seconds() <= row.get('eligibility_max_age_seconds',60)
                    and all(field_verified(SimpleNamespace(**evidence),name,value,now)
                        for name,value in [('tradable',True),('fractional',True),('halted',False),('corporate_action',False)]))
            except (TypeError,ValueError,KeyError,AttributeError):
                verified = False
            if not verified:
                row['eligibility_verified'] = False
                row['blockers'] = list(dict.fromkeys([*row.get('blockers',[]),'eligibility_unverified']))
            if row['rvol'].get('valid_until') and utc(row['rvol']['valid_until']) < now:
                row['rvol'].update(value=None,status='UNKNOWN')
    refreshed = quality_summary(rows, now, source_state=report.get('source_state'),synthetic=report.get('synthetic',False))
    refreshed['captured_at'] = report.get('observed_at')
    refreshed['market_reference_quality'] = report.get('market_reference_quality',{})
    refreshed['scope'] = report.get('scope','NO_EVALUATION')
    return refreshed


def persist_quality(engine, now):
    manager = engine.watchlist_policy
    focus = manager.focus(now) if manager and hasattr(manager,'focus') else []
    priority = list(dict.fromkeys([*engine.broker.positions,*engine.broker.exit_intents,
        *(order.intent.symbol for order in engine.broker.get_open_orders())]))
    targets = list(dict.fromkeys([*priority,*focus])) if manager else list(engine.market.quality_rows)
    rows = []
    for symbol in targets:
        row = engine.market.quality_rows.get(symbol)
        if row is None:
            row = {'symbol':symbol,'observed_at':now.isoformat(),'eligibility_verified':False,'quote_fresh':False,
                'bars':{'status':'UNKNOWN','five_coverage':None,'fifteen_coverage':None},
                'rvol':{'status':'UNKNOWN','value':None,'reasons':['symbol_not_yet_evaluated']},
                'blockers':['symbol_not_yet_evaluated','eligibility_unverified','relative_volume_unknown_or_low','quote_missing']}
        rows.append(row)
    report = quality_summary(rows,now,source_state=manager.state if manager else {},synthetic=engine.synthetic)
    # 가격 이력·집중 근거 전체를 중복 저장하지 않고 수집 계측만 연결한다.
    report['source_state'] = {k:v for k,v in report['source_state'].items() if k in {
        'last_cycle_calls','cycle_call_budget','last_cycle_duration_seconds','backoff_until',
        'read_measurements','priority_delivered_at','priority_phase_seconds','budget_exhausted','history_progress',
        'provider_read_observations','candle_observations'}}
    report['scope'] = 'focus_and_risk_priority' if manager else 'evaluated_symbols'
    report['market_reference_quality'] = {s:bar_quality(engine.market.bars.get(s,[]),s,now) for s in ['SPY','QQQ']}
    current = current_report(report,now)
    previous = engine.repo.get_control(QUALITY_KEY,{})
    engine.repo.set_control(QUALITY_KEY,current)
    if previous.get('blocker_counts') != current['blocker_counts']:
        engine.repo.log('INFO','시장 데이터 확보율·진입 차단 사유가 갱신되었습니다.',
                        blocker_counts=current['blocker_counts'],required_data_coverage=current['required_data_coverage'])

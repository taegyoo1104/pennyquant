"""Retain old observations for diagnosis while all expired entry gates stay closed."""
from copy import deepcopy
from datetime import timedelta

from sessionquant.dashboard.market_quality_panel import quality_observation_fields, coverage_display
from sessionquant.market.reporting import current_report
from test_market_data_quality import NOW


def report():
    return {'observed_at':NOW.isoformat(),'items':[{
        'symbol':'TEST','observed_at':NOW.isoformat(),'quote_at':NOW.isoformat(),
        'quote_received_at':NOW.isoformat(),'eligibility_verified':False,'quote_fresh':True,
        'bars':{'status':'COMPLETE','five_coverage':1,'fifteen_coverage':1},
        'rvol':{'status':'UNKNOWN','calculated_value':1.25,'cutoff_at':NOW.replace(second=0).isoformat()},
        'strategy_rvol':1.25,'strategy_rvol_status':'OBSERVED_PAPER','blockers':[]}]}


def test_expiry_preserves_observation_through_repeated_persistence_without_enabling_entry():
    source=report();original=deepcopy(source)
    first=current_report(source,NOW+timedelta(seconds=31))
    second=current_report(first,NOW+timedelta(seconds=60))
    assert source==original
    for current in [first,second]:
        row=current['items'][0]
        assert all(v['verified']==0 for v in current['required_data_coverage'].values())
        assert row['strategy_rvol'] is None and row['rvol']['calculated_value'] is None
        assert row['last_evaluation']['rvol_calculated_value']==1.25
        fields=quality_observation_fields(row,NOW+timedelta(seconds=60))
        assert fields['평가 당시 RVOL·참고']==1.25 and fields['평가 당시 15분봉']=='확보'
        assert fields['호가 갱신 상태']=='새 호가 수신 대기'


def test_quote_receipt_distinguishes_old_provider_time_from_no_recent_receipt():
    row=report()['items'][0]
    row['quote_at']=(NOW-timedelta(minutes=4)).isoformat()
    fields=quality_observation_fields(row,NOW)
    assert fields['호가 갱신 상태']=='수신 당시 호가 시각 오래됨'
    assert fields['수신 당시 호가 경과(초)']==240 and fields['호가 수신 후 경과(초)']==0
    row['quote_received_at']=None
    fields=quality_observation_fields(row,NOW)
    assert fields['호가 갱신 상태']=='시각 미확인' and fields['수신 당시 호가 경과(초)'] is None


def test_invalid_or_future_evaluation_cannot_create_historical_success():
    for stamp in [None,'invalid',(NOW+timedelta(seconds=1)).isoformat()]:
        source=report();source['items'][0]['observed_at']=stamp
        row=current_report(source,NOW)['items'][0]
        assert row['evaluation_age_seconds'] is None and not row.get('last_evaluation')
        assert quality_observation_fields(row,NOW)['평가 당시 RVOL·참고'] is None


def test_dashboard_distinguishes_expired_evaluation_from_verified_zero():
    live = current_report(report(), NOW)
    assert coverage_display(live, 'quote') == '1 / 1'
    assert coverage_display(live, 'rvol') == '0 / 1'  # 계산값과 비교 근거 검증은 다르다.
    assert coverage_display(live, 'strategy_rvol') == '1 / 1'
    stale = current_report(report(), NOW+timedelta(seconds=31))
    original = deepcopy(stale)
    assert all(coverage_display(stale, key) == '— / 1' for key in stale['required_data_coverage'])
    assert stale == original and stale['items'][0]['strategy_rvol'] is None
    assert coverage_display(stale, 'strategy_rvol') == '— / 1'
    missing = report()
    missing['items'][0]['bars']['five_coverage'] = 0
    assert coverage_display(current_report(missing, NOW), 'five_bars') == '0 / 1'
    assert coverage_display(current_report({}, NOW), 'quote') == '미평가'


def test_partial_expiry_retains_denominator_without_claiming_unknown_success():
    source = report()
    old = deepcopy(source['items'][0])
    old.update(symbol='OLD', observed_at=(NOW-timedelta(seconds=31)).isoformat())
    source['items'].append(old)
    current = current_report(source, NOW)
    assert coverage_display(current, 'quote') == '1 / 2'
    assert coverage_display(current, 'strategy_rvol') == '1 / 2'
    assert current['items'][1]['strategy_rvol'] is None


def test_schedule_waiting_text_does_not_claim_data_collection_is_incomplete():
    from datetime import datetime, timezone
    from sessionquant.dashboard.labels import market_hours_label
    before = datetime(2026, 11, 18, 15, 41, tzinfo=timezone.utc)
    label = market_hours_label(before)
    assert '00:45 KST' in label and '매수 시작 대기' in label
    assert '자료 수집' not in label
    assert '매수 조건 확인 시간' in market_hours_label(before+timedelta(minutes=5))

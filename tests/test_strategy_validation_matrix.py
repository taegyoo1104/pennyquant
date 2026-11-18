"""확정봉 기반 네 전략의 독립 조건과 신호 중복·시각 경계를 검증한다."""
from dataclasses import replace
from datetime import timedelta

import pytest

from sessionquant.market.bars import indicators
from sessionquant.market.strategies import technical_signal
from test_session_market import CASES, NOW, OPEN, engine, minutes


def data_for(strategy):
    return indicators(minutes(closes=CASES[strategy]),NOW)


def changed_bar(bar, close):
    return replace(bar,open=close,close=close,low=close-.15,high=close+.15)


@pytest.mark.parametrize('strategy',list(CASES))
def test_four_single_strategies_use_closed_signal_id_and_common_risk_gates(strategy):
    data = data_for(strategy)
    diagnostics = []
    one = technical_signal('AAPL',strategy,data,'BULLISH',NOW,diagnostics=diagnostics)
    assert diagnostics == []
    two = technical_signal('AAPL',strategy,data,'BULLISH',NOW+timedelta(seconds=1))
    assert one and two and one.id==two.id and one.timestamp==data.five[-1].timestamp
    assert one.stop_price<one.reference_price
    for regime in ['BEARISH','UNKNOWN','NEUTRAL']:
        assert technical_signal('AAPL',strategy,data,regime,NOW) is None
    blocked = engine(strategy).evaluate('AAPL',NOW,news_blocked=True)
    assert blocked.signal is None and 'confirmed_news_entry_restriction' in blocked.blockers


@pytest.mark.parametrize('strategy',list(CASES))
@pytest.mark.parametrize('case',['older_five_future_receipt','fifteen_future_receipt','fifteen_future_close',
                               'future_calculation','wrong_vwap_cutoff','symbol_mismatch','overchase','invalid_atr'])
def test_every_strategy_rejects_future_mixed_or_overextended_inputs(strategy,case):
    data = data_for(strategy)
    five,fifteen = list(data.five),list(data.fifteen)
    if case=='older_five_future_receipt':five[0]=replace(five[0],received_at=NOW+timedelta(seconds=1));data=replace(data,five=tuple(five))
    if case=='fifteen_future_receipt':fifteen[0]=replace(fifteen[0],received_at=NOW+timedelta(seconds=1));data=replace(data,fifteen=tuple(fifteen))
    if case=='fifteen_future_close':
        fifteen[-1]=replace(fifteen[-1],timestamp=NOW+timedelta(minutes=15),received_at=NOW+timedelta(minutes=15));data=replace(data,fifteen=tuple(fifteen))
    if case=='future_calculation':data=replace(data,calculated_at=NOW+timedelta(seconds=1))
    if case=='wrong_vwap_cutoff':data=replace(data,vwap_through=data.five[-2].timestamp)
    if case=='symbol_mismatch':five[0]=replace(five[0],symbol='MSFT');data=replace(data,five=tuple(five))
    if case=='overchase':data=replace(data,atr14=data.atr14/100)
    if case=='invalid_atr':data=replace(data,atr14=float('nan'))
    assert technical_signal('AAPL',strategy,data,'BULLISH',NOW) is None


@pytest.mark.parametrize('strategy',['momentum','pullback','vwap'])
def test_trend_strategies_require_independent_fifteen_minute_rising_structure(strategy):
    data = data_for(strategy)
    bars = list(data.fifteen)
    bars[-1]=changed_bar(bars[-1],bars[-2].close)
    assert technical_signal('AAPL',strategy,replace(data,fifteen=tuple(bars)),'BULLISH',NOW) is None


@pytest.mark.parametrize('case',['not_above_recent_high','volume_not_confirmed'])
def test_momentum_independently_checks_high_breakout_and_volume(case):
    data = data_for('momentum')
    if case=='not_above_recent_high':
        bars=list(data.five);bars[-1]=changed_bar(bars[-1],max(b.high for b in bars[-5:-1]));data=replace(data,five=tuple(bars))
    else:data=replace(data,volume_ratio=1.19)
    assert technical_signal('AAPL','momentum',data,'BULLISH',NOW) is None


@pytest.mark.parametrize('case',['first_not_pullback','second_not_pullback','rebound_not_breaking_high','rebound_volume_low','excessive_retracement'])
def test_pullback_two_adjustments_rebound_and_depth_are_independent(case):
    data=data_for('pullback');bars=list(data.five)
    if case=='first_not_pullback':bars[-3]=changed_bar(bars[-3],bars[-4].close+.01)
    if case=='second_not_pullback':bars[-2]=changed_bar(bars[-2],bars[-3].close+.01)
    if case=='rebound_not_breaking_high':bars[-1]=changed_bar(bars[-1],bars[-2].high)
    if case=='rebound_volume_low':bars[-1]=replace(bars[-1],volume=bars[-2].volume)
    if case=='excessive_retracement':bars[-6]=replace(bars[-6],high=bars[-1].close+5*data.atr14)
    diagnostics = []
    assert technical_signal('AAPL','pullback',replace(data,five=tuple(bars)),'BULLISH',NOW,
                            diagnostics=diagnostics) is None
    expected = {
        'first_not_pullback':'strategy_first_pullback_missing',
        'second_not_pullback':'strategy_second_pullback_missing',
        'rebound_not_breaking_high':'strategy_rebound_high_breakout_missing',
        'rebound_volume_low':'strategy_rebound_volume_missing',
        'excessive_retracement':'strategy_pullback_depth_invalid',
    }
    assert expected[case] in diagnostics


@pytest.mark.parametrize('support',['vwap','ema'])
def test_pullback_accepts_either_verified_support_instead_of_only_the_higher_line(support):
    data=data_for('pullback');low=min(data.five[-3].low,data.five[-2].low)
    if support=='vwap':data=replace(data,vwap=low,ema9=low+1.5*data.atr14)
    else:data=replace(data,vwap=low-1.5*data.atr14,ema9=low)
    signal=technical_signal('AAPL','pullback',data,'BULLISH',NOW)
    assert signal and signal.allow_add


def test_opening_range_uses_only_first_fifteen_or_thirty_minutes_and_requires_coverage():
    closes=[100,100.05,100.1]+[100.1]*14+[100.5]
    raw=minutes(closes=closes)
    # 첫 15분 밖·첫 30분 안의 고점은 두 OR 정의에 서로 다르게 반영된다.
    raw=[replace(b,high=100.8) if OPEN+timedelta(minutes=15)<b.timestamp<=OPEN+timedelta(minutes=20) else b for b in raw]
    now=OPEN+timedelta(minutes=90)
    data=indicators(raw,now)
    assert technical_signal('AAPL','gap_breakout',data,'BULLISH',now,opening_range_minutes=15)
    assert technical_signal('AAPL','gap_breakout',data,'BULLISH',now,opening_range_minutes=30) is None
    assert technical_signal('AAPL','gap_breakout',replace(data,five=data.five[1:]),'BULLISH',now) is None


@pytest.mark.parametrize('case',['previous_above_vwap','current_not_reclaimed','previous_vwap_from_current_bar','no_high_confirmation'])
def test_vwap_reclaim_uses_previous_and_current_cutoffs_with_price_confirmation(case):
    data=data_for('vwap')
    if case=='previous_above_vwap':data=replace(data,previous_vwap=data.five[-2].close-.01)
    if case=='current_not_reclaimed':data=replace(data,vwap=data.five[-1].close)
    if case=='previous_vwap_from_current_bar':data=replace(data,previous_vwap_through=data.five[-1].timestamp)
    if case=='no_high_confirmation':
        bars=list(data.five);bars[-2]=replace(bars[-2],high=bars[-1].close+.01);data=replace(data,five=tuple(bars))
    assert technical_signal('AAPL','vwap',data,'BULLISH',NOW) is None


def test_signal_future_incomplete_minute_never_changes_closed_strategy_early():
    raw=minutes(closes=CASES['momentum'])
    data=indicators(raw,NOW)
    future=replace(raw[-1],timestamp=NOW+timedelta(minutes=1),received_at=NOW+timedelta(minutes=1),high=1000)
    assert technical_signal('AAPL','momentum',indicators(raw+[future],NOW),'BULLISH',NOW).id==technical_signal('AAPL','momentum',data,'BULLISH',NOW).id


def test_diagnostics_report_every_unmet_pullback_condition_without_changing_signal():
    data = data_for('pullback')
    five, fifteen = list(data.five), list(data.fifteen)
    five[-1] = replace(five[-1], volume=five[-2].volume)
    fifteen[-1] = changed_bar(fifteen[-1], fifteen[-2].close)
    waiting = replace(data, five=tuple(five), fifteen=tuple(fifteen))
    diagnostics = []
    assert technical_signal('AAPL','pullback',waiting,'BULLISH',NOW) is None
    assert technical_signal('AAPL','pullback',waiting,'BULLISH',NOW,diagnostics=diagnostics) is None
    assert diagnostics == ['strategy_fifteen_minute_uptrend_missing', 'strategy_rebound_volume_missing']
    from sessionquant.dashboard.labels import label
    assert [label(reason) for reason in diagnostics] == ['15분봉 상승 추세 대기', '반등 거래량 증가 대기']


def test_strategy_recent_volume_diagnostic_does_not_call_it_historical_rvol_failure():
    diagnostics = []
    data = replace(data_for('pullback'), volume_ratio=.99)
    assert technical_signal('AAPL','pullback',data,'BULLISH',NOW,diagnostics=diagnostics) is None
    assert diagnostics == ['strategy_recent_volume_below_minimum']


def test_regime_and_pullback_accept_sequential_current_quotes_without_etf_history():
    # Live reads arrive sequentially. They need freshness, not identical timestamps
    # or 20 days of ETF history (only the stock RVOL needs that history).
    current = engine('pullback')
    for offset, symbol in enumerate(('SPY', 'QQQ', 'AAPL'), start=1):
        current.quotes[symbol] = replace(current.quotes[symbol],
            timestamp=NOW + timedelta(seconds=offset), received_at=NOW + timedelta(seconds=offset + 1))
    current.quotes['SPY'] = replace(current.quotes['SPY'],tradable=False)
    current.quotes['QQQ'] = replace(current.quotes['QQQ'],tradable=False)
    decision = current.evaluate('AAPL',NOW + timedelta(seconds=20))
    assert decision.regime == 'BULLISH'
    assert decision.eligible and decision.signal
    assert {bar.timestamp.date() for values in current.bars.values() for bar in values} == {NOW.date()}
    stale = current.evaluate('AAPL',NOW + timedelta(seconds=32))
    assert stale.regime == 'UNKNOWN' and stale.signal is None
    assert 'market_unknown' in stale.blockers

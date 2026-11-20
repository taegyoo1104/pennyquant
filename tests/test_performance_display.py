from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from sessionquant.dashboard.performance_display import (
    two_decimals, performance_rows, daily_performance_points, daily_performance_rows, daily_chart_spec,
)


@pytest.mark.parametrize('value,expected', [('100.01540387802','100.01'),('-1.239','-1.23'),
    ('0.009','0.00'),('-0.009','0.00'),('NaN','—'),(None,'—'),('Infinity','—')])
def test_display_truncates_toward_zero_without_rounding(value,expected):
    assert two_decimals(value)==expected


def test_performance_format_preserves_identifiers_counts_and_original_precision():
    rows=[{'id':'123.456','equity':'100.01540387802','synthetic':False,'count':7,
           'metrics':{'return':Decimal('1.23456')},'timestamp':'2026-11-19T16:00:00Z'}]
    original=deepcopy(rows)
    visible=performance_rows(rows)
    assert rows==original
    assert visible[0]['id']=='123.456' and visible[0]['equity']=='100.01'
    assert visible[0]['count']==7 and visible[0]['synthetic'] is False
    assert visible[0]['metrics']['return']=='1.23'


NOW = datetime(2026,11,20,9,3,tzinfo=timezone.utc)


def daily_record(day='2026-11-19', **changes):
    return {'id':day, 'timestamp':'2026-11-20T03:59:00Z', 'equity':'99.4648',
            'day_start_equity':'100', 'realized_pnl':'-0.5352', 'halted':False, **changes}


def test_daily_records_keep_us_dates_and_cumulative_dollars_distinct_from_daily_returns():
    rows=[daily_record('2026-11-20',timestamp='2026-11-20T09:02:00Z',day_start_equity='99.4648'),
          daily_record(timestamp='2026-11-19T16:00:00Z',equity='101'), daily_record()]
    original=deepcopy(rows)
    points=daily_performance_points(rows,NOW)
    assert [p['거래일'] for p in points] == ['2026-11-19','2026-11-20']
    assert points[0]['총자산']==99.4648 and points[0]['총자산 표시']=='$99.46'
    # Calculate before display truncation: 99.46/100 would incorrectly show -0.54%.
    assert points[0]['일간 수익률']==pytest.approx(-0.5352)
    assert points[0]['수익률 표시']=='-0.53%'
    assert points[1]['수익률 표시']=='0.00%'
    assert points[0]['누적 실현손익']==points[1]['누적 실현손익']=='-$0.53'
    visible=daily_performance_rows(points)
    assert visible[0]['거래일 (미국)']=='2026-11-20'
    assert visible[0]['하루 시작 자산 ($)']=='$99.46'
    assert visible[1]['마지막 기록 (KST)']=='2026-11-20 12:59:00'
    assert visible[1]['신규 매수 중지']=='아니요'
    assert rows==original


@pytest.mark.parametrize('base',[None,'0','-10','NaN','bad',True])
def test_missing_daily_base_is_not_treated_as_zero_return_or_previous_day(base):
    point=daily_performance_points([daily_record(day_start_equity=base)],NOW)[0]
    assert point['총자산']==99.4648
    assert point['일간 수익률'] is None and point['수익률 표시']=='—'


def test_daily_view_rejects_future_ambiguous_and_mismatched_dates():
    rows=[daily_record('not-a-date'), daily_record('2026-11-21',timestamp='2026-11-21T15:00:00Z'),
          daily_record(timestamp='2026-11-19T17:00:00'), daily_record(timestamp=None),
          daily_record('2026-11-20'), daily_record()]
    assert len(daily_performance_points(rows,NOW))==1


def test_missing_latest_equity_does_not_reuse_older_value_and_zero_equity_is_valid():
    earlier=daily_record(timestamp='2026-11-19T16:00:00Z',equity='100')
    points=daily_performance_points([earlier,daily_record(equity='NaN',halted=True)],NOW)
    assert len(points)==1 and points[0]['총자산'] is None
    assert points[0]['수익률 표시']=='—' and points[0]['신규 매수 중지']=='예'
    point=daily_performance_points([daily_record(equity='0')],NOW)[0]
    assert point['총자산 표시']=='$0.00' and point['수익률 표시']=='-100.00%'


def test_daily_charts_show_single_day_points_and_percentage_units_without_timezone_shift():
    assets=daily_chart_spec('총자산')
    assert assets['mark']['point'] is True
    assert assets['encoding']['x']['type']=='ordinal'
    assert assets['encoding']['y']['title']=='총자산 ($)'
    returns=daily_chart_spec('일간 수익률')
    assert returns['encoding']['y']['title']=='일간 수익률 (%)'
    assert returns['encoding']['tooltip'][1]['field']=='수익률 표시'


def render_daily_fixture():
    from sessionquant.dashboard.overview import render_strategies
    from sessionquant.dashboard.session_app import DashboardData
    data=DashboardData()
    data.tables['daily_performance']=[{
        'id':'2025-11-19','timestamp':'2025-11-19T21:05:00Z','equity':'99.4648',
        'day_start_equity':'100','realized_pnl':'-0.5352','halted':False}]
    render_strategies(data,{})


def test_strategy_screen_and_csv_have_meaningful_headers_and_units():
    from streamlit.testing.v1 import AppTest
    app=AppTest.from_function(render_daily_fixture,default_timeout=30).run()
    assert not app.exception
    assert len(app.get('vega_lite_chart'))==2
    frame=app.dataframe[0].value
    exported=frame.to_csv(index=False)
    assert '추가 정보' not in exported and '추가정보' not in exported
    assert frame.iloc[0]['총자산 ($)']=='$99.46'
    assert frame.iloc[0]['누적 실현손익 ($)']=='-$0.53'
    assert frame.iloc[0]['일간 수익률 (%)']=='-0.53%'
    assert frame.iloc[0]['신규 매수 중지']=='아니요'

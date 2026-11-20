from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from sessionquant.dashboard.performance_display import two_decimals, performance_rows, asset_curve_points, asset_chart_spec


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


def test_drawdown_is_percentage_and_chart_coordinates_keep_full_precision():
    now=datetime(2026,11,19,16,tzinfo=timezone.utc)
    rows=[{'timestamp':'2026-11-19T15:59:00Z','equity':'100'},
          {'timestamp':now.isoformat(),'equity':'99.7501'}]
    original=deepcopy(rows)
    points=asset_curve_points(rows,now)
    assert points[-1]['장부 평가자산']==99.7501 and points[-1]['평가자산 표시']=='99.75 USD'
    assert points[-1]['장부 낙폭']==pytest.approx(-0.2499) and points[-1]['낙폭 표시']=='-0.24%'
    assert rows==original
    spec=asset_chart_spec('장부 낙폭')
    assert spec['encoding']['y']['title']=='장부 낙폭 (%)'
    assert spec['encoding']['tooltip'][-1]['field']=='낙폭 표시'
    assert asset_curve_points([{'timestamp':now.isoformat(),'equity':'0'}],now)[0]['낙폭 표시']=='—'

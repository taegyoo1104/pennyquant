"""장전·미수집·잘못된 시각을 빈 거래 값으로 표시하고 화면 갱신을 계속한다."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.dashboard.market_quality_panel import watchlist_rvol_rows
from sessionquant.market.history import current_view
from sessionquant.market.quality import rvol_report
from sessionquant.models import utc

NOW = datetime(2026, 11, 18, 15, 0, 10, tzinfo=timezone.utc)


def calculated_row(symbol='AAPL'):
    return {'symbol': symbol, 'cutoff_at': NOW.replace(second=0).isoformat(),
            'observed_at': NOW.isoformat(), 'calculated_valid_until': (NOW+timedelta(seconds=30)).isoformat(),
            'value': 1.25, 'calculated_value': 1.25, 'status': 'VERIFIED', 'calculation_status': 'CALCULATED',
            'complete_sessions': 20, 'reasons': []}


@pytest.mark.parametrize('field', ['cutoff_at', 'observed_at', 'calculated_valid_until'])
@pytest.mark.parametrize('value', [None, '', 'not-a-date', 123, False, {}, [], '2026-11-18T15:00:00'])
def test_invalid_timestamp_clears_current_values_without_affecting_other_symbols(field, value):
    bad, good = calculated_row(), calculated_row('MSFT')
    bad[field] = value
    source = {'observed_at': NOW.isoformat(), 'items': [bad, good]}
    original = deepcopy(source)
    current = current_view(source, NOW)
    assert current['items'][0]['fresh'] is False
    assert current['items'][0]['value'] is None
    assert current['items'][0]['calculated_value'] is None
    assert current['items'][0]['status'] == 'UNKNOWN'
    rows = watchlist_rvol_rows(source, NOW)
    assert rows[0]['RVOL 계산값'] is None and rows[0]['매매용 검증값'] is None
    if field in {'cutoff_at', 'observed_at'}:
        assert rows[0]['마지막 계산값·참고'] is None and rows[0]['기준 시각 경과(초)'] is None
        assert rows[0]['현재성'] == '시각 미확인'
    assert rows[1]['RVOL 계산값'] == 1.25 and rows[1]['현재성'] == '최신'
    assert source == original


@pytest.mark.parametrize('field', ['cutoff_at', 'observed_at', 'calculated_valid_until'])
def test_missing_field_is_not_replaced_with_collector_heartbeat(field):
    row = calculated_row()
    del row[field]
    result = current_view({'observed_at': NOW.isoformat(), 'items': [row]}, NOW)['items'][0]
    assert not result['fresh'] and result['value'] is None and result['calculated_value'] is None
    assert watchlist_rvol_rows({'items': [row]}, NOW)[0]['RVOL 계산값'] is None


@pytest.mark.parametrize('field,value', [
    ('observed_at', NOW+timedelta(seconds=1)),
    ('observed_at', NOW-timedelta(seconds=11)),
    ('cutoff_at', NOW+timedelta(seconds=50)),
    ('calculated_valid_until', NOW-timedelta(seconds=1)),
])
def test_inconsistent_or_expired_time_does_not_show_current_rvol(field, value):
    row = calculated_row()
    row[field] = value.isoformat()
    result = watchlist_rvol_rows({'items': [row]}, NOW)[0]
    assert result['RVOL 계산값'] is None and result['매매용 검증값'] is None


@pytest.mark.parametrize('value', [None, 0, True, [], {}, datetime(2026, 11, 18)])
def test_utc_rejects_invalid_values_as_validation_errors(value):
    with pytest.raises(ValueError, match='timezone-aware timestamp required'):
        utc(value)


def test_utc_requires_an_actual_offset_and_converts_valid_offsets():
    class MissingOffset(tzinfo):
        def utcoffset(self, dt):
            return None
    with pytest.raises(ValueError):
        utc(datetime(2026, 11, 18, tzinfo=MissingOffset()))
    assert utc('2026-11-19T00:00:10+09:00') == NOW
    assert utc(NOW) == NOW


def test_preopen_report_with_null_cutoff_renders_entire_market_page(tmp_path, monkeypatch):
    from sessionquant.database.repository import Repository
    preopen = datetime(2026, 11, 18, 6, tzinfo=timezone.utc)
    row = {'symbol': 'AAPL', **rvol_report([], 'AAPL', None, preopen)}
    assert row['cutoff_at'] is None
    assert row['reasons'] == ['current_closed_interval_missing']
    path = tmp_path/'preopen.db'
    repo = Repository(path, backend='sqlite')
    repo.set_control('sessionquant_context', {'environment': 'PAPER_LOCAL'})
    repo.set_control('watchlist_rvol_v1', {'items': [row, {'symbol': 'KO', 'reasons': ['symbol_not_yet_collected']}]})
    repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND', 'sqlite')
    monkeypatch.setenv('SQ_DB', str(path))
    monkeypatch.setattr('sys.argv', ['session_app.py', '--db', str(path)])
    app_path = Path(__file__).resolve().parents[1]/'sessionquant/dashboard/session_app.py'
    app = AppTest.from_file(str(app_path), default_timeout=30).run()
    app.radio(key='dashboard_page').set_value('시장·종목').run()
    assert not app.exception and not app.error
    assert any('별도 일괄 수집 기록 없음' in r.value for r in app.caption)
    assert not any('0 / 0' in r.value for r in app.caption)
    assert any(r.value == '기술 분석 이력과 진입 근거' for r in app.subheader)
    # 새로고침으로 같은 미수집 기록을 다시 읽어도 아래 표까지 표시한다.
    app.run()
    assert not app.exception and not app.error

from copy import deepcopy
from pathlib import Path

from streamlit.testing.v1 import AppTest

from sessionquant.dashboard.labels import display_rows
from sessionquant.dashboard.system_panel import pipeline_rows
from sessionquant.database.repository import Repository


STAMP = '2026-11-15T09:39:58.492020+00:00'


def test_processing_records_keep_real_results_and_each_record_timestamp():
    records = [
        {'kind': 'watchlist_focus', 'known_at': STAMP, 'data_status': 'VALID', 'items': []},
        {'kind': 'watchlist_history_selection', 'checked_at': STAMP, 'included_count': 26, 'excluded_count': 24},
        {'kind': 'portfolio_selection', 'evaluated_at': STAMP, 'evaluated_count': 20, 'approved_count': 0},
        {'kind': 'risk_decision', 'timestamp': STAMP, 'symbol': 'AAPL', 'approved': False,
         'reason': 'daily_loss_limit'},
        {'kind': 'macro_reference_risk', 'observed_at': STAMP, 'status': 'UNKNOWN', 'effective_enabled': False},
        {'kind': 'reference_entry_block', 'timestamp': STAMP, 'symbol': 'NVDA'},
    ]
    original = deepcopy(records)
    visible, unknown = display_rows(pipeline_rows(records))
    assert records == original
    assert not unknown
    assert all(row['기록 시각'] == '11.15 18:39:58 KST' for row in visible)
    assert len({row['작업'] for row in visible}) == 6
    assert visible[0]['내용'] == '집중 분석 대상 0종목'
    assert visible[1]['내용'] == '자료 준비 26종목 · 자료 부족으로 제외 24종목'
    assert visible[2]['내용'] == '평가 20종목 · 매수 승인 0종목'
    assert visible[3]['결과'] == '매수 보류'
    assert visible[3]['내용'] == '하루 손실 한도 도달'
    assert visible[4]['결과'] == '판단 자료 부족'
    assert visible[4]['내용'] == '참고용 · 매매에 적용 안 함'


def test_missing_fields_do_not_invent_success_counts_or_timestamps():
    rows, unknown = display_rows(pipeline_rows([
        {'kind': 'future_internal_task', 'status': 'NEW_STATUS'},
        {'kind': 'watchlist_history_selection'},
        {'kind': 'risk_decision'},
    ]))
    assert not unknown
    assert rows[0]['작업'] == '기타 처리 기록'
    assert rows[0]['결과'] == '원본 기록에서 확인'
    assert rows[1]['내용'] == '집계 미기록'
    assert rows[2]['결과'] == '승인 여부 미기록'
    assert all(row['기록 시각'] == '—' for row in rows)


def test_system_page_renders_processing_summary_and_preserves_full_original(tmp_path, monkeypatch):
    path = tmp_path / 'system.db'
    record = {'id': 'focus-test', 'kind': 'watchlist_focus', 'known_at': STAMP,
              'data_status': 'UNKNOWN', 'items': [], 'watchlist_id': 'original-detail'}
    repo = Repository(path, backend='sqlite')
    repo.record('pipeline_status', record)
    repo.close()
    monkeypatch.setenv('SQ_DB_BACKEND', 'sqlite')
    monkeypatch.setenv('PQ_DB_BACKEND', 'sqlite')
    monkeypatch.setattr('sys.argv', ['session_app.py', '--db', str(path)])
    app = AppTest.from_file(str(Path(__file__).parents[1] / 'sessionquant/dashboard/session_app.py'),
                            default_timeout=30).run()
    app.radio(key='dashboard_page').set_value('시스템·설정').run()
    assert not app.exception
    table = next(frame.value for frame in app.dataframe if '작업' in frame.value.columns)
    assert table.iloc[0]['작업'] == '집중 분석 종목 선정'
    assert table.iloc[0]['기록 시각'] == '11.15 18:39:58 KST'
    assert any('original-detail' in str(item.value) for item in app.json)
    repo = Repository(path, backend='sqlite')
    assert repo.all('pipeline_status') == [record]
    assert not repo.all('orders') and not repo.all('order_intents')
    repo.close()

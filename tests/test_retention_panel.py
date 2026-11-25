from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant.dashboard.retention_panel import retention_summary


NOW = datetime(2026, 11, 20, 21, tzinfo=timezone.utc)


def report(**observability):
    return {'status': 'COMPLETE', 'started_at': (NOW - timedelta(hours=2)).isoformat(),
            'scanned': 50000, 'deleted': 30000, 'policy': {'max_rows': 50000},
            'observability': observability}


@pytest.mark.parametrize('value', [None, {}, [], 'invalid'])
def test_missing_reports_do_not_invent_zero_or_completion(value):
    summary = retention_summary(value, NOW)
    assert not summary['available'] and summary['headline'] == '정리 기록 없음'
    assert summary['backlog'] == summary['scanned'] == summary['deleted'] == '미확인'


def test_legacy_complete_only_describes_a_pass_not_empty_backlog():
    saved = report()
    saved.pop('observability')
    summary = retention_summary(saved, NOW)
    assert summary['pass_status'] == '검사 순회 완료'
    assert summary['headline'] == '남은 정리 대상 미확인' and summary['backlog'] == '미확인'
    assert summary['age'] == '2.0시간 전'
    assert summary['scanned'] == '50,000행' and summary['deleted'] == '30,000행'


def test_present_backlog_and_capped_generation_are_minimum_counts_without_rates():
    saved = report(backlog_status='PRESENT', eligible_backlog_min=12, oldest_eligible_age_days=9.5,
                   stop_reason='PASS_COMPLETE', attention='BACKLOG')
    saved['tables'] = {'quote_snapshots': {'deleted': 100, 'observation': {
        'backlog_status': 'PRESENT', 'eligible_rows_min': 12, 'backlog_exact': False,
        'oldest_eligible_age_days': 9.5, 'generation': {
            'rows_min': 200, 'exact': False, 'interval_seconds': 86400,
            'measured_at': NOW.isoformat(), 'high_water_seq': 999999}}}}
    original = deepcopy(saved)
    summary = retention_summary(saved, NOW)
    assert saved == original
    assert summary['headline'] == '정리 대상이 남아 있습니다'
    assert summary['backlog'] == '최소 12행' and summary['oldest_eligible_age'] == '9.5일 전 기록'
    row = summary['tables'][0]
    assert row['남은 정리 대상'] == '최소 12행' and row['신규 저장 관측'] == '최소 200행'
    assert row['신규 관측 기간'] == '1.0일' and 'high_water_seq' not in row


def test_clear_zero_is_distinct_from_unknown_zero_and_first_generation_baseline():
    cleared = retention_summary(report(backlog_status='CLEAR', eligible_backlog_min=0), NOW)
    unknown = report(backlog_status='UNKNOWN', eligible_backlog_min=0)
    unknown['tables'] = {'quote_snapshots': {'observation': {'eligible_rows_min': 0, 'backlog_exact': False,
                                                          'generation': {'rows_min': None, 'exact': False}}}}
    uncertain = retention_summary(unknown, NOW)
    assert cleared['backlog'] == '0행' and cleared['headline'] == '이번 관측에서 정리 대상 없음'
    assert cleared['oldest_eligible_age'] == '해당 없음'
    assert uncertain['backlog'] == '미확인'
    assert uncertain['tables'][0]['남은 정리 대상'] == uncertain['tables'][0]['신규 저장 관측'] == '미확인'
    assert uncertain['tables'][0]['가장 오래된 정리 대상'] == '미확인'


def test_repeated_limits_request_measurement_without_inventing_a_backlog_count():
    saved = report(backlog_status='UNKNOWN', eligible_backlog_min=None, stop_reason='ROW_LIMIT',
                   attention='CAPACITY_REVIEW', budget_limited_runs=3)
    saved['status'] = 'PARTIAL'
    summary = retention_summary(saved, NOW)
    assert summary['tone'] == 'warning' and summary['headline'] == '정리 처리량 점검 필요'
    assert '연속 3회' in summary['detail'] and summary['backlog'] == '미확인'
    assert summary['stop_reason'] == '검사 행 수 한도 도달'


@pytest.mark.parametrize('limited_runs', [0, 1])
def test_capacity_review_from_arrivals_does_not_claim_repeated_budget_limits(limited_runs):
    saved = report(backlog_status='PRESENT', eligible_backlog_min=10, attention='CAPACITY_REVIEW',
                   budget_limited_runs=limited_runs)
    summary = retention_summary(saved, NOW)
    assert summary['headline'] == '정리 처리량 점검 필요'
    assert '신규 저장 관측량이 이번 삭제량보다 많습니다' in summary['detail']
    assert '관측 기간·최소 수량' in summary['detail']
    assert '연속' not in summary['detail'] and '한도' not in summary['detail']


@pytest.mark.parametrize('bad', [True, -1, float('nan'), float('inf'), 'unknown'])
def test_invalid_numeric_observations_remain_unknown(bad):
    saved = report(backlog_status='PRESENT', eligible_backlog_min=bad, oldest_eligible_age_days=bad)
    saved['scanned'] = bad
    summary = retention_summary(saved, NOW)
    assert summary['backlog'] == summary['oldest_eligible_age'] == summary['scanned'] == '미확인'


def test_future_naive_and_missing_run_times_do_not_look_recent():
    for stamp in ((NOW + timedelta(seconds=1)).isoformat(), '2026-11-20T21:00:00', None):
        saved = report()
        saved['started_at'] = stamp
        assert retention_summary(saved, NOW)['age'] in {'미확인', '시각 확인 필요'}


@pytest.mark.parametrize('stage', ['GENERATION', 'GENERATION_KEYS', 'GENERATION_PAYLOAD'])
def test_growth_timeout_preserves_clear_backlog_and_names_optional_failure(stage):
    saved = report(backlog_status='CLEAR', eligible_backlog_min=0, stop_reason='PASS_COMPLETE')
    saved['tables'] = {'research_events': {'deleted': 2, 'observation': {
        'backlog_status': 'CLEAR', 'eligible_rows_min': 0, 'backlog_exact': True,
        'status': 'FAILED', 'error_details': {'stage': stage, 'code': 3024},
        'generation': {'rows_min': None, 'exact': False}}}}
    summary = retention_summary(saved, NOW)
    assert summary['pass_status'] == '검사 순회 완료' and summary['backlog'] == '0행'
    assert summary['observation_issues'] == 1
    row = summary['tables'][0]
    assert row['신규 저장 관측'] == '미확인' and row['남은 정리 대상'] == '0행'
    assert row['가장 오래된 정리 대상'] == '해당 없음'
    assert row['보조 통계 상태'] == '신규 저장 집계 시간 초과 · 정리 결과와 별도'


def test_render_handles_saved_report_without_database_reads(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('retention presentation must not read a database')
    monkeypatch.setattr('sessionquant.database.repository.Repository', unexpected)
    saved = report(backlog_status='UNKNOWN', eligible_backlog_min=None, stop_reason='TIME_LIMIT',
                   attention='CAPACITY_REVIEW', budget_limited_runs=2)
    saved['tables'] = {'quote_snapshots': {'deleted': 10, 'observation': {}}}
    script = ('import streamlit as st\nfrom sessionquant.dashboard.retention_panel import render_retention\n'
              f'render_retention(st, {saved!r})\n')
    app = AppTest.from_string(script).run()
    assert not app.exception and app.warning[0].value == '정리 처리량 점검 필요'
    assert app.dataframe[0].value.iloc[0]['남은 정리 대상'] == '미확인'
    assert any('미확인은 0이 아닙니다' in item.value for item in app.caption)

"""Saved retention observations; rendering never queries or changes the database."""
from datetime import datetime, timedelta, timezone
import math

from ..models import utc


KINDS = {
    'news_ingestions': '뉴스 수신 기록', 'news_enrichments': '뉴스 보강 기록',
    'macro_context_snapshots': '거시 맥락 기록', 'market_regimes': '시장 국면',
    'price_samples': '가격 관측', 'quote_snapshots': '호가 관측', 'system_logs': '시스템 기록',
    'macro_context_analysis': '거시 사전 점검', 'pipeline_status': '종목 선정 기록',
    'news_events': '기업 뉴스', 'news_analysis': '기업 뉴스 분석',
    'macro_news': '거시 뉴스', 'macro_analysis': '거시 뉴스 분석',
    'research_events': '매매 재현·연구 기록',
}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def _rows(value, *, exact=True):
    count = _count(value)
    if count is None or (count == 0 and not exact):
        return '미확인'
    return ('' if exact else '최소 ') + f'{count:,}행'


def _duration(seconds):
    if seconds >= 86400:
        return f'{seconds / 86400:.1f}일'
    if seconds >= 3600:
        return f'{seconds / 3600:.1f}시간'
    if seconds >= 60:
        return f'{int(seconds / 60)}분'
    return f'{int(seconds)}초'


def _timestamp(value):
    try:
        return utc(value).astimezone(timezone(timedelta(hours=9))).strftime('%m.%d %H:%M:%S KST')
    except (ValueError, TypeError, AttributeError, OverflowError):
        return '미확인'


def _age(value, now):
    try:
        seconds = (utc(now) - utc(value)).total_seconds()
        return _duration(seconds) + ' 전' if seconds >= 0 else '시각 확인 필요'
    except (ValueError, TypeError, AttributeError, OverflowError):
        return '미확인'


def _days(value):
    number = _number(value)
    return f'{number:.1f}일 전 기록' if number is not None else '미확인'


def observation_status(observation):
    stage = _mapping(observation.get('error_details')).get('stage', '')
    if observation.get('status') == 'FAILED':
        target = ('신규 저장 집계' if stage in {'GENERATION', 'GENERATION_KEYS', 'GENERATION_PAYLOAD'}
                  else '남은 대상 집계' if stage in {'BACKLOG', 'BACKLOG_VALIDATION'} else '보조 통계')
        timed_out = _mapping(observation.get('error_details')).get('code') == 3024
        return target + (' 시간 초과' if timed_out else ' 확인 실패') + ' · 정리 결과와 별도'
    if observation.get('status') in {'BUDGET_EXHAUSTED', 'SKIPPED_BUDGET'}:
        return '검사 한도로 통계 일부 미확인'
    if observation.get('status') == 'COMPLETE':
        return '작게 나눠 재조회 완료' if observation.get('generation_read_splits') else '확인 완료'
    return '기록 없음'


def retention_summary(report, now=None):
    """Keep pass completion, measured backlog and missing observations distinct."""
    report = _mapping(report)
    now = now or datetime.now(timezone.utc)
    observed = _mapping(report.get('observability'))
    policy = _mapping(report.get('policy'))
    backlog = observed.get('backlog_status')
    attention = observed.get('attention')
    status = report.get('status')
    headline, tone, detail = '남은 정리 대상 미확인', 'info', '관측 결과가 없어 남은 자료 수를 판단할 수 없습니다.'
    if not report:
        headline, detail = '정리 기록 없음', '아직 저장된 정리 결과가 없습니다.'
    elif status == 'DISABLED':
        headline, detail = '자동 정리 사용 안 함', '현재 설정에서는 오래된 자료 정리를 실행하지 않습니다.'
    elif attention == 'CAPACITY_REVIEW':
        headline, tone = '정리 처리량 점검 필요', 'warning'
        runs = _count(observed.get('budget_limited_runs'))
        if runs is not None and runs >= 2:
            detail = f'검사 한도에 연속 {runs}회 도달했습니다. 남은 자료와 생성·삭제 관측량을 확인하세요.'
        else:
            detail = '오래된 정리 대상이 남아 있고 신규 저장 관측량이 이번 삭제량보다 많습니다. 관측 기간·최소 수량을 함께 확인하세요.'
    elif backlog == 'PRESENT':
        headline, tone = '정리 대상이 남아 있습니다', 'warning'
        detail = '이번에 확인한 남은 자료입니다. 다음 정리 결과에서 남은 수와 대상의 나이를 함께 확인하세요.'
    elif backlog == 'CLEAR':
        headline, detail = '이번 관측에서 정리 대상 없음', '보관 대상과 매매 근거는 계속 보존합니다.'
    summary = {
        'available': bool(report), 'headline': headline, 'tone': tone, 'detail': detail,
        'pass_status': {'COMPLETE': '검사 순회 완료', 'PARTIAL': '검사 일부 진행',
                        'DISABLED': '사용 안 함'}.get(status, '실행 결과 미확인'),
        'last_started_at': _timestamp(report.get('started_at')), 'age': _age(report.get('started_at'), now),
        'scanned': _rows(report.get('scanned')), 'deleted': _rows(report.get('deleted')),
        'limit': _rows(policy.get('max_rows')),
        'intraday_auxiliary': policy.get('intraday_auxiliary') is True,
        'backlog': _rows(observed.get('eligible_backlog_min'), exact=backlog == 'CLEAR'),
        'oldest_eligible_age': '해당 없음' if backlog == 'CLEAR' else _days(observed.get('oldest_eligible_age_days')),
        'stop_reason': {'ROW_LIMIT': '검사 행 수 한도 도달', 'TIME_LIMIT': '검사 시간 한도 도달',
                        'QUERY_TIMEOUT': 'DB 조회 시간 초과 · 정리 미완료', 'PASS_COMPLETE': '이번 검사 순회 종료'}.get(observed.get('stop_reason'), '종료 사유 미기록'),
        'tables': [], 'observation_issues': 0,
    }
    for kind, raw in _mapping(report.get('tables')).items():
        table = _mapping(raw)
        observation = _mapping(table.get('observation'))
        generation = _mapping(observation.get('generation'))
        if observation.get('status') in {'FAILED', 'BUDGET_EXHAUSTED', 'SKIPPED_BUDGET'}:
            summary['observation_issues'] += 1
        interval = _number(generation.get('interval_seconds'))
        summary['tables'].append({
            '자료': KINDS.get(kind, '기타 보관 자료'),
            '남은 정리 대상': _rows(observation.get('eligible_rows_min'), exact=observation.get('backlog_exact') is True),
            '가장 오래된 정리 대상': '해당 없음' if observation.get('backlog_status') == 'CLEAR' else _days(observation.get('oldest_eligible_age_days')),
            '이번 삭제': _rows(table.get('deleted')),
            '신규 저장 관측': _rows(generation.get('rows_min'), exact=generation.get('exact') is True),
            '신규 관측 기간': _duration(interval) if interval else '기준 미확인',
            '신규 관측 시각': _timestamp(generation.get('measured_at')),
            '보조 통계 상태': observation_status(observation),
        })
    return summary


def render_retention(st, report):
    summary = retention_summary(report)
    st.subheader('자료 보관·정리')
    getattr(st, summary['tone'])(summary['headline'])
    st.caption(summary['detail'])
    if not summary['available']:
        return
    if summary['intraday_auxiliary']:
        st.caption('당일 자료 보관: 장전·장중에는 지난 날짜 자료를, 폐장 후에는 종료된 당일 자료도 새 백업 확인 뒤 정리합니다. '
                   '체결·손익·재시작 장부와 전략용 과거 분봉은 보존합니다. DB 파일 크기가 즉시 줄어드는 것은 아닙니다.')
    st.caption(f"마지막 정리 시작 {summary['last_started_at']} · {summary['age']}")
    st.write(f"{summary['pass_status']} · 검사 {summary['scanned']} · 삭제 {summary['deleted']}")
    st.caption(f"남은 정리 대상 {summary['backlog']} · 가장 오래된 정리 대상 {summary['oldest_eligible_age']}")
    st.caption(f"{summary['stop_reason']} · 한 번의 검사 한도 {summary['limit']}")
    if summary['observation_issues']:
        st.caption(f"보조 통계 {summary['observation_issues']}개 자료에서 일부 미확인 · 자료 정리 결과와 구분합니다. 아래 상태를 확인하세요.")
    if summary['tables']:
        st.dataframe(summary['tables'], hide_index=True, width='stretch')
    st.caption('순회 완료는 남은 정리 대상이 없다는 뜻이 아닙니다. 최소 수량은 확인한 하한이며, 미확인은 0이 아닙니다. '
               '신규 저장은 관측 기간의 행 수이며 초당 생성률이나 전체 저장량을 뜻하지 않습니다.')

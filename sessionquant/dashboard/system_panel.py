"""종류마다 다른 처리 기록을 읽기 쉬운 공통 표로 표시한다."""
from .labels import LABELS
from .session_app import timestamp


TASKS = {
    'watchlist_focus': '집중 분석 종목 선정',
    'watchlist_history_selection': '과거 분봉 준비 검사',
    'portfolio_selection': '매수 후보 비교',
    'risk_decision': '매수 위험 검사',
    'macro_reference_risk': '금리·유가 등 시장 위험 확인',
    'reference_entry_block': '시장 위험으로 매수 보류',
}


def _description(value, missing='기록 없음'):
    if value is None or value == '':
        return missing
    if isinstance(value,str) and value in LABELS:
        return LABELS[value]
    if isinstance(value, str) and any('가' <= c <= '힣' for c in value):
        return value
    return '원본 기록에서 확인'


def pipeline_rows(records):
    rows = []
    for record in records:
        kind = record.get('kind')
        result = _description(record.get('status'))
        detail = _description(record.get('reason') or record.get('code'), '—')
        if kind == 'watchlist_focus':
            result = _description(record.get('data_status'))
            count = record.get('items_count')
            if type(count) is not int and isinstance(record.get('items'),list):
                count = len(record['items'])
            if type(count) is int and count >= 0:
                detail = f'집중 분석 대상 {count}종목'
        elif kind == 'watchlist_history_selection':
            result = '준비 중' if record.get('preparing') is True else '검사 기록'
            detail = _counts(record, [('included_count', '자료 준비'), ('excluded_count', '자료 부족으로 제외')])
        elif kind == 'portfolio_selection':
            result = '비교 기록'
            detail = _counts(record, [('evaluated_count', '평가'), ('final_candidate_count', '최종 후보'),
                                      ('approved_count', '매수 승인')])
        elif kind == 'risk_decision':
            result = {True: '매수 승인', False: '매수 보류'}.get(record.get('approved'), '승인 여부 미기록')
        elif kind == 'macro_reference_risk':
            result = {'NORMAL': '특이 위험 없음', 'WATCH': '주의', 'ELEVATED': '위험 높음',
                      'UNKNOWN': '판단 자료 부족'}.get(record.get('status'), result)
            if record.get('effective_enabled') is False:
                detail = '참고용 · 매매에 적용 안 함'
            elif record.get('block_entries') is True:
                detail = '신규 매수 제한 중'
            elif record.get('block_entries') is False:
                detail = '이 검사에 따른 신규 매수 제한 없음'
        elif kind == 'reference_entry_block':
            result = '매수 보류'
        at = next((record.get(key) for key in
                   ('observed_at', 'timestamp', 'evaluated_at', 'known_at', 'checked_at')
                   if record.get(key)), None)
        rows.append({'작업': TASKS.get(kind, '기타 처리 기록'), '결과': result,
                     '기록 시각': timestamp(at), '종목': record.get('symbol')
                     if record.get('symbol') != 'MARKET_REFERENCE' else '시장 전체',
                     '내용': detail})
        if not rows[-1]['종목']:
            rows[-1]['종목'] = '—'
    return rows


def _counts(record, fields):
    return ' · '.join(f'{name} {record[key]}종목' for key, name in fields
                      if record.get(key) is not None) or '집계 미기록'

"""뉴스 화면은 저장된 수집·검증 사실만 읽는다. 네트워크와 분석을 시작하지 않는다."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from ..models import utc
from ..news.intake import company_reason, statistics


def current_company_news(rows, data, view, now=None):
    now = utc(now or datetime.now(timezone.utc))
    symbols = {row['종목'] for row in view['positions']}
    for row in (data.actual_account or {}).get('items', []):
        try:
            if Decimal(str(row.get('quantity', '0'))) > 0:
                symbols.add(row['symbol'])
        except (InvalidOperation, KeyError, TypeError):
            pass
    from .watchlist_panel import current_status
    status, active = current_status(data.watchlist_state, now)
    if active and status == 'VALID':
        for row in data.watchlist_state.get('items', []):
            try:
                if row.get('focused') and utc(row['focus_valid_until']) >= now:
                    symbols.add(row['ticker'])
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
    latest = {}
    for row in rows:
        event = row.get('event', row)
        key = (event.get('id'), row.get('ticker', ''))
        latest[key] = row
    result = []
    for row in latest.values():
        event = row.get('event', row)
        try:
            if row.get('error') or utc(row.get('analyzed_at') or event['received_at']) > now:
                continue
            value = SimpleNamespace(**{key: event[key] for key in
                ('id', 'symbols', 'title', 'verified', 'source', 'url', 'body', 'published_at', 'received_at')})
            kind = row.get('source_kind') or ('sec' if value.id.startswith('sec:') else
                                            'rss' if value.id.startswith('rss:') else 'alpaca')
            primary = row.get('primary_verified', value.verified and kind in {'sec', 'rss'})
            tickers = symbols.intersection(value.symbols)
            if row.get('ticker'):
                tickers.intersection_update({row['ticker']})
            if any(not company_reason(value, ticker, kind, primary, now) for ticker in tickers):
                result.append(row)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return sorted(result, key=lambda r: utc(r.get('event', r)['published_at']), reverse=True)[:50]


def calendar_rows(rows, now=None):
    from .session_app import timestamp
    now = utc(now or datetime.now(timezone.utc))
    latest = {}
    for row in rows:
        try:
            if row.get('verified') is not True or utc(row['known_at']) > now:
                continue
            key = row.get('logical_id') or row['id']
            rank = (row.get('revision', 0), utc(row['known_at']))
            if key not in latest or rank > latest[key][0]:
                latest[key] = (rank, row)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return [{'일정': r['name'], '발표 시각': timestamp(r['scheduled_at']), '상태': r.get('status', 'CONFIRMED'),
             '확인 시각': timestamp(r['known_at']), '출처': r['source_url']}
            for _, r in latest.values() if utc(r['scheduled_at']).date() >= now.date()]


def provider_rows(status):
    from .session_app import timestamp
    return [{'공급자·작업': name, '상태': row.get('status', 'UNKNOWN'),
             '최근 시도': timestamp(row.get('last_attempt_at')), '최근 성공': timestamp(row.get('last_success_at')),
             '요청 수': row.get('calls', 0), '최근 기사 수': row.get('last_count'),
             '403': row.get('http_403', 0), '429': row.get('http_429', 0),
             '다음 재시도': timestamp(row.get('cooldown_until')), '오류': row.get('last_error')}
            for name, row in sorted(status.get('schedule', {}).items())]


def render_news_diagnostics(data):
    import streamlit as st
    from .session_app import _table, timestamp
    now = datetime.now(timezone.utc)
    report=data.news_flow
    if report:
        st.subheader('뉴스 처리 진행 상황')
        st.info(report['message'])
        counts=report['counts']
        columns=st.columns(4)
        for column,key,label in zip(columns,
                ('news_receipts','llm_request_attempts','validated_responses','failed_or_rejected'),
                ('기사 수신','LLM 요청 시도','응답 검증 통과','요청 실패·검증 거부')):
            column.metric(label,str(counts[key]))
        st.caption('최근 24시간 · 수신 최대 2,000건, 실제 AI 요청과 분석 처리 기록 각각 최대 500건. 중복 수신과 생성 요청은 다른 수치입니다.')
        st.caption('최근 기사 수신 '+timestamp(report['last_news_received_at'])+' · 최근 LLM 요청 '+timestamp(report['last_llm_request_at'])+
            f" · 캐시 재사용 {counts['cache_hits']}건 · 처리 중/결과 미확인 {counts['pending_or_unconfirmed']}건")
        if report['scope']['may_be_truncated']:st.caption('조회 상한에 도달했습니다. 위 수치는 전체 누계가 아닙니다.')
        st.caption('응답 검증 통과는 형식·근거 검사를 통과했다는 의미입니다. 분류의 정답률이나 매매 승인을 뜻하지 않습니다.')
        _table([{'시각':timestamp(r['requested_at']),'종목·주제':r['ticker_or_topic'],'처리 결과':r['status'],
            '분류':r['direction'],'사건 유형':r['event_types'],'추가 확인 필요':r['requires_verification']}
            for r in report['latest_calls']],'조회 범위에 LLM 생성 요청이 없습니다. 아래 공급자/제외 사유를 확인하세요.')
        if report['latest_calls']:
            with st.expander('분류 이유와 원문 근거'):
                _table([{'종목·주제':r['ticker_or_topic'],'회사 영향':r['company_impacts'],
                    '주주 영향':r['shareholder_impacts'],'내용 확인':r['event_statuses'],
                    '분류 이유':r['reason'],'원문 근거':' / '.join(str(x) for x in r['evidence']),
                    '오류':r['error'],'검사 실패 항목':r.get('failed_field') or '—'} for r in report['latest_calls']])
    with st.expander('뉴스 공급자·신선도·원문 검증 상세'):
        st.caption('저장된 최근 최대 2,000건 기준입니다. 수집·분석·새 기사가 없음·장애를 구분합니다.')
        st.caption('마지막 뉴스 작업: '+timestamp(data.news_status.get('updated_at')))
        providers=provider_rows(data.news_status)
        current_states={r['provider']:r['state'] for r in report.get('providers',[])}
        for row in providers:
            row['상태']=current_states.get(row['공급자·작업'],row['상태'])
        _table(providers, '공급자 요청 기록이 없습니다.')
        counts = statistics(data.tables['news_ingestions'], now)
        _table([{'나이 구간': name, '수신 건수': count} for name, count in counts['age_buckets'].items()])
        _table([{'유료 분석 전 제외 사유': name, '수신 건수': count}
                for name, count in counts['pre_llm_exclusions'].items()])
        st.caption(f"중복 수신 {counts['duplicates']}건 · 최초 수신 시 분석 후보 {counts['llm_candidates']}건")
        _table([{'발행사': r.get('publisher_name') or r.get('publisher_domain'), '상태': r.get('status'),
                 '게시 시각': timestamp(r.get('published_at')), '확인 시각': timestamp(r.get('checked_at')),
                 '본문 글자 수': r.get('body_char_count'), '시도 수': r.get('attempt_count'),
                 '분석 적격': r.get('eligible_for_llm', False), '차단 사유': r.get('block_reason'),
                 '원문': r.get('source_url')} for r in data.tables['news_enrichments']])
        _table(data.news_status.get('issues', []), '저장된 현재 오류가 없습니다. 연결 정상 검증을 의미하지 않습니다.')

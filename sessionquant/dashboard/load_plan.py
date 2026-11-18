"""화면에서 실제로 사용하는 기록만 읽는다. 원본 분봉은 화면 조회 대상이 아니다."""

CONTROLS = {
    'context': ('sessionquant_context', '실행 정보'),
    'engine_state': ('trade_control_state', '엔진 상태'),
    'actual_account': ('actual_account_snapshot', '실제 계좌'),
    'account_status': ('account_monitor_status', '계좌 조회 상태'),
    'watchlist_state': ('watchlist_dashboard_v1', '감시 목록'),
    'market_quality': ('market_data_quality_v1', '시세 품질'),
    'market_performance': ('market_performance_v1', '처리 시간'),
    'watchlist_rvol': ('watchlist_rvol_v1', '거래량 비교'),
    'history_backfill': ('watchlist_history_backfill_v1', '분봉 준비'),
    'portfolio_selection': ('portfolio_selection_v1', '매수 후보 선정'),
    'market_reference': ('market_reference_view_v1', '시장 참고 지표'),
    'news_status': ('session_news_status', '뉴스 수집 상태'),
    'warmup': ('startup_data_warmup_v1', '자료 준비'),
}

PAGE_CONTROLS = {
    '대시보드': ('actual_account', 'account_status', 'watchlist_state', 'warmup', 'news_status'),
    '시장·종목': ('watchlist_state', 'market_quality', 'market_performance', 'watchlist_rvol',
                 'history_backfill', 'portfolio_selection', 'market_reference'),
    '전략·백테스트': (),
    '매매 내역': ('actual_account', 'account_status'),
    '뉴스·LLM': ('actual_account', 'account_status', 'watchlist_state', 'news_status'),
    '시스템·설정': ('warmup', 'market_reference', 'news_status'),
}

# 최신 상태 하나와 이력 표를 구분한다. 체결 내보내기·뉴스 집계의 기존 범위는 유지한다.
PAGE_TABLES = {
    '대시보드': dict.fromkeys(('executions', 'news_events', 'news_analysis', 'quote_snapshots', 'news_ingestions'), 300),
    '시장·종목': {'market_regimes': 1, 'quote_snapshots': 300, 'candidates': 300, 'economic_events': 200},
    '전략·백테스트': {'account_snapshots': 2000, 'daily_performance': 2000},
    '매매 내역': {'session_state': 1, 'positions': 2000, 'account_snapshots': 1, 'quote_snapshots': 300,
                 'orders': 2000, 'executions': 2000, 'alerts': 300},
    '뉴스·LLM': dict.fromkeys(('news_events', 'news_analysis', 'macro_news', 'macro_analysis',
                             'news_ingestions', 'news_enrichments'), 2000),
    '시스템·설정': {'alerts': 300, 'pipeline_status': 100},
}

SOURCE_LABELS = {
    **{name: label for name, (_, label) in CONTROLS.items()},
    'connection': 'DB 연결', 'state': '전략 장부', 'broker_status': '거래 모드',
    'market_regimes': '시장 방향', 'quote_snapshots': '최근 호가', 'candidates': '기술 분석 이력',
    'economic_events': '경제 일정', 'fred_references': '일별 참고 지표',
    'session_state': '청산 상태', 'positions': '보유 종목', 'account_snapshots': '계좌 이력',
    'orders': '주문', 'executions': '체결', 'alerts': '운영 경고', 'daily_performance': '전략 성과',
    'pipeline_status': '처리 이력', 'news_events': '기업 뉴스', 'news_analysis': '기업 뉴스 분석',
    'macro_news': '시장 뉴스', 'macro_analysis': '시장 뉴스 분석', 'news_ingestions': '뉴스 수신',
    'news_enrichments': '뉴스 원문 확인', 'news_schedule': '뉴스 수집 일정',
    'news_flow': '뉴스·AI 처리 요약', 'transaction': 'DB 조회 마무리',
}


def read_plan(page):
    if page is None:
        tables = {}
        for plan in PAGE_TABLES.values():
            for name, limit in plan.items():
                tables[name] = max(limit, tables.get(name, 0))
        return tuple(CONTROLS), tables
    if page not in PAGE_TABLES:
        raise ValueError('지원하지 않는 대시보드 화면입니다.')
    controls = dict.fromkeys(('context', 'engine_state', 'actual_account', 'account_status', *PAGE_CONTROLS[page]))
    return tuple(controls), PAGE_TABLES[page]

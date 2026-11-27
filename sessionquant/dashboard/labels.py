"""화면에서만 쓰는 쉬운 이름. 저장된 코드와 분석 원문은 변경하지 않는다."""
import re

LABELS = {
    'toss:market_reference:v1.2.28': '토스증권',
    'strategy_outside_regular_session':'정규장 시작 대기·종료',
    'strategy_market_direction_blocked':'전략에 맞는 시장 방향 대기',
    'strategy_bar_identity_or_time_invalid':'분봉 종목·확정 시각 불일치',
    'strategy_indicator_cutoff_mismatch':'지표 계산 기준 시각 불일치',
    'strategy_indicators_unavailable':'VWAP·이동평균·변동폭·거래량 지표 부족',
    'strategy_five_minute_history_incomplete':'연속된 5분봉 15개 확보 대기',
    'strategy_closed_bar_stale':'확정 5분봉 갱신 대기',
    'strategy_price_extended_or_invalid':'가격 과열·유효 가격 확인 필요',
    'strategy_recent_volume_below_minimum':'최근 5분 거래량 부족',
    'strategy_fifteen_minute_uptrend_missing':'15분봉 상승 추세 대기',
    'strategy_price_above_support_missing':'VWAP·이동평균 위로 상승 대기',
    'strategy_recent_high_breakout_missing':'최근 고점 돌파 대기',
    'strategy_breakout_volume_missing':'돌파 거래량 증가 대기',
    'pullback_simple':'단순 눌림목',
    'simple_trend_missing':'15분 상승 추세 대기',
    'simple_correction_missing':'1~4개 조정봉·조정 폭 조건 대기',
    'simple_rebound_missing':'직전 고가 위 확정봉 반등 대기',
    'simple_pattern_unavailable':'당일 전략 자료 준비 중',
    'strategy_first_pullback_missing':'첫 번째 조정봉 대기',
    'strategy_second_pullback_missing':'두 번째 조정봉 대기',
    'strategy_pullback_support_missing':'VWAP·이동평균 부근 지지 대기',
    'strategy_pullback_depth_invalid':'조정 폭 조건 미충족',
    'strategy_rebound_high_breakout_missing':'직전 봉 고점 돌파 대기',
    'strategy_rebound_above_vwap_missing':'반등 가격의 VWAP 회복 대기',
    'strategy_rebound_volume_missing':'반등 거래량 증가 대기',
    'strategy_opening_range_incomplete':'개장 구간 분봉 부족',
    'strategy_opening_range_not_finished':'개장 구간 확정 대기',
    'strategy_opening_range_breakout_missing':'개장 구간 고점 돌파 대기',
    'strategy_price_above_vwap_missing':'VWAP 위로 상승 대기',
    'strategy_previous_vwap_unavailable':'직전 봉 기준 VWAP 부족',
    'strategy_vwap_reclaim_missing':'VWAP 재돌파 대기',
    'strategy_price_above_ema_missing':'이동평균 위로 상승 대기',
    'strategy_stop_price_invalid':'유효한 손절 가격 계산 불가',
    'rvol_observation_stale_or_incomplete':'현재 분까지 계산 결과 미갱신·자료 부족',
    'current_closed_interval_missing':'정규장 확정 분봉 대기',
    'symbol_not_yet_collected':'아직 분봉 미수집',
    'rvol_calendar_unknown':'비교 거래일 확인 필요',
    'volume_source_or_adjustment_mismatch':'거래량 출처·가격 조정 방식 불일치',
    'invalid_or_zero_history_volume':'과거 거래량 평균이 없거나 0',
    'WARMING':'자료 준비 중','COLLECTING':'자료 수집 중','WAITING_WATCHLIST':'감시 목록 확인 중',
    'RETRY_WAIT':'다음 조회 대기','cursor_stalled':'이전 분봉 조회 중단',
    'startup_data_not_ready':'시작 자료 준비 중',
    'PAPER_LOCAL':'모의매매','LIVE_LOCAL':'실제 계좌','LIVE_GCP':'실제 계좌',
    'UNKNOWN':'확인 필요','NOT_STARTED':'아직 시작 안 함','WAITING':'시작 버튼 대기',
    'RUNNING':'실행 중','STOPPED':'종료','LIQUIDATING':'매도 진행 중','OFFLINE':'프로그램 종료',
    'VALID':'사용 가능','VERIFIED':'확인됨','AVAILABLE':'사용 가능','PARTIAL':'일부 확인 필요',
    'COMPLETE':'준비 완료','INCOMPLETE':'자료 부족','STALE':'오래된 자료','EXPIRED':'유효기간 지남',
    'BLOCKED':'보류','READY':'준비 완료','WATCH':'관찰 중','CALCULATED':'계산됨','CLOSED':'장 종료',
    'SIMULATION_ASSUMPTIONS':'모의 거래 조건','EXPERIMENTAL_UNVERIFIED':'비교 근거 미확인',
    'strict':'엄격한 확인','observed_paper_v1':'모의 거래 조건','market_v2':'시장가 모의 체결',
    'SUCCEEDED':'분석 완료','REQUESTED':'응답 대기','RESPONSE_RECEIVED':'응답 검사 중',
    'OUTCOME_UNKNOWN':'요청 결과 확인 필요','CACHE_HIT':'이전 분석 재사용',
    'SKIPPED_PRECHECK':'분석 대상 제외','BUDGET_BLOCKED':'예산·호출 제한',
    'HTTP_FAILED':'연결 요청 실패','RATE_LIMITED':'요청 제한','JSON_PARSE_FAILED':'응답 형식 오류',
    'SCHEMA_VALIDATION_FAILED':'응답 항목 오류','BUSINESS_RULE_REJECTED':'내용 검사 미통과',
    'PROVIDER_REFUSED':'AI 응답 거절','PROVIDER_REFUSAL':'AI 응답 거절','SEMANTIC_CONFLICT':'내용 해석 충돌',
    'LEGACY_UNMAPPED':'이전 기록 · 요청 연결 미확인',
    'EVIDENCE_QUOTE_MISMATCH':'원문 근거 불일치','SCHEMA_MISMATCH':'응답 항목 불일치',
    'EVIDENCE_MISMATCH':'원문 인용 검사 미통과',
    'events.evidence':'응답 인용문 형식·원문 일치','evidence':'본문 문단 내 인용문 일치',
    'evidence.status':'인용문의 확정·예정 구분',
    'JSON_PARSE_ERROR':'응답 형식 오류','TIMEOUT':'응답 시간 초과','TRANSPORT_ERROR':'통신 오류',
    'NO_KEY':'AI 키 없음','NO_CREDIT':'AI 잔액·한도 부족','AUTH_ERROR':'AI 인증 확인 필요',
    'TEMPORARY_ERROR':'일시적 연결 오류','RATE_LIMIT':'요청 제한',
    'FETCHED':'수집 완료','NO_NEW_ARTICLES':'새 기사 없음','IN_PROGRESS':'작업 중',
    'PROVIDER_RATE_LIMITED':'공급자 요청 제한','PROVIDER_UNAVAILABLE':'공급자 연결 확인 필요',
    'PROVIDER_HTTP_ERROR':'공급자 요청 실패','DISCOVERED_ONLY':'기사 발견 · 본문 미확인',
    'POSITIVE':'긍정','NEGATIVE':'부정','NEUTRAL':'중립','MIXED':'긍정·부정 혼재',
    'CONFIRMED':'확인된 내용','UNCONFIRMED':'확인 필요','RUMOR':'소문','PLANNED':'예정',
    'HIGH':'높음','MEDIUM':'보통','LOW':'낮음','BULLISH':'상승','BEARISH':'하락',
    'EARNINGS_BEAT':'실적 호조','EARNINGS_MISS':'실적 부진','GUIDANCE_RAISE':'실적 전망 상향',
    'GUIDANCE_CUT':'실적 전망 하향','CLINICAL_SUCCESS':'임상 성공','CLINICAL_FAILURE':'임상 실패',
    'DILUTION':'주식 증가 부담','OFFERING':'주식 발행','BUYBACK':'자사주 매입','DIVIDEND':'배당',
    'MERGER_ACQUISITION':'인수·합병','REGULATORY_APPROVAL':'규제 승인','LAWSUIT':'소송',
    'BANKRUPTCY':'파산','OTHER':'기타','EMPLOYMENT':'고용','INFLATION':'물가','INTEREST_RATE':'금리',
    'pullback':'눌림목','momentum':'상승 추세','gap_breakout':'갭 돌파','vwap':'평균 거래가격 회복',
    'buy':'매수','sell':'매도','BUY':'매수','SELL':'매도','primary':'기본 분석','escalation':'추가 분석',
    'submitted':'접수 대기','accepted':'접수됨','filled':'체결 완료','partial':'일부 체결',
    'cancel_pending':'취소 대기','cancelled':'취소됨','rejected':'거절됨','pending':'처리 대기',
    'INFO':'안내','WARNING':'주의','ERROR':'오류','info':'안내','warning':'주의','error':'오류',
    'stale_article':'오래된 기사','duplicate_article':'중복 기사','article_body_missing':'본문 없음',
    'title_only_article':'제목만 있음','published_at_unknown':'발행 시각 미확인',
    'invalid_timestamp':'기사 시각 불일치','unverified_article_identity':'기사 출처·종목 미확인',
    'primary_source_unverified':'공식 출처 미확인','ticker_not_in_article_symbols':'대상 종목 불일치',
    'source_missing':'출처 없음','publisher_missing':'발행사 없음','llm_budget_limit':'AI 예산·호출 제한',
    'company_feed_mapping_unverified':'기업 RSS 연결 미확인','company_mapping_missing':'기업 뉴스 설정 없음',
    'company_refresh_failed':'기업 뉴스 수집 실패','macro_refresh_failed':'경제 뉴스 수집 실패',
    'history_not_in_daily_operating_watchlist':'과거 분봉 부족으로 오늘 제외',
    'history_session_missing':'거래일 자료 없음','history_minutes_missing_or_conflicting':'분봉 누락·값 충돌',
    'history_record_invalid':'저장된 분봉 오류','historical_same_minute_coverage_incomplete':'과거 비교 분봉 부족',
    'current_session_coverage_incomplete':'오늘 분봉 부족','split_volume_comparability_unknown':'분할 전후 거래량 비교 미확인',
    'volume_source_or_adjustment_mismatch':'거래량 출처·조정 방식 불일치',
    'relative_volume_unknown_or_low':'평소 대비 거래량 부족·미확인','minute_history_incomplete_or_stale':'분봉 부족·갱신 지연',
    'minute_gaps':'분봉 누락','bar_revisions_unresolved':'분봉 값 충돌','minute_data_stale_or_missing':'최근 분봉 부족',
    'quality_observation_stale':'최근 데이터 검사 필요','new_closed_minute_not_evaluated':'새 분봉 검사 대기',
    'quote_stale':'시세 갱신 지연','quote_missing':'시세 없음','eligibility_unverified':'거래 조건 미확인',
    'market_unknown':'시장 방향 미확인','market_bearish':'하락장','market_neutral':'방향성 부족',
    'outside_market_cap_watchlist':'시총 감시 대상 밖','focus_unknown_or_stale':'집중 분석 대기·만료',
    'focus_refresh_pending':'집중 유지 · 자료 갱신 전 매수 보류',
    'watchlist_unknown':'감시 목록 미확인','watchlist_partial':'감시 자료 부족','watchlist_expired':'감시 목록 만료',
    'confirmed_news_entry_restriction':'확인된 뉴스 위험','trading_not_started_or_halted':'매매 시작 대기·중지',
    'preferred_position_target_reached':'권장 보유 수 도달','final_candidate_limit':'최종 후보 수 제한',
    'stop_loss':'손절','session_exit':'장 마감 전 매도','daily_loss_limit':'하루 손실 한도 도달',
    'holding_time_exit':'보유 시간 종료','confirmed_adverse_news':'확인된 악재','profit_protection':'수익 보호',
    'net_profit_target':'목표 수익 도달','paper_fractional_trading_enabled':'모의 소수점 거래 사용',
    'actual_account_permissions_not_verified':'실제 계좌 권한 미확인','halted_unknown':'거래정지 여부 미확인',
    'corporate_action_unknown':'기업행동 미확인','TOKEN_BASED_ESTIMATE':'토큰 기준 추정 비용',
    'UNCONFIRMED_RESERVED_ESTIMATE':'사용량 미확인 · 예상 비용','NO_REQUEST':'요청 없음',
    'company_cycle':'기업 뉴스','calendar_cycle':'경제 일정','fred_daily':'일별 경제지표',
    'macro:official':'공식 경제 뉴스','macro:treasury':'미 재무부','macro:gdelt':'세계 뉴스 검색',
    'macro:alpaca':'시장 보조 뉴스','rss':'기업 RSS','sec':'미국 공시','official':'공식 기관',
    'gdelt':'세계 뉴스 검색','treasury':'미 재무부','alpaca':'알파카 뉴스',
    'toss:/api/v1/candles:1m':'토스 1분봉','raw':'수정 전 가격','adjusted':'수정 가격',
    'FDA_APPROVAL':'미 FDA 승인','FDA_REJECTION':'미 FDA 승인 거절','MAJOR_CONTRACT':'주요 계약',
    'STRATEGIC_PARTNERSHIP':'전략적 제휴','ACQUISITION_OFFER':'인수 제안','DEBT_REDUCTION':'부채 감소',
    'EQUITY_OFFERING':'주식 발행','ATM_OFFERING':'시장 내 주식 발행','CONVERTIBLE_DEBT':'전환사채',
    'DELISTING_NOTICE':'상장폐지 경고','GOING_CONCERN':'기업 존속 우려','DEBT_DEFAULT':'채무 불이행',
    'TRADING_SUSPENSION':'거래정지','REVERSE_SPLIT':'주식 병합','REGISTRATION':'증권 등록',
    'MANAGEMENT_CHANGE':'경영진 변경','RIGHTS_UNKNOWN':'이용 권한 미확인',
    'BODY_EXTRACTED':'본문 확보','ELIGIBLE_FOR_LLM':'AI 분석 가능','FETCH_PENDING':'수집 대기',
    'FETCH_UNAVAILABLE':'본문 수집 불가','CONTENT_TOO_SHORT':'본문 부족','BOILERPLATE_ONLY':'안내문만 있음',
    'DATE_CONFLICT':'발행 시각 불일치','PUBLISHED_AT_UNKNOWN':'발행 시각 미확인',
    'ROBOTS_DISALLOWED':'사이트 수집 제한','ROBOTS_RATE_LIMITED':'사이트 요청 제한',
    'ROBOTS_UNAVAILABLE':'수집 허용 여부 미확인','RETRY_EXHAUSTED':'재시도 한도 도달',
    'SOURCE_AND_TIME_VERIFIED':'출처·시각 확인됨','CANCELLED':'취소됨',
    'paper_listing_evidence_missing':'상장 정보 없음','paper_listing_evidence_stale_or_missing':'상장 정보 부족·만료',
    'paper_listing_not_active':'현재 거래 중인 상장 종목 아님','exchange_unverified':'거래소 미확인',
    'paper_known_restriction_tradable':'거래 제한 확인','paper_known_restriction_fractional':'소수점 거래 제한 확인',
    'paper_known_restriction_halted':'거래정지 확인','paper_known_restriction_corporate_action':'기업행동 제한 확인',
    'GOOD':'양호','DELAYED':'지연 시세','REALTIME':'실시간','DAILY':'일별 자료',
    'WITHIN_ESTIMATES':'예상 한도 이내','NO_OBSERVATION':'관측 자료 없음','SHADOW':'참고만 표시',
    'toss_prices':'토스 현재가 일괄 조회','toss_rankings':'토스 거래대금 순위',
    'toss_orderbook':'토스 호가','toss_candles':'토스 분봉','toss_stocks':'토스 종목 정보',
    'deadline_skipped':'시간 부족으로 조회 대기',
    'quote_time_reserved':'호가 조회 시간 확보로 다음 회차 대기','budget_exhausted':'주기 요청 한도 도달',
    'provider_backoff':'공급자 재조회 대기','provider_group_backoff':'해당 자료 재조회 대기',
    'symbol_prices_missing':'종목 현재가 미수신','symbol_second_price_missing':'종목 비교 가격 1회 부족',
    'spy_prices_missing':'시장(SPY) 현재가 미수신','spy_second_price_missing':'시장(SPY) 비교 가격 1회 부족',
    'turnover_missing':'거래대금 미확인','observation_stale_or_future':'관찰 자료 지연·미래 시각',
    'observation_window_mismatch':'종목·시장 관측 구간 불일치',
    # Scanner/selection codes are persisted before the strategy can run.
    'universe_unknown_stale_or_nonmember':'종목 목록 미확인·만료 또는 대상 밖',
    'instrument_symbol_mismatch':'종목 정보의 티커 불일치',
    'instrument_eligibility_unknown':'종목 거래 조건 미수신',
    'instrument_eligibility_stale_or_future':'종목 거래 조건의 확인 시각 오류·만료',
    'instrument_eligibility_expired':'종목 거래 조건 유효기간 지남',
    'instrument_tradable_unverified_or_restricted':'거래 가능 여부 미확인·제한',
    'instrument_fractional_unverified_or_restricted':'소수점 거래 여부 미확인·제한',
    'instrument_halted_unverified_or_restricted':'거래정지 여부 미확인·제한',
    'instrument_corporate_action_unverified_or_restricted':'기업행동 여부 미확인·제한',
    'quote_time_or_symbol_invalid':'호가의 종목·시각 불일치',
    'quote_timestamp_conflict':'같은 시각의 호가 값 충돌 · 새 호가 대기',
    'runtime_clock_rollback':'실행 시각이 뒤로 바뀌어 체결 대기',
    'runtime_clock_unavailable':'주문 직전 시각을 확인하지 못해 체결 대기',
    'runtime_instrument_eligibility_restricted':'거래 자격 만료 또는 제한으로 모의 매수 보류',
    'runtime_order_no_longer_active':'주문 유효기간 또는 취소 확정 시각이 지나 모의 체결 보류',
    'runtime_quote_stale_or_invalid':'주문 직전 호가가 오래되거나 유효하지 않아 체결 대기',
    'runtime_entry_window_closed':'주문 직전에 매수 가능 시간이 끝남',
    'runtime_exit_window_closed':'체결 직전에 매도 가능 시간이 끝남',
    'runtime_sell_quantity_unavailable':'매도 잔여 수량 확인 필요',
    'runtime_execution_check_missing':'재생에 필요한 주문 직전 확인 기록 없음',
    'runtime_execution_check_mismatch':'재생 주문과 주문 직전 확인 기록이 일치하지 않음',
    'runtime_execution_check_invalid':'주문 직전 확인 기록을 검증할 수 없음',
    'runtime_execution_check_limit':'한 번에 확인할 주문 수를 넘어 체결 대기',
    'quote_halted':'거래정지 호가',
    'quote_depth_missing':'매수·매도 호가 수량 부족',
    'spread_too_wide':'매수·매도 가격 차이가 큼',
    'closed_bars_missing_or_stale':'확정 5분봉 미수신·갱신 지연',
    'technical_history_incomplete':'매매 지표를 계산할 당일 분봉 부족',
    'insufficient_liquidity':'당일 거래량·거래대금 부족',
    'candidate_bar_time_mismatch':'최신 확정 5분봉 미반영',
    'candidate_quote_stale_or_unknown':'주문용 호가 미수신·갱신 지연',
    'candidate_quote_time_mismatch':'비교 후보 간 호가 시각 차이',
    'technical_signal_absent':'전략 매수 신호 없음',
    'signal_already_consumed':'이미 주문한 전략 신호',
    'duplicate_symbol_signal':'같은 종목의 중복 신호',
    'market_reference_not_trading_candidate':'시장 참고용 ETF · 매수 대상 아님',
    'macro_reference_risk_restriction':'시장 참고 지표의 위험 제한',
    'focus_time_expired':'집중 분석 자료의 유효기간 지남',
    'focus_time_unverified':'집중 분석 자료의 시각 미확인',
    'market_provider_backoff':'시세 공급자 재조회 대기',
    'websocket_quote_unavailable':'실시간 호가 확인 전 매수 대기',
    'price_or_turnover_missing':'현재가·거래대금 미수신',
    'watchlist_history_unknown':'감시 목록의 과거 기록 미확인',
    'watchlist_history_expired_or_partial':'감시 목록의 과거 기록 만료·부족',
    'watchlist_history_synthetic_blocked':'합성 감시 목록은 실제 자료로 사용 불가',
    'watchlist_history_membership_unknown':'당시 감시 대상 여부 미확인',
    'watchlist_synthetic_blocked':'합성 감시 목록은 실제 자료로 사용 불가',
    'focus_history_unknown_or_stale':'당시 집중 분석 자료 미확인·만료',
    'outside_historical_focus':'당시 집중 분석 대상 밖',
    'quote_unknown_or_stale':'호가 미수신·갱신 지연',
    'closed_bars_unknown_or_stale':'확정 분봉 미수신·갱신 지연',
    'outside_regular_session':'정규장 시간 아님',
    'outside_entry_window':'매수 가능 시간 아님',
    'market_not_bullish':'상승장 조건 미충족',
    'confirmed_company_risk':'확인된 기업 위험',
    'trend_unconfirmed':'상승 추세 미확인',
    'invalid_evidence':'주문 판단 자료 오류',
    'invalid_stop':'손절 가격 조건 미충족',
    'account_order_state_unknown':'주문 처리 상태 미확인',
    'pending_order_or_exit':'기존 주문·청산 처리 중',
    'add_conditions_not_met':'추가매수 조건 미충족',
    'add_would_average_down':'하락 중 추가매수 제한',
    'invalid_add_stop':'추가매수 손절 가격 조건 미충족',
    'position_exists':'이미 보유한 종목',
    'existing_risk_limit_breached':'기존 보유 위험 한도 초과',
    'position_limit':'최대 보유 종목 수 도달',
    'pending_mark_reference_unknown':'예약 주문의 평가 가격 미확인',
    'allocation_or_cash_limit':'투자 비중·가용 현금 부족',
    'risk_budget_insufficient':'허용 손실 범위 내 주문 금액 부족',
    'exit_stop_unverified':'손절 가격 미확인',
    'sector_diversification_unknown':'업종 분산 자료 미확인',
    'same_sector_limit':'같은 업종 보유 한도 도달',
    'index_correlation_unknown':'시장 지수와의 상관관계 미확인',
    'high_index_correlation_limit':'비슷하게 움직이는 종목의 보유 한도 도달',
    'RANKED':'후보 비교 중','FINAL_CANDIDATE':'최종 후보','APPROVED':'주문 승인',
    'ALLOCATION_REJECTED':'수량·위험 조건으로 보류','approved':'주문 승인',
    'SIGNAL':'전략 신호 있음','UNAVAILABLE':'판단 자료 부족','NO_SIGNAL':'전략 신호 없음',
    'DATA_WAIT':'확정봉 자료 대기','TREND_MISSING':'상승 조건 대기',
    'TREND':'상승 확인 · 조정 대기','CORRECTION':'조정 중 · 반등 대기',
    'REBOUND':'반등 확인','TOO_LONG':'조정 기간 초과','TOO_DEEP':'조정 하락 폭 초과',
    'PREOPEN':'개장 전','AFTER_HOURS':'장 종료 후','STOPPING':'종료 중',
    'MISSING':'미수신','NO_DATA':'수신 자료 없음',
    'IDLE':'구독 대상 대기','CHANGING':'실시간 시세 구독 변경 중',
    'CONNECTING':'실시간 시세 연결 중','SUBSCRIBED':'실시간 시세 구독 확인',
    'DISCONNECTED':'실시간 시세 연결 끊김','BACKOFF':'실시간 시세 재연결 대기',
    'DISABLED':'사용 안 함','NOT_REQUIRED':'필수 아님','IGNORED':'반영 안 함',
    'NONE':'기록 없음','PENDING':'처리 대기','FIXTURE':'검증용 가상 결과',
}

COLUMNS = {
    'blockers':'차단 사유','readiness_blockers':'준비 대기 이유',
    'strategy_status':'전략 판단','strategy_diagnostics':'전략 대기 이유',
    'read_attempts':'실제 조회 시도 수','focus_missing_inputs':'집중 선정 부족 자료',
    'retry_deferred':'재조회 대기','retry_after':'다음 조회 가능 시각',
    'session':'거래일','current_session':'당일 자료','engine_minutes_missing':'전략에 전달할 분봉 수',
    'received_minutes':'확보한 분봉 수',
    '403':'접근 제한 횟수','429':'요청 제한 횟수',
    'id':'기록 번호','kind':'구분','status':'상태','code':'사유','timestamp':'시각','observed_at':'확인 시각',
    'symbol':'종목','ticker':'종목','provider':'공급자','source':'출처','message':'설명','reason':'사유',
    'reasons':'사유','level':'수준','severity':'수준','error_type':'오류 종류','date':'날짜',
    'strategy':'전략','equity':'총자산','cash':'현금','realized_pnl':'매매 손익','net_pnl':'순손익',
    'unrealized_pnl':'평가 손익','total_return':'총수익률','return_pct':'수익률','win_rate':'승률',
    'trade_count':'거래 수','trades':'거래 수','max_drawdown':'최대 손실폭','profit_factor':'손익비',
    'received_at':'수신 시각','event_at':'발생 시각','published_at':'발행 시각','valid_until':'유효 시각',
    'started_at':'시작 시각','finished_at':'완료 시각','checked_at':'검사 시각','updated_at':'갱신 시각',
    'duration_seconds':'소요 시간(초)','elapsed_seconds':'소요 시간(초)','calls':'요청 수','path':'조회 경로',
    'category':'분류','priority':'우선순위','success':'성공 여부','count':'건수','error':'오류',
    'http_status':'응답 번호','operation':'작업','endpoint':'조회 경로','attempt':'시도 수','latency_ms':'지연(ms)',
    'session_open':'개장 시각','session_close':'폐장 시각','expected_minutes':'필요 분봉 수',
    'observed_minutes':'수집 분봉 수','missing_minutes':'빠진 분봉 수','missing_rate':'누락 비율',
    'conflicting_minutes':'값이 다른 분봉 수','five_count':'5분봉 수','fifteen_count':'15분봉 수',
    'five_coverage':'5분봉 확보율','fifteen_coverage':'15분봉 확보율','five_expected':'필요 5분봉 수',
    'fifteen_expected':'필요 15분봉 수','last_closed_at':'마지막 확정봉','last_received_at':'마지막 수신',
    'age_seconds':'자료 경과(초)','latest_receipt_lag_seconds':'수신 지연(초)','price_adjustment':'가격 조정',
    'missing_examples':'누락 시각 예시','explicit_zero_volume_minutes':'거래량 0인 분봉',
    'identical_duplicates':'중복 분봉','cursor':'다음 조회 위치','pages':'조회 횟수','complete':'완료 여부',
    'commission':'수수료','regulatory':'거래 부과금','total':'합계','qty':'수량','price':'가격',
}


def label(value):
    return LABELS.get(value,value) if isinstance(value,str) else value


def display_rows(rows):
    """미등록 코드도 화면에는 노출하지 않고 별도 원본 보기로 보존한다."""
    unknown=False; headers={};used=set()
    def heading(key):
        nonlocal unknown
        if key in headers:return headers[key]
        name=COLUMNS.get(key,key)
        if not re.search('[가-힣]',str(name)):
            unknown=True;name='추가 정보'
        base=name;index=2
        while name in used:name=f'{base} {index}';index+=1
        used.add(name);headers[key]=name
        return name
    def value(item,key=''):
        nonlocal unknown
        if key == '사유 코드':
            from .status_labels import safe_reason_code
            return safe_reason_code(item) or '원문 표시 제외'
        if key in {'blockers','readiness_blockers','차단 사유','대기·차단 이유','제한 사유','차단·거절','신규 진입 차단'}:
            from .status_labels import blocker_details, blocker_summary, is_reason_list
            if is_reason_list(item):
                unknown = unknown or any(not row['known'] for row in blocker_details(item))
                return blocker_summary(item)
        if isinstance(item,bool):return '예' if item else '아니요'
        if isinstance(item,dict):
            if any(k not in COLUMNS and not re.search('[가-힣]',k) for k in item):unknown=True
            return ' · '.join(f'{COLUMNS.get(k,k if re.search("[가-힣]",k) else "추가 정보")}: {value(v,k)}' for k,v in item.items())
        if isinstance(item,(list,tuple)):return ', '.join('—' if v is None else str(value(v,key)) for v in item)
        if not isinstance(item,str):return item
        if key in {'제목','원문','원문 근거','분류 이유','분석 근거','title','body','evidence','article_url',
                   'symbol','ticker','종목','종목·주제','id','모델','계좌 검증 범위'} or 'ID' in key:return item
        if item in LABELS:return LABELS[item]
        if item.startswith(('http://','https://')):return item
        if item.startswith('rss:'):return 'RSS 피드'
        if item.startswith('sec:'):return '미국 공시 · '+item.split(':',1)[1]
        if item.startswith('http_'):return {'http_401':'인증 필요','http_403':'접근 제한','http_429':'요청 제한'}.get(item,'공급자 응답 오류')
        def token(match):
            nonlocal unknown
            name=match.group()
            if name in LABELS:return LABELS[name]
            if '_' in name:unknown=True;return '상세 확인 필요'
            return name
        return re.sub(r'[A-Za-z][A-Za-z0-9_]*',token,item)
    return [{heading(str(k)):value(v,str(k)) for k,v in row.items()} for row in rows],unknown


def market_hours_label(now):
    from ..session_window import schedule_times
    try:
        times=schedule_times(now)
        if times is None:return '정규장 아님 · 뉴스 확인 가능'
        if now < times['opens_at'] or now >= times['closes_at']:return '장외 시간 · 뉴스 확인 가능'
        if times['entry_start'] >= times['entry_end']:return '오늘 신규 매수 없음 · 보유 종목 관리'
        if now < times['entry_start']:
            from zoneinfo import ZoneInfo
            starts = times['entry_start'].astimezone(ZoneInfo('Asia/Seoul')).strftime('%H:%M')
            return f'매수 시작 대기 · {starts} KST부터 조건 확인'
        if now >= times['entry_end']:return '신규 매수 마감 · 보유 종목 관리'
        return '정규장 · 매수 조건 확인 시간'
    except (ValueError,TypeError,RuntimeError):return '시장 시간 확인 필요'

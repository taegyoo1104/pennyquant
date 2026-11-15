> **과거 기록** · 작성 당시의 기획·구현을 보관한 문서입니다. 현재 프로젝트는 [소개](../../../README.md)와 [시작 안내](../../GETTING_STARTED.md)를 먼저 읽어 주세요.

# 단계 04 — 네 전략·위험·청산 보완 결과

이번 작업은 최신 02 보완을 테스트한 뒤 기존 04 작업을 복원·보완했다. 05 실데이터 기록/외표본 백테스트와 후속 단계는 시작하지 않았다. 기본 `run`은 계속 PAPER_LOCAL이며 한 번에 선택한 전략 하나를 평가한다.

## 변경 내용과 파일

| 영역 | 변경 파일 | 동작 |
|---|---|---|
| 확정봉 전략 | `sessionquant/market/{bars,strategies}.py` | 이전/현재 VWAP 기준 시각·계산 시각, 5/15분봉의 종목·세션·정렬·수신 시각 검증. 미래·미확정·과잉 추격 차단, Pullback 지지선을 VWAP 또는 EMA로 확인 |
| 배분·추가매수 | `sessionquant/portfolio/{models,manager}.py` | 최초/추가 주문의 예상 비용·기존 수량·동시 예약액·평가 손실·종목/전체 비중을 재계산. 신규 주문의 매수호가 근거 저장, 계획상 평단 낮추기 금지 |
| 체결·청산 | `sessionquant/portfolio/{session,market_fills,legacy_fills}.py` | 매수 취소 요청→확인→현재 잔량 조회→한 번의 매도. 미확인 주문은 완료로 표시하지 않음. 시장가 갭으로 실제 추가 체결 가격이 기존 평단 아래가 되면 숨기지 않고 경고 |
| 청산 가설 | `sessionquant/portfolio/exit_policy.py`, `sessionquant/risk_experiments.py`, `sessionquant/session_backtest.py`, `sessionquant/cli.py` | ATR·고정 손절, 수익 목표·이익 보호·보유 시간의 독립 비교. 새 보고서·임시 SQLite만 사용하고 최적값을 자동 반영하지 않음 |
| 참고 위험 감시 | `sessionquant/market_reference/risk.py`, `sessionquant/session_config.py` | 검증된 참고 시계열에 NORMAL/WATCH/ELEVATED/UNKNOWN, 가설 버전·시각·사용 자료·히스테리시스·쿨다운·중복 방지 기록 |
| 통합·복구 | `sessionquant/{session_engine,session_runtime}.py` | 뉴스 조회 실패 시 UNKNOWN과 기존 진입 제한 보존, 가격 손절·시간 청산 계속. 위험 정책·연구 청산 설정 복구 검사 |
| 기존 화면 보완 | `sessionquant/dashboard/{session_app,reference_panel}.py` | 예상 위험·취소 대기·과거 위험 평가 시각·참고 만료 표시. FRED 공식 일별 표와 ETF 관측을 구분. 기존 메인 탭 유지 |
| 검사 | `tests/test_strategy_validation_matrix.py`, `test_strategy_risk_exit.py`, `test_macro_reference_risk.py`, `test_risk_experiments.py`, `test_market_reference.py`, `test_mysql_integration.py`, `test_session_dashboard.py` | 독립 조건·ON/OFF·오류·복구·비교 및 기존 화면 회귀 |
| 안내 | 본 문서, `docs/SESSIONQUANT_CURRENT.md`, `docs/REVISION_00_04_AUDIT.md`, `README.md` | 이번 범위·실제 경로·미검증 입력·다음 승인 경계 |

## 네 전략 검증 표

공통으로 BULLISH·최신 확정봉·동일 종목/세션·유한 지표·최대 ATR 추격 폭·뉴스 진입 제한을 적용한다. 실제 `run`의 적격성·시총·RVOL 엄격 검사를 완화하지 않는다.

| 전략 | 독립 검증 | 테스트 |
|---|---|---|
| Momentum | 15분 상승 구조·최근 5분 고점 돌파·VWAP/EMA 위·거래량 | `test_momentum_independently_checks_high_breakout_and_volume`, 공통 상승 구조 검사 |
| Pullback | 두 조정봉·정상 조정 폭·VWAP 또는 EMA 지지·직전 고가 돌파·거래량 회복 | `test_pullback_two_adjustments_rebound_and_depth_are_independent`, 지지선 각각 검사 |
| Gap Breakout | 첫 15 또는 30분 OR 완성·범위 돌파·VWAP·거래량·추격 제한 | `test_opening_range_uses_only_first_fifteen_or_thirty_minutes_and_requires_coverage` |
| VWAP Reclaim | 이전 가격/VWAP와 현재 VWAP의 올바른 기준 시각·상향 회복·가격 확인 | `test_vwap_reclaim_uses_previous_and_current_cutoffs_with_price_confirmation` |

`test_four_single_strategies_use_closed_signal_id_and_common_risk_gates`와 시각 경계 행렬은 동일 확정봉 재평가·약세/중립/UNKNOWN·뉴스 차단·미래 5/15분 자료·잘못된 VWAP 기준·과잉 추격을 검사한다. 재시작/중복 봉 이후 같은 신호로 새 주문이 생기지 않는지도 검사한다. 네 전략 동시 운용 기능을 추가하지 않았다.

## 참고 위험 감시와 기본 정책

경로는 `MarketReferenceMonitor → 별도 참고 원장 → MacroReferenceRiskMonitor → 관찰 기록`이다. 기존 MarketEngine 국면과 병렬이며 네트워크·LLM·주문 API를 호출하지 않는다.

```yaml
macro_reference_filter:
  enabled: false
  paper_shadow_only: true
  fail_closed: true
  cooldown_seconds: 300
  clear_samples: 3
  threshold_version: hypothesis_v1
  ief_15m_pct: '-0.30'
  tlt_15m_pct: '-0.70'
  uso_30m_abs_pct: '2.0'
```

임계값은 **ETF 가격 등락률 가설**이다. 금리 bp·WTI 현물 변화로 환산하지 않는다. SHY/IEF/TLT/SPY/QQQ는 15분 최소 16개, USO는 30분 최소 31개의 연속된 유효 관측을 요구한다. 최소 표본뿐 아니라 정확한 창 길이·끝 시각 일치·원본/수신/계산/유효 시각·실시간 권한·비교 가능성이 필요하다. 모든 5/15/30분 창과 변동성·일간 비교 가능성을 근거에 남긴다. 지연·누락·정정·만료·시간대 혼합은 UNKNOWN이다.

유효 채권 ETF 하락 또는 원유 ETF 급변은 WATCH, 여기에 SPY/QQQ의 15분 동반 하락이 있으면 ELEVATED다. 가격 예측이나 안전 확정이 아니다. 위험 이후에는 서로 다른 유효 정상 관측 3회와 300초 대기가 모두 충족되어야 해제된다. 같은 자료를 반복 조회해 회복 횟수를 늘리지 않는다. UNKNOWN은 회복 표본이 아니며 이전 위험 제한을 해제하지 않는다.

`pipeline_status.kind=macro_reference_risk`와 `macro_reference_risk_state_v1`/`macro_reference_risk_view_v1`에 사건 ID·상태·원본 시계열·평가/사용 가능 시각·가설 버전·명시적 해제 사유·활성화/관찰 여부를 보존한다. 같은 사건을 다시 평가해 새 사건이나 새 수신 시각으로 만들지 않는다.

**현재 운영 설정에서 `enabled: true`와 `paper_shadow_only: false`의 결합은 거부한다.** 공급자 검증·05 외표본 결과·사용자 활성화 전에는 Paper에서도 실제 매매 판단을 바꾸지 않는다. ON의 신규 진입/추가매수 차단·미체결 매수 취소 분기는 `research_only=True`와 합성 엔진의 격리 단위 테스트로만 검사한다. 실제 운영 활성화 절차는 미구현이며 이번 범위가 아니다. ON의 UNKNOWN 기본은 fail-closed이고 설정별 동작을 검사했다. OFF와 shadow에서는 거래에 영향이 없다.

이 감시기는 ETF 단독 강제 청산 의도를 만들지 않는다. 종목 손절·확정 기업 악재·일일 중단·수동 전체 종료·장 종료 청산은 기존 경로로 계속 실행한다. 뉴스 위험 조회의 429/timeout이 나도 ETF 수치 계산과 보유 손절을 계속한다. DB 쓰기가 실패한 경우에는 기존 정책대로 신규 주문을 진행하지 않고 엔진을 실패 상태로 둔다.

## 배분·손익·청산 해석

기본 자금 $100, 최초 20%, 추가 20%/1회, 종목 40%, 총 80%, 최대 5종목, 종목 위험 예산 0.75%, 일일 손실 2%, 목표 10%를 임의 최적값으로 바꾸지 않았다. 2종목은 권장 초기 운영 수일 뿐 강제 매수 수가 아니며 0종목도 허용한다.

추가매수 계획은 기존 수량+신규 수량, 기존 원가, 신규/최종 매도 수수료, 예상 슬리피지, 예약 현금/수수료/평가 손실을 포함한다. 종목별·전체 비중은 예상 비용 차감 후 자산을 기준으로 재검사한다. 확인된 상승·지지·반등 및 기존 평단보다 높은 진입 가격이 필요하며 기존 손절은 낮추지 않는다. 계획과 실제 갭 체결은 다를 수 있어, 실제 체결을 취소된 것처럼 기록하지 않고 초과 위험을 경고한다.

10%는 도달할 때까지 무조건 기다리는 조건이 아니다. 설정에 따라 수익 중 하락 시 이익 보호, 확정 악재, 손절, 연구 보유 시간·당일 시간 청산이 먼저 발생할 수 있다. 2% 일일 손실은 신규 매수 중단 기준이며 보장 손실 상한이 아니다. 급변·시세 지연·부분 체결·장애 시 손실이 더 커질 수 있다.

정규장 기준 신규 진입은 10:45~13:30, 청산 시도 14:40, 접수 마감 15:00(뉴욕시간)이라는 기존 브로커 가정을 유지한다. 조기 폐장·DST는 달력과 기존 `min_entry_to_exit_minutes`를 반영한다. 청산 직전 신규 진입을 허용하지 않는다. 실제 토스 계좌의 소수점 접수 시간·최소 금액·취소/체결 API의 실측 검증은 별도 필요하다. 매도 요청과 전량 체결은 다르며, 포지션이 0이어도 미확인/열린 매수 주문이 있으면 청산 완료가 아니다.

## 비교 명령과 데이터 구분

기존 기록 또는 합성 데모 테이프를 사용하며 새로운 실제 데이터 수집·05 외표본 기능을 추가하지 않았다.

```powershell
# 기존 합성 검증 자료가 없다면 새 경로에 생성한다. 실제 API 호출 없음.
.\venv\Scripts\python.exe -m sessionquant demo --out data/stage04-demo

# 10개 가설 × 네 전략 각각 단독 = 40개 비교. 보고서 경로는 새 파일이어야 한다.
.\venv\Scripts\python.exe -m sessionquant risk-experiments --input data/stage04-demo/synthetic.jsonl --universe data/stage04-demo/universe.json --report data/stage04-risk.json

# 전체 동작 검사. MySQL 검사는 별도 격리 환경에서만 선택적으로 활성화한다.
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m ruff check sessionquant tests
```

비교 가설은 목표 2/5/10%, ATR 1/2배, 고정 손절 1/2%, 이익 보호 OFF, 최대 보유 30/60분이다. 전략별 임시 SQLite 장부를 사용하며 운영 MySQL에 실험을 쓰지 않는다. 보고서에는 비용 차감 손익·수수료·미청산·체결된 청산 사유·위험 거절을 기록한다. `recommended_parameters=null`, `profitability_validated=false`이며 수익률 검증 주장이 아니다. 실제/검증되지 않은 기록은 데이터 검증 UNKNOWN으로 표시한다.

시장가 근사·스프레드·슬리피지·실제 모의 체결 수수료는 포함하되 미청산의 미래 비용, LLM·인프라 비용은 포함하지 않는다. 입력에 없는 시각의 호가를 만들어 청산하지 않는다. 이번 실제 계좌·유료 LLM·주문 API 호출은 0회다.

## 호환성과 남은 작업

기존 주문·이벤트 ID와 금융 기록을 이름 변경이나 새 정책 때문에 다시 쓰지 않는다. `risk_policy_version=2`, 선택적 `reference_bid`·위험 감사 필드가 추가된다. 과거 대기 매수에 매수호가 근거가 없으면 새 배분 시 UNKNOWN으로 보류한다. 복구 때 청산 연구 정책을 바꾸면 거부하며 별도 DB가 필요하다. 이전 체결 장부는 계속 `legacy_limit_v1`으로 분리한다.

00 CI 미구축, 01 실측 시총 공급, 02 실제 계좌 적격성·기업행동·RVOL 비교 자료·참고 ETF 권한은 해결되지 않은 외부 검증 항목이다. 기본 엄격 검사 아래에서 Paper 진입이 계속 0건일 수 있으며 이를 해결하려 검사를 우회하지 않았다. 사용자 로컬 실제 읽기 검사·수익성·소액 실거래 검증은 이번 자동 테스트 결과로 대체하지 않는다.

**다음 단계: 05 보류.** 사용자가 승인하면 실제 데이터의 수신 시각 기반 기록·재생·백테스트와 외표본 비교를 별도 범위로 진행한다. 현재는 코드·외부 서비스 설정을 변경하지 않고 승인 대기한다.

## 최종 검증 결과 — 2026-11-14

| 검사 | 결과 |
|---|---|
| 선행 개정 02 전체 (`PQ_TEST_MYSQL=1 python -m pytest -q`, 격리 MySQL) | 801 통과·실패/제외 0 |
| 전략 조건·위험·취소·청산 복원 검사 | 69 통과 |
| 참고 위험 ON/OFF·오류·회복 및 40개 오프라인 비교 검사 | 41 통과 |
| 전략·참고 관측·기존 화면 집중 회귀 | 145 통과 |
| 실제 격리 MySQL 8 전체 (`PQ_TEST_MYSQL=1 python -m pytest tests/test_mysql_integration.py -q`) | 13 통과 |
| **최종 전체 (`PQ_TEST_MYSQL=1 python -m pytest -q`)** | **913 통과·실패/제외 0**, 81.34초 |
| `python -m ruff check sessionquant tests`, `git diff --check` | 통과 |
| wheel 빌드, CLI 도움말 | 통과 |

위 검사 집합은 서로 겹치므로 횟수를 합산하지 않는다. 기존 pandas 시간대 배열 경고 8개는 남아 있다. 합성 정상/지연/결측/정정·UNKNOWN·ON/OFF·손절·14:40 청산·2% 중단·10% 전 이익 보호·부분 체결·취소 대기·재시작·시장 달력의 기존 검사도 전체 실행에 포함됐다. 테스트용 MySQL은 임의 네임스페이스를 사용하고 자체 기록만 정리한 뒤 컨테이너를 중지한다.

판정은 **구현·합성/격리 DB 회귀 검증 완료, 실제 운용 검증 부분완료**다. 새로운 ETF 위험 필터는 관찰 전용이고 공급자/계좌·시총·기업행동/RVOL의 UNKNOWN 항목은 계속 진입을 제한한다. 실거래 활성화·사용자 DB 초기화·실제 주문·유료 LLM·후속 단계 구현은 수행하지 않았다.

# SessionQuant 현재 실행 경로

이번 기준선은 `568d29b` 이후의 소스를 분석했다. 첨부 로드맵의 기준 `f587e162` 이후 추가된 기본 RSS 목록을 유지하면서 단계 01의 감시 정책을 분리했다.

**`run`은 PAPER_LOCAL 전용이며, `live-local`은 기존의 제한적 1종목 주문 검증 경로다. 네 전략의 완전 자동 실거래나 GCP 운영을 완료한 상태가 아니다.**

| 명령 | 실제 연결 경로 | 주문 경계 |
|---|---|---|
| `run` | `session_runtime` → WatchlistManager → TossSessionSource → SessionEngine → PaperBroker | 모의 주문만. 신규 매수는 시작 버튼과 모든 위험 검사를 통과해야 함 |
| `run-account` | 토스 읽기 API → 실제 계좌 스냅숏 → 메인 화면 | 조회 전용 |
| `live-local` | 별도 실계좌 대사·위험 관리 → 제한적 Toss 어댑터 | 기본 사전 조회. 실제 주문은 매 실행 별도 활성화 조건 필요 |
| `demo` / `replay` / `backtest` | 합성 또는 수신 기록 → 별도 모의 장부 | 외부 실주문 없음. 최신 감시목록을 과거에 자동 적용하지 않음 |
| `watchlist-refresh` | 검증된 로컬 시총 입력 → PAPER 선택 이력 | 외부 API·주문 없음. 실행 중인 작성자와 파일 잠금 공유 |
| `market-data-status` | PAPER 품질 보고서 → 만료·확보율 재검사 | 외부 API·주문·LLM 호출 없음 |
| `record-export` / `record-quality` | 중지한 PAPER 원장 → 불변 처리 시점 묶음 → 무결성·품질 | 새 파일만 생성, 외부 API·실주문·LLM 호출 없음 |
| `replay-recording` / `evaluate-recording` | 처리 시계·당시 뉴스/참고 자료 → 격리 SQLite Paper → 고정 정책·외표본 구간 진단 | 운영 장부·위험 필터 활성화 없음, 실제 성과는 미검증 |
| `watchlist-status` / `watchlist-export` | PAPER 감시·집중 이력 조회 | 주문·LLM 생성 없음 |
| `legacy` / 이전 호환 수집 명령 | `legacy_cli` 및 이전 PennyQuant 경로 | 현재 100→20 정책과 분리. 별도 매뉴얼 참조 |

`pennyquant` 패키지 이름은 호환 진입점이다. 새 운영 안내는 이 문서와 [단계 01](STAGE01_WATCHLIST.md), [단계 02](STAGE02_MARKET_DATA.md), [MySQL](MYSQL_LOCAL.md), [실거래 제한](PHASE5_LOCAL.md)을 따른다. 과거 `SYSTEM_RUN.md`, `STRATEGIES.md`, `LEGACY_README.md`의 안내를 새 감시 정책으로 해석하지 않는다.

## 공통 회귀 검사

```powershell
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\python.exe -m ruff check sessionquant tests
.\venv\Scripts\python.exe -m sessionquant doctor
.\venv\Scripts\python.exe -m sessionquant --help
```

| 영역 | 유지할 증거 |
|---|---|
| 데이터·시각 | 미래/미확정 봉 차단, 거래일·DST·조기 폐장, 원래 수신 시각 보존 |
| 주문·위험 | Decimal, 일일 손실 한도, 실제 접수 불명 상태의 재전송 차단, 손절·당일 청산 |
| DB·재시작 | PAPER/LIVE 격리, 비파괴 복구, 멱등성, 단일 작성자 잠금 |
| 뉴스·LLM | 출처·근거 검증, 미확인 사건은 UNKNOWN, 잔액 부족 시 차트·청산 계속 |
| 화면 | 실제 계좌와 모의 장부 구분, 과거 상태와 현재 유효성 구분 |

이 단계의 코드 변경 전 기준선은 **631개 통과·선택 MySQL 검사 8개 제외**다. 기존 경고는 pandas의 시간대 배열 변환 관련 경고다. 기존 실패는 확인되지 않았다. 외부 토스 계좌·유료 LLM·사용자 PC MySQL은 이 기준선 검사에 포함하지 않았다.

첨부 `00_baseline_docs_ci.txt`의 전체 CI 구축은 별도 단계 00 작업이다. 이 요청은 단계 01이므로 GitHub Actions를 새로 활성화하거나 다음 단계 기능을 묶지 않았다. 이 문서는 현재 경로와 회귀 기준에 필요한 최소 진입 문서다. CI 미구축 상태를 실계좌 안전성 검증 완료로 해석하지 않는다.

단계 02부터 `run`은 엄격한 시세·적격성·RVOL 근거 검사를 사용한다. 위험 호가와 분봉은 후보 탐색 전에 엔진에 전달하고 과거 페이지는 남은 예산에서만 보충한다. 실제 계좌 자격·기업행동·시총 공급 자료의 차단 항목은 아직 해결되지 않았다. 자세한 출처·정책·호환 범위는 단계 02 문서를 따른다.

단계 03부터 새 Paper 기본 체결은 `market_v2`다. 기존 버전 1 장부는 `legacy_limit_v1`으로 복구하며 기본 시장가 장부로 자동 전환하지 않는다. [새 실행 공간·호환 방법](STAGE03_PAPER_FILLS.md)을 먼저 확인한다. `run-account`, 제한적 `live-local`의 실제 주문 연결은 이 변경에 포함하지 않았다.

최신 개정의 [00~04 점검](REVISION_00_04_AUDIT.md), [02 참고 ETF 보완](STAGE02_MARKET_DATA.md), [04 전략·위험·청산 결과](STAGE04_STRATEGY_RISK_EXIT.md), 승인된 [05 기록·재생·평가](STAGE05_RECORDING_BACKTEST.md)를 함께 확인한다. `market_reference`는 거래 100/집중 20 정원 밖의 읽기 전용 관측이다. `macro_reference_filter` 기본값은 OFF·shadow이며 운영 매매 적용은 아직 허용하지 않는다. `risk-experiments`는 기존 자료의 손절·익절 가설 비교이며, 새 기록의 시점 정합 평가와 홀드아웃 계획은 `evaluate-recording`을 사용한다. 실제 장기간 데이터와 효과 검증은 미완료다. 승인된 [06 뉴스·공식 일정](STAGE06_NEWS.md), [07 LLM 감사·근거·비용](STAGE07_LLM.md), [08 후보·포트폴리오](STAGE08_CANDIDATES.md)는 구현·격리 PAPER 회귀 범위다. [방향·단계 경계](PROJECT_DIRECTION.md)에 KIS 취소를 반영했다. 승인된 [08A 통합 대시보드](STAGE08A_DASHBOARD.md)에서 6개 화면과 기존 큐 제어를 제공한다. 실제 공급자·계정·모델 품질·실거래 운영 검증과 09/10 이후는 별도 승인 후 진행한다.

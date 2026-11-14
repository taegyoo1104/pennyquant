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
| `watchlist-status` / `watchlist-export` | PAPER 감시·집중 이력 조회 | 주문·LLM 생성 없음 |
| `legacy` / 이전 호환 수집 명령 | `legacy_cli` 및 이전 PennyQuant 경로 | 현재 100→20 정책과 분리. 별도 매뉴얼 참조 |

`pennyquant` 패키지 이름은 호환 진입점이다. 새 운영 안내는 이 문서와 [단계 01](STAGE01_WATCHLIST.md), [MySQL](MYSQL_LOCAL.md), [실거래 제한](PHASE5_LOCAL.md)을 따른다. 과거 `SYSTEM_RUN.md`, `STRATEGIES.md`, `LEGACY_README.md`의 안내를 새 감시 정책으로 해석하지 않는다.

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

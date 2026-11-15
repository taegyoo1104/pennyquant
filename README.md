# SessionQuant

현재 실행 경로는 [SESSIONQUANT_CURRENT.md](docs/SESSIONQUANT_CURRENT.md), 새 감시 정책은 [단계 01](docs/STAGE01_WATCHLIST.md)을 따릅니다. **`run`은 시총 감시 최대 100 → 집중 분석 최대 20의 PAPER_LOCAL 경로입니다.** 토스 섹터 시총을 자동 수집하며, 검증 파일도 선택할 수 있습니다. 구성 종목 전체의 시총이 확인되지 않으면 UNKNOWN/PARTIAL로 표시하고 신규 진입을 차단합니다. S&P 500 전체 편입 원장과 기존 보유·미체결 위험 관리는 유지합니다.

[단계 02 시세·자격·RVOL 품질 안내](docs/STAGE02_MARKET_DATA.md): `run`은 근거·만료를 검사하고 위험 자료를 후보 탐색 전에 전달합니다. `market-data-status`로 확보율과 부족 입력을 조회합니다. 실제 토스 계좌 자격·분할 거래량 비교 근거가 없는 항목은 UNKNOWN을 유지합니다.

[단계 03 Paper 체결 안내](docs/STAGE03_PAPER_FILLS.md): 새 모의 체결 기본값은 `market_v2`입니다. 기존 가격제한 장부와 섞지 않도록 새 DB 설정 예시를 제공합니다. 실제 주문 연결은 변경하지 않았습니다.

[단계 04 전략·위험·청산 결과](docs/STAGE04_STRATEGY_RISK_EXIT.md): 추가매수 위험 재계산·취소 확인 후 잔량 청산·오프라인 매개변수 비교를 보완했습니다. 시장 참고 ETF 위험 감시는 기본 OFF·관찰 전용입니다. 실제 공급자/계좌 검증과 외표본 결과 전에는 운영 매매 필터를 활성화하지 않습니다. [00~04 점검](docs/REVISION_00_04_AUDIT.md)을 참고하세요.

[단계 05 실데이터 기록·재생·평가](docs/STAGE05_RECORDING_BACKTEST.md): `run`의 실제 처리 시계와 분석 버전을 별도 원장에 기록하고 `record-export`, `record-quality`, `replay-recording`, `evaluate-recording`으로 검증합니다. 새 기록 설정은 [sessionquant.recording.example.yaml](config/sessionquant.recording.example.yaml)입니다. 실제 장기간 기록·외표본 효과는 아직 미검증이며, 07 이후는 별도 승인 후 진행합니다. 이 단계 05는 아래에서 언급하는 기존 Phase 5 실거래 기능과 구분합니다.

[단계 06 뉴스·원문·공식 일정](docs/STAGE06_NEWS.md): 기업·거시 독립 주기, 분석 전 신선도/출처 차단, BLS403·GDELT429 대기, 허용 발행사 원문 검증과 공식 일정 수정·취소 이력을 보완했습니다. 대시보드 진단과 SELECT 전용 SQL을 포함하며 실제 공급자·실거래 운영은 미검증입니다. 07단계는 승인 대기입니다.
 2.1

S&P 500 구성 종목의 장중 추세와 SPY·QQQ 시장 방향, 기업·경제 뉴스를 함께 확인하는 Python 3.12 이상 모의매매 시스템입니다. 일반 `run`은 Paper Trading 전용입니다. Phase 5의 첫 1종목 소액 실주문 검증 경로 `live-local`이 추가됐으며 기본은 주문 없는 계좌 검사입니다. 실제 계좌 검증과 완전 자동 실거래 연결은 별도 단계이고 GCP 배포는 Phase 6 범위입니다.

GitHub 저장소 주소는 [`taegyoo1104/pennyquant`](https://github.com/taegyoo1104/pennyquant)를 그대로 사용합니다. 프로젝트·패키지·실행 명령은 `SessionQuant` / `sessionquant`로 바뀌었습니다. 과거 주문 ID와 SQLite 기록은 자동 변경하지 않습니다.

## PyCharm에서 업데이트하고 실행하기

기존에 **GitHub에서 클론한 프로젝트**를 열고 `Ctrl+T`로 업데이트합니다. PyCharm 터미널에서 다음을 실행하세요. 아래 예시는 가상환경 폴더 이름이 `venv`인 Windows 환경입니다.

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.lock
.\venv\Scripts\python.exe -m pip install --no-deps -e .
.\venv\Scripts\python.exe -m sessionquant doctor
```

가상환경 이름이 `.venv`라면 명령의 `venv`를 `.venv`로 바꾸세요. 새 패키지와 CLI 등록을 위해 두 번째 설치 명령도 필요합니다.

먼저 API 키와 실제 계좌 없이 합성 데이터로 기능을 확인할 수 있습니다.

```powershell
.\venv\Scripts\python.exe -m sessionquant demo --out data/session-demo
.\venv\Scripts\python.exe -m sessionquant dashboard --backend sqlite --db data/session-demo/sessionquant-demo-session-demo.db
```

브라우저에서 **http://localhost:8501**을 엽니다. 합성 데모의 거래와 수익은 실제 시장 성과가 아닙니다. 기존 결과를 덮어쓰지 않도록 다시 실행할 때는 `--out data/session-demo-2`처럼 새 폴더를 사용하세요.

## 로컬 MySQL과 토스 시세로 실행하기

[로컬 MySQL 설정](docs/MYSQL_LOCAL.md)에 따라 전용 데이터베이스와 제한된 사용자 계정을 만들고 `SQ_DB_BACKEND=mysql`, `SQ_MYSQL_HOST=127.0.0.1`, `SQ_MYSQL_DATABASE`, `SQ_MYSQL_USER`, `SQ_MYSQL_PASSWORD`를 본인 PC 환경에 설정합니다. 비밀번호는 Git이나 채팅에 넣지 않습니다. `.env.example`은 변수 이름 안내이며, 실제 `.env`는 실행할 때 자동으로 읽습니다. 한 번 저장하는 방법은 아래를 참고하세요.

```powershell
.\venv\Scripts\python.exe -m sessionquant db-check --db data/sessionquant-paper-local.db
.\venv\Scripts\python.exe -m sessionquant check-toss --symbol AAPL
.\venv\Scripts\python.exe -m sessionquant universe-refresh --output data/sp500-universe.json
.\venv\Scripts\python.exe -m sessionquant run --config config/sessionquant.yaml
```

토스 연결에는 기존 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`을 사용합니다. 새 대시보드는 시장 상태·S&P 500 후보·소수점 모의 포지션·기업/거시 뉴스·LLM 비용·당일 청산 상태를 표시합니다. 종료는 실행 터미널에서 `Ctrl+C`입니다.

**시세가 들어온다는 사실만으로 거래 가능 상태를 확인한 것으로 처리하지 않습니다.** 소수점 지원, 계좌 거래 가능 여부, 거래정지, 기업행동, 비교 가능한 RVOL 또는 최신 호가가 부족하면 해당 종목은 관찰 상태를 유지합니다. 토스 계좌의 실제 주문 규칙은 Phase 5에서 공식 문서와 계좌 응답으로 검증합니다. 정상적인 무거래 상태와 수집 오류는 대시보드에서 구분합니다.

## 현재 시스템의 판단 흐름

1. 확인 시각과 편입 이력이 있는 S&P 500 유니버스에서 거래 후보를 찾습니다.
2. 완료된 1분봉으로 5분봉·15분봉을 만들고 VWAP·EMA·ATR·거래량을 계산합니다. 누락·미완성·미래 봉으로 신호를 만들지 않습니다.
3. SPY·QQQ의 최신 상태로 시장을 `BULLISH`, `NEUTRAL`, `BEARISH`, `UNKNOWN`으로 분류합니다.
4. 네 전략 `momentum`, `pullback`, `gap_breakout`, `vwap`의 조건을 확인합니다. 긍정 뉴스가 없어도 기술적 조건으로 후보가 될 수 있습니다.
5. SEC·기업 RSS·Alpaca 보조 자료 및 공식 경제기관·GDELT 기사를 분리 분석합니다. 미확인 기사와 LLM의 추측을 확정 사실로 바꾸지 않습니다.
6. 비중·위험 예산을 통과한 주문만 소수점 Paper Broker에 전달합니다. 주문 전송과 체결을 구분하고 다음 유효 호가에서 체결을 모의합니다.
7. 손절·이익 보호·일일 손실 제한·당일 청산을 별도로 관리하고 상태를 DB에 저장합니다.

LLM 수집·분석 작업은 시장 처리 및 청산 관리와 분리됩니다. 뉴스 API가 실패하면 뉴스 상태를 미확인으로 남기며, 기존 포지션의 위험 관리는 계속합니다.

## 기본 자금·위험 설정

| 항목 | 초기 설정 |
|---|---:|
| 모의 초기 자본 | $100 |
| 최대 보유 종목 | 5개, 초기 2개 중심 |
| 최초 진입 목표 | 계좌 평가금액의 20% |
| 추가매수 | 최대 1회, 목표 20% |
| 종목당 최대 비중 | 40% |
| 전체 투자 상한 | 80% |
| 종목별 손실 예산 | 0.75% |
| 일일 손실 한도 | 2% |

금액과 소수점 수량은 `Decimal`로 처리합니다. 목표 금액은 매수 가능 현금, 기존 주문 예약금, 수수료·슬리피지, 손절 거리, 위험 예산에 따라 줄어들 수 있습니다. 추가매수는 지지와 재상승을 확인한 경우만 허용하며 기존 손절을 자동으로 낮추지 않습니다.

정규장 초기 시간표는 뉴욕시간 **10:45 진입 시작 → 13:30 신규/추가매수 마감 → 14:40 청산 시작 → 14:50 미청산 경고**입니다. 거래소 달력의 휴장·조기 폐장을 반영합니다. 현재 소수점 주문 마감은 모의 시뮬레이션 가정이며 실제 계좌 주문 시간을 보증하지 않습니다.

## 뉴스와 LLM

- 기업: SEC EDGAR, 검증된 기업 IR/RSS, 무료 Alpaca REST 보조 자료.
- 거시: Federal Reserve·BLS·BEA·Treasury·EIA 공식 자료, GDELT 글로벌 기사 탐색.
- FRED: `DGS2`, `DGS10`, `DCOILWTICO` 일별 참고 자료. 실시간 주문 신호로 취급하지 않습니다.
- LLM: 모델·프롬프트 버전·토큰 사용량·분석 비용·캐시를 기록하고 일일 호출 수 및 월간 예산을 관리합니다.

기획서의 `gpt-6-luna`, `gpt-6.1-terra`는 설정 가능한 모델 후보입니다. **공식 API 제공 여부, 본인 계정 접근 권한, 구조화 출력 지원 및 실제 요금은 별도 확인해야 합니다.** 기획서의 단가로 계산한 비용을 검증된 청구액으로 표시하지 않습니다. 연결이 안 되는 모델을 성공한 분석으로 꾸미거나 자동으로 다른 유료 모델을 호출하지 않습니다.

현재 기본 1차 분석 모델은 비교 전 기존 `gpt-4.1-mini`를 유지합니다. 모델 비교와 공식 경제 일정 등록, 정규화 입력 필드는 [입력·평가 형식](docs/EVENT_FORMAT.md)을 참고하세요. 재무부 HTML에서 공개 시각을 확인하지 못한 항목은 미확인 상태로 저장합니다.

## 주요 명령

```bash
# 연결 정보나 비밀번호를 출력하지 않는 환경 점검
python -m sessionquant doctor
python -m sessionquant db-check --db data/sessionquant-paper-local.db
python -m sessionquant db-stats --db data/sessionquant-paper-local.db

# 정규화한 과거 기록을 소수점 모의주문으로 재생
python -m sessionquant replay --input data/events.jsonl --universe data/sp500-universe.json --db data/replay.db

# 동일 입력으로 전략·구성 비교 보고서 생성
python -m sessionquant backtest --input data/events.jsonl --universe data/sp500-universe.json --report data/report.json

# 종전 PennyQuant 기록을 종전 규칙으로 연구하는 명시적 경로
python -m sessionquant legacy --help
```

현재 구성 종목 목록을 과거 전체에 적용하지 않습니다. 백테스트 날짜에 알려진 편입 이력이 없으면 해당 종목을 확인된 S&P 500 구성원으로 취급하지 않습니다. 과거 전략의 정수 주문 장부를 새 소수점 전략의 장부로 자동 변환하지도 않습니다.

## 문서

- [실행·구조·데이터 흐름](docs/SESSIONQUANT.md)
- [Phase 0~4 구현·검증 기록](docs/PHASES.md)
- [PennyQuant에서 이전하기](docs/MIGRATION.md)
- [로컬 MySQL 설정](docs/MYSQL_LOCAL.md)
- [DB 저장 항목과 용량](docs/DATABASE_STORAGE.md)
- [기존 버전 안내 보관본](docs/LEGACY_README.md)

실계좌 소수점 주문, 소액 실거래 검증, GCP 배포·원격 운영은 이번 범위에 포함하지 않습니다. 로컬 MySQL 접속과 본인 API 권한은 사용자 PC에서 마지막 연결 검사를 진행해야 합니다.

## Phase 5 첫 실거래 검증

[소수점 실주문 검증 매뉴얼](docs/PHASE5_LOCAL.md)을 먼저 읽으세요. 기본 명령은 실제 주문을 보내지 않습니다.

```powershell
.\venv\Scripts\python.exe -m sessionquant live-local --config config/live.local.yaml
```

사용자 계좌의 최소 금액·소수점 지원·현재 거래 제한은 이 개발 환경에서 검증하지 못했습니다. 실제 주문은 매 실행마다 명시적 활성화와 계좌 확인이 필요합니다. 일반 `run`을 실제 주문으로 바꾸거나 기존 정수 지정가 실거래 어댑터를 소수점 주문에 재사용하지 않습니다.

## 접속 정보를 한 번만 저장하기

이미 토스·MySQL 환경변수를 입력한 **같은 PyCharm PowerShell 터미널**에서 실행합니다.

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.lock
.\venv\Scripts\python.exe -m sessionquant setup-env --from-current
```

현재 설정된 토스·MySQL·Alpaca·OpenAI·FRED·SEC 접속 변수를 프로젝트 `.env`에 저장합니다. 값은 출력하지 않으며 Git과 Docker 이미지에서 제외합니다. 터미널·PyCharm을 다시 열어도 `python -m sessionquant ...`가 자동으로 읽습니다. 설정되지 않은 값을 입력하거나 변경하려면 `python -m sessionquant setup-env`를 실행하세요. API 키·DB 비밀번호 입력은 숨겨집니다.

이미 프로세스에 설정된 환경변수가 `.env`보다 우선합니다. 파일 변경 후에도 이전 값을 쓴다면 해당 터미널/PyCharm 실행 구성의 오래된 환경변수를 해제한 뒤 다시 실행하세요. `.env`는 로컬 평문 설정이므로 내용을 공유하거나 업로드하지 마세요. 실행할 때마다 실주문 활성화 플래그가 필요한 규칙은 유지됩니다.

## 내 실제 계좌 내역 보기

이전 $70 모의 화면 대신 **토스 API에서 조회한 실제 국내·미국 보유 주식, 평가손익, 원화·달러 매수 가능 금액, 미체결 주문**을 봅니다. `setup-env`로 접속 정보를 저장한 뒤 아래를 실행하세요.

```powershell
.\venv\Scripts\python.exe -m sessionquant run-account
```

브라우저에서 **http://localhost:8502**를 엽니다. 기존 메인 화면의 시장·종목·포트폴리오·뉴스·운영 탭을 유지하고 상단에 실제 계좌와 OpenAI 사용량·충전 기록을 표시합니다. 이 경로에는 매수·매도·취소 기능이 없으며 별도 MySQL 네임스페이스(`live_local_sessionquant-account`)에 현재 조회 결과를 저장합니다. 자동매매를 시작하는 명령이 아닙니다.

기본 10초 간격으로 조회하고 화면은 5초마다 새로 읽습니다. `Ctrl+C`로 종료합니다. 다른 토스 API 수집 프로그램과 동시에 실행하면 토큰 재발급이 기존 토큰을 무효화할 수 있으므로 먼저 기존 수집기를 종료하세요. 계좌가 여러 개면 `.env`의 `TOSS_ACCOUNT_SEQ`로 조회할 계좌를 선택합니다.

API의 매수 가능 금액은 현금 예수금·총자산과 다를 수 있습니다. 제공된 주식 평가금액과 통화별 매수 가능 금액을 분리 표시하고, 미제공·조회 실패 항목은 0으로 만들지 않습니다. 옵션·채권이나 API가 지원하지 않는 주문 유형은 이 화면 범위에 포함되지 않습니다.

## 대시보드에서 매매 시작·종료

운영 `run`은 항상 시작 대기로 실행됩니다. 시세·뉴스 수집과 기존 포지션의 손절·세션 청산 관리는 계속하지만 신규 매수는 **매매 시작**을 눌러야 허용합니다. `demo`와 과거 `replay`는 자동 재생을 유지합니다.

- 종목별 **시장가 매도 요청**: 확인 체크 후 엔진이 현재 잔고·미체결을 대사하여 전량 청산을 요청합니다. 실제 제어는 미국 주식만 지원하며 국내 보유는 조회합니다.
- **전체 매매 종료**: 신규/추가매수 중지, 미체결 매수 취소, 시스템 관리 포지션 전량 청산 순서로 처리합니다. 기존 외부 보유는 자동 청산 대상에 넣지 않고 개별 버튼으로 선택합니다.
- 종료 요청을 곧바로 청산 성공으로 표시하지 않습니다. 잔량과 미체결이 없어야 종료 상태가 됩니다. 브라우저만 닫아도 실행 중인 엔진은 계속되므로 종료 버튼의 상태를 확인하세요.

실계좌 제어 화면만 확인하려면 다음 명령을 사용합니다. 활성화하지 않은 실행에서는 실제 주문 버튼이 비활성화됩니다. `run-account`는 기존처럼 조회 전용입니다.

```powershell
.\venv\Scripts\python.exe -m sessionquant live-local --dashboard
```

실제 주문 권한을 매 실행 명시적으로 승인한 경우에는 [실거래 매뉴얼](docs/PHASE5_LOCAL.md)의 계좌 확인 플래그와 함께 실행합니다. 플래그가 있어도 신규 주문은 **매매 시작** 버튼 전에는 전송하지 않습니다. 이미 보유 중인 시스템 포지션의 손절·청산 의도 복구는 신규 진입과 독립적으로 계속합니다. Phase 5 첫 실험의 1종목·1회 신규 매수 제한은 유지됩니다.

정규장 16:00 폐장 예시: **13:30 신규/추가매수 마감 → 14:40 청산 시작 → 15:00 소수점 접수 마감**입니다. `min_entry_to_exit_minutes` 기본 30분 이상과 기존 진입 마감 중 더 이른 시각을 적용하고, 주문 전송 직전에도 확인합니다. 조기 폐장일은 거래소 달력에 맞춰 앞당깁니다. 이는 청산 직전 신규 진입을 막는 조건이며, 진입 직후 손절·악재·수동 매도까지 30분 동안 금지하는 조건은 아닙니다.

## 추가 뉴스·LLM 키 설정

기존 Alpaca·SEC·RSS에 OpenAI와 FRED를 추가하는 전체 절차는 [NEWS_SETUP.md](docs/NEWS_SETUP.md)에 있습니다. `setup-news`로 뉴스 접속 정보만 한 번 저장하고 `check-news`로 값 노출 없이 점검합니다. `check-news --connect`도 주문이나 LLM 텍스트 생성을 하지 않습니다. 새 엔진의 모델·예산은 `.env`의 OPENAI_MODEL이 아니라 `config/sessionquant.yaml`의 llm 블록을 사용합니다.


## 메인 화면의 실제 계좌·OpenAI 비용과 전체 연결 점검

기존 메인 화면에서 시세·뉴스·모의 전략과 실제 계좌를 함께 보려면 실행합니다.

```powershell
.\venv\Scripts\python.exe -m sessionquant run --config config/sessionquant.yaml
```

**http://localhost:8501**에서 확인합니다. 실제 보유 주식과 통화별 매수 가능 금액은 토스에서 조회하지만 이 명령의 전략 주문은 Paper입니다. 실제 보유와 전략 장부를 분리합니다. 계좌 조회만 하려면 위의 `run-account`를 사용하며, 별도 시세·뉴스 분석은 시작하지 않습니다. 두 토스 수집 명령을 동시에 실행하지 마세요.

OpenAI **프로그램 누적 비용 / 이번 달 비용 / 직접 기록한 충전 누계**를 메인에 표시합니다. 충전 누계는 화면의 **OpenAI 충전 누계 기록**을 펼쳐 입력합니다. 표준 API 키로 공식 잔액이나 충전 내역을 자동 조회했다고 표시하지 않습니다. 비용은 선택한 Paper 운영 DB의 설정된 토큰 단가를 사용한 추정치이며 다른 앱·조직 전체 비용은 포함하지 않습니다. 상세 내용은 [MAIN_DASHBOARD.md](docs/MAIN_DASHBOARD.md)를 보세요.

**OpenAI 키가 없거나 잔액이 부족해도 차트 시그널에 따른 매매와 손절·당일 청산은 계속합니다.** 잔액·인증·요청 제한을 구분해 경고하고, 실패한 뉴스는 UNKNOWN으로 남깁니다. 이전에 확인된 악재·경제 일정·시장 및 위험 제한은 유지합니다. 충전 누계 미입력 또는 $0 자체는 거래 중단 조건이 아닙니다.

모든 수집 프로그램을 종료한 뒤 전체 연결을 점검합니다.

```powershell
.\venv\Scripts\python.exe -m sessionquant check-connections
```

토스 시세·실계좌, MySQL, OpenAI 모델 접근, Alpaca·SEC·RSS·공식 기관·Treasury·GDELT·FRED를 검사합니다. 주문이나 LLM 텍스트 생성은 하지 않습니다. 모델 조회 성공은 잔액·실제 생성 성공의 증거가 아닙니다. `--offline`을 붙이면 저장된 설정 여부만 확인합니다. 미등록 RSS는 `not_configured`, 실행 중 토스 엔진을 감지하면 토큰 충돌을 피하려고 `skipped_active_collector`로 표시합니다. 종료 직후에는 마지막 기록이 만료되는 60초 뒤 다시 검사하세요.


## 기업 RSS 기본 등록

[검증된 공식 기업 RSS 목록](docs/COMPANY_RSS.md)을 업데이트 후 자동으로 읽습니다. **RSS 등록 범위는 확인 당시 S&P 500 내 시가총액 상위 100개 기업**이며, 그중 현재 수집할 수 있는 공식 피드를 포함합니다. S&P 500 전체 편입 원장은 유지하며, 단계 01부터 신규 감시는 별도의 검증된 100→20 정책을 따릅니다. 시총 순위 확인 시점·선정 방식과 제외 사유를 문서에 기록했습니다.

기존 `config/sessionquant.yaml`의 사용자 기업별 필드가 기본 목록보다 우선합니다. 전체 기본 목록을 끄려면 `company_rss_catalog: false`, 개별 RSS를 끄려면 해당 기업에 `rss_urls: []`를 지정합니다. 순위는 검증 당시 스냅숏이며 운영 중 자동 변경하지 않습니다. 등록된 전체 연결을 확인하려면 실행합니다.

```powershell
.\venv\Scripts\python.exe -m sessionquant check-news --connect --provider rss
```

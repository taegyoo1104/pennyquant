# SessionQuant 2.1

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

브라우저에서 **http://localhost:8502**를 엽니다. 이 경로에는 매수·매도·취소 기능이 없으며 별도 MySQL 네임스페이스(`live_local_sessionquant-account`)에 현재 조회 결과를 저장합니다. 자동매매를 시작하는 명령이 아닙니다.

기본 10초 간격으로 조회하고 화면은 5초마다 새로 읽습니다. `Ctrl+C`로 종료합니다. 다른 토스 API 수집 프로그램과 동시에 실행하면 토큰 재발급이 기존 토큰을 무효화할 수 있으므로 먼저 기존 수집기를 종료하세요. 계좌가 여러 개면 `.env`의 `TOSS_ACCOUNT_SEQ`로 조회할 계좌를 선택합니다.

API의 매수 가능 금액은 현금 예수금·총자산과 다를 수 있습니다. 제공된 주식 평가금액과 통화별 매수 가능 금액을 분리 표시하고, 미제공·조회 실패 항목은 0으로 만들지 않습니다. 옵션·채권이나 API가 지원하지 않는 주문 유형은 이 화면 범위에 포함되지 않습니다.

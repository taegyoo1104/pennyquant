# 처음 설치하고 실행하기

이 프로젝트는 **내가 정한 전략이 코드대로 판단하고, LLM 분석을 참고하며, 모의 주문과 체결을 기록하는지** 확인하는 프로그램입니다. 처음에는 API 없는 데모를 보고, 그다음 실제 시세를 연결하세요. 실제 시세를 사용해도 이 문서의 주문은 모두 가상 계좌의 모의매매입니다.

전체 흐름은 [매매 과정](TRADING_PROCESS.md), 용어는 [용어 안내](GLOSSARY.md), 만든 이유는 [프로젝트 이야기](PROJECT_STORY.md)를 참고하세요. 이미 설치했다면 [일상 운영](REALTIME_PAPER.md), GCP에서 실행한다면 [GCP 운영](GCP_PAPER.md)으로 이동하면 됩니다.

## 1. 프로젝트와 Python 준비

Python 3.12 이상과 Git을 설치합니다. 처음 내려받을 때는 다음을 실행합니다.

```powershell
git clone --branch main https://github.com/taegyoo1104/pennyquant.git
cd pennyquant
```

이미 사용 중인 프로젝트는 프로그램을 종료하고 `git status --short --branch`로 작업 중인 파일과 브랜치를 먼저 확인하세요. 수정 파일을 보존한 뒤 `main`의 검증된 변경을 반영합니다. GCP 운영 서버는 일반 `git pull` 대신 [고정 커밋 업데이트 절차](GCP_PAPER.md#장전-업데이트)를 사용합니다.

Windows PowerShell에서 프로젝트 전용 가상환경을 만듭니다. 이미 PyCharm의 `venv` 또는 `.venv`를 사용한다면 그 환경을 사용하세요.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps -e .
python -m sessionquant doctor
```

가상환경 활성화가 제한된 PC에서는 아래 명령의 `python`을 `.\.venv\Scripts\python.exe`로 바꾸면 됩니다. macOS·Linux는 `python3.12 -m venv .venv`와 `source .venv/bin/activate`를 사용합니다.

## 2. API 키 없이 전략 데모 보기

```powershell
python -m sessionquant strategy-lab --out data/strategy-demo-01
```

완료하면 `data/strategy-demo-01/report.html`을 브라우저로 엽니다. 조건 충족 후 매수·청산, 재시작 복구, 오래된 호가 차단, 반등이 없을 때 대기, LLM이 없거나 실패할 때의 동작을 확인할 수 있습니다.

이 데모는 **합성 시세와 가상 LLM 응답**을 사용합니다. 실제 API 호출·LLM 과금·실제 주문은 없습니다. 다시 실행할 때는 `strategy-demo-02`처럼 새 폴더를 지정합니다.

같은 데모의 저장된 결과를 대시보드로 보려면 다음을 실행합니다.

```powershell
python -m sessionquant dashboard --backend sqlite --db data/strategy-demo-01/entry_exit.db
```

**http://localhost:8501**에 접속합니다. `dashboard`는 저장된 기록을 보여주는 명령이며, 실제 시세를 수집하는 엔진을 시작하지 않습니다. 화면을 확인한 뒤 터미널에서 `Ctrl+C`로 닫습니다.

## 3. 실제 데이터 연결 준비

| 준비 항목 | 쓰는 곳 |
| --- | --- |
| 토스 API Client ID·Secret, 허용 IP | 웹소켓 호가와 REST 분봉·거래대금 조회 |
| MySQL 8 | 시세·뉴스·모의매매 기록 저장. [로컬 MySQL 설정](reference/MYSQL_LOCAL.md) 참고 |
| OpenAI API 키와 사용 가능한 크레딧 | 뉴스 분석·전략 설명. [뉴스·AI 설정](reference/NEWS_SETUP.md) 참고 |
| SEC 연락처 식별자, 사용할 RSS 등 | 실제 기업 뉴스·공시 수집 |
| Alpaca·FRED 등 선택 설정 | 해당 공급자를 사용할 때만 준비 |

접속 정보는 환경변수 또는 로컬 `.env`에 저장합니다. 키·비밀번호를 Git에 올리지 않습니다.

```powershell
python -m sessionquant setup-env
```

이미 현재 터미널에 접속 정보를 등록했다면 `setup-env --from-current`로 저장할 수 있습니다. 프로세스 환경변수가 `.env`보다 우선하므로 변경이 반영되지 않으면 PyCharm 실행 설정과 터미널 환경변수를 함께 확인하세요.

**현재 권장 설정은 `strategy-lab-websocket`입니다.** 명령의 기본값에 맡기지 말고 설정 파일을 명시합니다. 기존 장부와 섞이지 않도록 다음 실행 공간도 사용합니다.

```powershell
$env:SQ_DB_NAMESPACE="sessionquant-strategy-lab-ws-v1"
python -m sessionquant db-check --db data/sessionquant-strategy-lab-ws-v1.db
python -m sessionquant check-toss --symbol AAPL
python -m sessionquant universe-refresh --output data/sp500-universe.json
```

Bash에서는 `export SQ_DB_NAMESPACE=sessionquant-strategy-lab-ws-v1`을 사용합니다. 상태 확인 명령도 같은 환경변수와 설정 파일을 사용해야 같은 장부를 읽습니다. `db_path`의 `.db` 이름은 MySQL에서도 실행 공간을 구분하는 데 쓰이며, 이 프로필의 저장소는 MySQL입니다.

연결 검사는 기존 엔진과 별도 토스 수집기를 중지한 상태에서 실행합니다. `check-toss` 성공은 인증·현재가 읽기 성공이며 웹소켓 연결, 최신 호가, 매매 준비 완료까지 보증하지 않습니다. 위 명령은 실제 주문이나 LLM 생성 요청을 하지 않습니다.

**이미 다른 프로필로 운영했다면** 보유·미체결을 정리하고 기존 장부를 백업한 뒤 전환하세요. 실행 공간 이름만 바꾸면 기존 보유가 화면에서 사라져도 실제로 청산된 것이 아닙니다. GCP의 기존 장부 전환은 [전용 전환 스크립트](WEBSOCKET_ROLLOUT.md#gcp에서-전환)를 사용합니다.

## 4. 실제 시세로 모의매매 실행

```powershell
python -m sessionquant run --config config/sessionquant.strategy-lab-websocket.example.yaml
```

**http://localhost:8501**에서 `모의매매` 표시와 자료 준비 상태를 확인합니다. 실행 직후에는 목록·당일 분봉·장부 복원에 시간이 걸릴 수 있습니다. 준비가 끝나면 **매매 시작**을 열고 확인란을 선택해 시작합니다. 시작을 눌러도 전략·거래 시간·위험 조건이 맞아야 모의 주문이 생깁니다.

권장 프로필은 시총 후보 최대 50개 중 거래대금으로 최대 10개를 집중 감시하고, 정규장 개장 기준 30분마다 목록을 재선정합니다. 전략은 **상승 추세 → 1~4개 조정봉 → 확정봉 반등**입니다. 자세한 판단 기준은 [매매 과정](TRADING_PROCESS.md)을 참고하세요.

PyCharm Run 설정은 Module name `sessionquant`, Parameters `run --config config/sessionquant.strategy-lab-websocket.example.yaml`, 작업 폴더는 프로젝트 최상위, 인터프리터는 설치한 가상환경입니다. 환경변수에도 위 `SQ_DB_NAMESPACE`를 맞춥니다.

## 5. 중지와 다음 실행

- 브라우저만 닫으면 실행 중인 엔진은 계속 동작합니다.
- 화면의 **전체 매도·매매 종료**는 신규 매수를 막고 보유를 정리하는 요청입니다. 주문·체결·잔량을 확인해야 완료 여부를 알 수 있습니다.
- 로컬 터미널의 `Ctrl+C`는 프로그램 종료입니다. 보유를 자동으로 모두 팔았다는 뜻은 아닙니다.
- 같은 설정과 실행 공간으로 다시 켜면 저장 상태를 복원하고 자료를 확인합니다. 신규 매수는 시작 대기이며, 프로그램이 꺼져 있는 동안에는 시세 감시와 손절을 처리하지 못합니다.

다음부터 볼 화면과 오류별 점검 방법은 [일상 운영](REALTIME_PAPER.md)에 정리했습니다.

# 처음 설치하고 실행하기

프로젝트 개요는 [README](../README.md), 장중 사용과 결과 확인은 [운영 안내](REALTIME_PAPER.md)를 참고하세요. 이 문서는 현재의 **실제 데이터 + 모의매매** 설정을 기준으로 합니다.

## 1. 프로젝트와 Python 준비

Python 3.12 이상을 설치하고 PyCharm에서 `pyproject.toml`이 있는 프로젝트 폴더를 엽니다. 현재 개발 작업은 `wip/stage06-handoff-20261115` 브랜치에서 진행합니다.

처음 내려받는 경우:

```powershell
git clone --branch wip/stage06-handoff-20261115 https://github.com/taegyoo1104/pennyquant.git
cd pennyquant
```

이미 프로젝트를 사용하고 있다면 프로그램을 종료하고 현재 브랜치와 수정 파일을 먼저 확인합니다. 작업 중인 파일은 보존한 뒤 업데이트하세요.

```powershell
git status --short --branch
git switch wip/stage06-handoff-20261115
git pull --ff-only origin wip/stage06-handoff-20261115
```

PyCharm에서 프로젝트 전용 가상환경을 선택합니다. 이미 `venv` 또는 `.venv`를 사용 중이면 그대로 사용합니다. 가상환경이 없는 Windows 환경에서는 다음과 같이 만들 수 있습니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m sessionquant doctor
```

아래의 `python`은 이 가상환경의 Python을 뜻합니다. 터미널이 다른 Python을 사용한다면 `.\.venv\Scripts\python.exe` 또는 본인의 `venv` 경로로 바꿔 실행하세요. macOS·Linux에서는 `python3.12 -m venv .venv`로 만든 뒤 `source .venv/bin/activate`를 사용합니다.

## 2. API 키 없이 데모 보기

```powershell
python -m sessionquant demo --out data/session-demo
python -m sessionquant dashboard --backend sqlite --db data/session-demo/sessionquant-demo-session-demo.db
```

**http://localhost:8501**에서 화면을 확인합니다. 이 데모는 만들어 둔 가짜 시세로 동작하며 실제 시장 성과가 아닙니다. 다시 실행할 때는 `session-demo-2`처럼 새 출력 폴더를 사용하고 DB 경로도 맞춰 주세요.

## 3. 실제 데이터 연결 준비

| 준비 항목 | 용도 | 자세한 안내 |
| --- | --- | --- |
| 토스 API Client ID·Secret | 시세와 분봉 조회 | `setup-env`, `check-toss` 명령 |
| 로컬 MySQL 8 | 시세·뉴스·매매 기록 저장 | [MySQL 설정](reference/MYSQL_LOCAL.md) |
| OpenAI API 키·사용 가능한 크레딧 | 뉴스 본문 분석 | [뉴스·AI 설정](reference/NEWS_SETUP.md) |
| SEC 연락처 식별자 | 기업 공시 수집 | [뉴스·AI 설정](reference/NEWS_SETUP.md) |
| Alpaca·FRED 설정 | 선택적인 뉴스·경제 참고 자료 | [뉴스·AI 설정](reference/NEWS_SETUP.md) |

키와 DB 접속 정보는 본인 PC의 환경변수 또는 로컬 `.env`에 저장합니다. 키·비밀번호를 Git에 올리지 않습니다. 이미 환경변수를 등록했다면 같은 터미널에서 저장할 수 있습니다.

```powershell
python -m sessionquant setup-env --from-current
```

처음 입력하거나 변경하려면 `python -m sessionquant setup-env`를 사용합니다. 기존 프로세스 환경변수가 `.env`보다 우선하므로, 바꾼 값이 반영되지 않으면 PyCharm의 실행 설정과 터미널 환경변수도 확인하세요.

다른 수집 프로그램을 중지한 상태에서 연결과 S&P 500 목록을 준비합니다.

```powershell
python -m sessionquant db-check --db data/sessionquant-realtime-paper-v1.db
python -m sessionquant check-toss --symbol AAPL
python -m sessionquant universe-refresh --output data/sp500-universe.json
```

위 명령은 실제 주문을 보내지 않습니다. 뉴스의 유료 분석은 API 접근 확인과 별개이며, 운영 시에는 설정된 호출·비용 한도를 적용합니다.

## 4. 실제 데이터로 모의매매 실행

```powershell
python -m sessionquant run --config config/sessionquant.realtime-paper.example.yaml
```

처음 실행하면 시총 후보를 수집하고 과거·당일 분봉을 준비하므로 시간이 걸릴 수 있습니다. 다음 실행부터는 저장된 자료와 완료 검사 결과를 재사용합니다. **기존 설정에서 수집해 둔 분봉이 있다면** [운영 안내의 일회 복사 절차](REALTIME_PAPER.md#1-기존-분봉을-재사용해-준비)를 먼저 확인하세요. 새로 시작하는 사람에게 복사할 기존 DB가 필요한 것은 아닙니다.

**http://localhost:8501**에서 모의매매 화면인지 확인하고, 자료 준비가 끝나면 **매매 시작**을 누릅니다. 조건이 충족되지 않으면 거래가 없어도 정상입니다. 주말에는 시장 거래 없이 저장 자료와 뉴스 수집·분석 상태를 확인할 수 있습니다.

PyCharm의 Run 버튼을 사용하려면 Python 실행 구성에서 다음을 지정합니다.

| 항목 | 값 |
| --- | --- |
| 실행 방식 | Module name |
| 모듈 | `sessionquant` |
| 매개변수 | `run --config config/sessionquant.realtime-paper.example.yaml` |
| 작업 폴더 | 프로젝트 최상위 폴더 |
| 인터프리터 | 설치에 사용한 가상환경 |

## 5. 중지와 다시 시작

브라우저를 닫아도 엔진은 계속 실행됩니다. 모의 보유 종목을 정리하려면 화면의 일괄 종료·청산 요청 후 잔량과 미체결 상태를 확인합니다. 터미널의 `Ctrl+C` 또는 PyCharm의 중지는 프로그램 종료이며, 포지션 청산 완료와는 다릅니다.

다시 실행하면 저장된 상태와 누락 자료를 확인하고 신규 매수는 시작 대기로 돌아갑니다. 프로그램이 꺼져 있는 동안에는 시세 감시와 손절을 실행할 수 없습니다. 상세 상태 조회와 장중 테스트 기록을 남기는 방법은 [운영 안내](REALTIME_PAPER.md)에 정리했습니다.

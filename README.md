# PennyQuant · Python MVP

미국 저가주의 급등·급락·거래량 후보 탐지 → SEC 공시·기업 RSS 확인 → LLM 사실 분석 → 네 매매 전략 → 비용·위험 관리로 연결하는 프로젝트입니다. Python 3.12 이상으로 Windows, Linux, macOS의 로컬 PC에서 실행합니다. 대시보드는 한국어이며 시간은 KST로 표시하고, 거래 세션은 미국 동부시간과 XNYS 휴장·단축장을 사용합니다.

**기본값은 모의매매입니다.** `run-system`에 명시적인 실거래 옵션을 주고 계정·시장 입력 검증을 통과한 경우에만 새 토스 주문 어댑터가 실제 요청을 보냅니다. 실계좌·실제 체결은 이 개발 환경에서 검증하지 않았습니다. API 키 없이 실행하는 데모와 보고서는 합성 데이터로 만든 기능 검증입니다.

## 통합 시스템 실행

PyCharm에서 Ctrl+T로 받은 뒤 PowerShell에서 실행합니다. 아래는 기존 가상환경 폴더 이름이 `venv`인 경우입니다.

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.lock
.\venv\Scripts\python.exe -m pip install --no-deps -e .
.\venv\Scripts\python.exe -m pennyquant run-system --pipeline-config config/pipeline.example.yaml
```

`http://localhost:8501`에서 후보·SEC/RSS·AI 분석·Alpaca 보조 뉴스·주문·위험 알림을 확인합니다. 최대 20개 후보, 보유 및 미체결 매수 합산 최대 5개 종목이며 조건 미충족 시 거래하지 않습니다. 기본 일일 손실 한도는 전략 배정 금액의 2%입니다. 한도 도달 후에는 신규 진입을 차단하고 보유 종목의 청산 관리를 계속합니다.

`TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `SEC_USER_AGENT`, `OPENAI_API_KEY`를 환경변수로 설정합니다. Alpaca 키는 무료 REST 보조 자료에 선택적으로 사용합니다. `.env` 자동 로딩은 없습니다. **토스 단독 관측 데이터에서 확인되지 않는 거래 가능 상태·RVOL 등을 임의로 만들지 않으므로, 완전한 시장 입력이 없으면 후보는 관찰 상태입니다.** [통합 실행 안내](docs/SYSTEM_RUN.md)와 [비용·위험 설정](docs/FEES_AND_RISK.md)을 참고하세요.

키 없이 화면과 연결을 먼저 확인하려면 다음 합성 데모를 사용합니다. 실제 뉴스나 LLM을 호출하지 않습니다.

```powershell
.\venv\Scripts\python.exe -m pennyquant demo-system --out data/system-demo
.\venv\Scripts\python.exe -m streamlit run pennyquant/dashboard/app.py -- --db data/system-demo/system-demo.db --mode system
```

## 빠른 실행

압축을 풀고 `pyproject.toml`, `README.md`, `main.py`가 보이는 프로젝트 루트에서 실행하세요. GitHub ZIP의 폴더 이름은 보통 `pennyquant-main`입니다. Python 3.12를 권장합니다.

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pennyquant doctor
.\.venv\Scripts\python.exe -m pennyquant demo --out data
.\.venv\Scripts\python.exe -m streamlit run pennyquant/dashboard/app.py
```

`py` 명령이 없다면 첫 줄을 `& "C:\Python312\python.exe" -m venv .venv`처럼 본인의 Python 설치 경로로 바꾸세요. PyCharm에서 이미 `.venv`를 생성했다면 첫 줄은 생략합니다. 위 명령은 가상환경의 Python을 직접 사용하므로 `Activate.ps1` 실행이나 PowerShell 실행 정책 변경이 필요하지 않습니다. [Windows·PyCharm 설정 안내](docs/WINDOWS_PYCHARM.md)를 참고하세요.

Linux/macOS 또는 WSL 터미널:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
python -m pennyquant doctor
python -m pennyquant demo --out data
python -m streamlit run pennyquant/dashboard/app.py
```

브라우저에서 `http://localhost:8501`을 엽니다. `demo`는 열흘의 가상 세션을 처리해 DB·테이프·네 전략 보고서를 생성합니다. 이미 `data/pennyquant.db`가 있으면 덮어쓰지 않습니다. 재실행은 `--out data/new-demo`처럼 새 디렉터리를 지정하고 대시보드에 해당 경로를 넘기세요.

```bash
python -m streamlit run pennyquant/dashboard/app.py -- --db data/new-demo/pennyquant.db --report data/new-demo/report.json
```

Windows에서는 이후 예시의 `python`을 `.\.venv\Scripts\python.exe`로 바꾸거나 PyCharm에서 프로젝트 `.venv` 인터프리터를 사용하세요. Linux/macOS에서는 위처럼 활성화한 가상환경의 `python`을 사용합니다.

간단한 개발 설치는 `python -m pip install -e '.[dev]'`도 가능합니다. `requirements.lock`은 이번 검증에 사용한 버전과 해시를 고정합니다. 검증 기록은 `docs/VALIDATION.md`에 있습니다. 이번 실행 환경은 Linux이며 실제 Windows OS에서의 실행 검증은 아직 수행하지 않았습니다.

## 토스만 연결해서 실행

토스 인증과 현재가 조회를 확인했다면 `run-toss`로 현재가 수집과 대시보드를 함께 실행할 수 있습니다. Alpaca·SEC·RSS 설정 없이 토스의 실제 현재가를 일정 간격으로 조회합니다. 이 모드는 **시세 모니터링 전용**이며, 뉴스 기반 전략의 자동 진입이나 모의주문을 실행하지 않습니다.

PyCharm에서 **Ctrl+T**로 최신 코드를 받은 뒤 프로젝트 루트의 PowerShell 터미널에서 실행합니다. 아래는 가상환경 폴더 이름이 `venv`인 경우입니다. `.venv`로 만들었다면 경로의 `venv`를 `.venv`로 바꾸세요.

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.lock
.\venv\Scripts\python.exe -m pip install --no-deps -e .
.\venv\Scripts\python.exe -m pennyquant run-toss --symbols AAPL,MSFT
```

브라우저에서 **http://localhost:8501**을 엽니다. 현재가, 제공 시각, 수신 시각, 조회한 가격의 추이를 확인할 수 있습니다. 종료는 실행 중인 터미널에서 **Ctrl+C**를 누르면 수집기와 대시보드가 함께 종료됩니다.

`TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`이 실행 환경에 있어야 합니다. Windows 사용자 환경변수로 저장했다면 PyCharm을 완전히 종료한 뒤 다시 실행해 새 값을 반영하세요. 기본 DB는 데모와 분리한 `data/toss-monitor.db`이며, 시세 모드에서는 계좌 잔고나 손익을 만들지 않습니다. 상세 설정과 오류 확인은 [토스만 연결해 실행하기](docs/TOSS_RUN.md)에 있습니다.

## 구현 범위

| 영역 | 제공 내용 | 확인할 제한 |
|---|---|---|
| 시세·뉴스 수집 | 토스 REST/WS, Alpaca REST/WS, SEC, RSS/Atom | 실제 키·권한 미검증. 수집 출력만으로 완전한 매매 입력은 되지 않음 |
| 토스 시세 모니터링 | 현재가 반복 조회·제공/수신 시각·조회 가격 추이·대시보드 동시 실행 | 시세 감시 전용. 뉴스·완전한 시장 입력이 없으면 자동 진입하지 않음 |
| 스캐너 | 가격·상승률·동시간 RVOL·거래대금·스프레드·중단·희석·기업행동 필터, 후보 20개 | 미확인 데이터로 진입하지 않음 |
| 뉴스 | SEC 티커→CIK 자동 조회·본문 수집, 기업 RSS, LLM 구조화 분석·원문 근거 검증·캐시 | LLM 키·자료 부족·실패는 UNKNOWN, Alpaca 지연 뉴스는 단독 진입 근거로 사용하지 않음 |
| 전략 | 모멘텀, 눌림목, 갭 돌파, VWAP 회복 | 실험 규칙이며 최적화·수익성 검증을 수행하지 않음 |
| 백테스트 | 이벤트 수신 순서, 다음 호가 체결, 정수 수량, 비용·호가 수량, 시계열 구간 분리 | 호가 대기순서·전체 깊이·정확한 거래소 매칭은 재현하지 않음 |
| 모의주문 | 위험 예산, 지정가, 부분 체결·취소·만료, 손절·익절, 중복 방지·복구·대사 | 손절·익절은 로컬 합성 지정가 주문. 갭에서 미체결 가능 |
| 대시보드 | 시세·뉴스·포지션·주문·전략 성과·상태·신규 진입 중지 | 화면의 중지 해제는 다른 위험 제한을 해제하지 않음 |
| 실거래 | 별도 무장 옵션, 토스 지정가 주문·취소·실제 체결 대사·재시작 저널 | 실계정 미검증, 불명 주문은 재전송 금지·정지. 로컬 손절은 프로그램 중단 시 작동하지 않음 |

`demo-system`은 통합 시스템의 합성 검증 경로이고, `run-system`은 실제 후보·뉴스 공급처와 전략을 연결합니다. 거래에는 신뢰할 수 있는 거래 가능 상태, 비교 가능한 과거 동시간 거래량, 당일 누적 지표가 포함된 완전한 시장 스트림이 필요합니다. 새 primary 정책은 검증된 SEC·기업 RSS 근거를 사용하므로 Alpaca 유료 실시간 뉴스 권한이 필수는 아닙니다. 부족한 시장 필드를 임의의 값으로 채우지 않습니다.

## 실행 명령

```bash
# 토스만 연결한 시세 수집과 대시보드
python -m pennyquant run-toss --symbols AAPL,MSFT

# 설정 변경: 전역 옵션은 하위 명령 앞에 지정
python -m pennyquant --config pennyquant/config/strategy.yaml demo --out data/configured

# 사용자 기록 데이터 비교
python -m pennyquant backtest --input path/to/events.jsonl --report data/custom-report.json

# 기록을 모의주문으로 실행. 과거 수신 시각을 재생하는 연구 모드
python -m pennyquant replay --input path/to/events.jsonl --db data/replay.db --strategy pullback
python -m pennyquant replay --input path/to/events.jsonl --db data/replay.db --strategy pullback --resume

# 외부 생산자가 줄 단위로 추가하는 완전한 실시간 이벤트 처리
python -m pennyquant paper --input data/live-events.jsonl --db data/live-paper.db --strategy pullback
```

`replay --resume`은 입력 파일 SHA-256·설정·전략이 같아야 합니다. `paper`는 현재 시각으로 데이터 신선도를 확인하며, 저장한 파일 오프셋·앞부분 해시와 주문 상태를 함께 복구합니다. `replay`와 `paper`는 각각 새 DB를 사용하세요. DB별 엔진 작성자는 하나만 실행할 수 있습니다.

## API 데이터 수집

키는 `.env.example`의 이름을 참고해 환경변수 또는 비밀 저장소로 주입합니다. 파일을 자동으로 읽지 않으며, 키를 소스나 채팅에 넣지 마세요. 실제 계정에 연결하는 명령은 사용자가 자신의 환경에서 실행합니다.

토스 Client ID·Client Secret을 발급받았다면 먼저 `python -m pennyquant check-toss --symbol AAPL`로 인증과 현재가 조회를 확인하세요. 토큰·키를 출력하지 않는 읽기 전용 검사이며 주문이나 모의매매를 시작하지 않습니다. Windows의 대화식 키 입력과 PyCharm Run 설정은 [토스 연결 확인 안내](docs/TOSS_CONNECTION.md)에 있습니다.

```bash
# 토스: 시세 한 번 조회. TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 필요
python -m pennyquant collect toss --symbols AAPL --output data/toss-quotes.jsonl

# 토스 호가 WebSocket. 읽기 전용이며 거래 가능 상태는 미확인으로 남음
python -m pennyquant collect toss --symbols AAPL --stream --output data/toss-stream.jsonl

# Alpaca REST 뉴스: 보조 데이터로만 취급
python -m pennyquant collect alpaca --symbols AAPL --output data/alpaca-news.jsonl

# 실제 계정의 실시간 권한을 확인한 경우만 지정
python -m pennyquant collect alpaca --symbols AAPL --stream --realtime-verified --output data/alpaca-live.jsonl

# SEC: SEC_USER_AGENT에 프로젝트명과 연락 이메일 필요
python -m pennyquant collect sec --cik 320193 --output data/sec-news.jsonl

# RSS: 회사 티커를 추측하지 않는 보조 수집
python -m pennyquant collect rss --url 'https://YOUR_VERIFIED_FEED_URL' --output data/rss.jsonl
```

서로 다른 수집 파일을 단순히 이어 붙이면 수신 순서가 깨집니다. 단일 생산자가 수신 시각 순서로 병합하고, 완전한 snapshot과 완료된 1분봉을 만들어 `paper`에 공급해야 합니다. 토스 호가 캡처는 기본 `tradable=false`, `exchange=UNKNOWN`이고 RVOL도 제공하지 않으므로 그 파일 하나로 진입 신호가 생성되지 않습니다. 동시간 RVOL 계산 도구와 정확한 전략 규칙은 `docs/STRATEGIES.md`를 참고하세요.

## 자금·위험 설정

기본 자본은 예시 USD 70이며 환율 시세가 아닙니다. 거래당 위험 1%, 손절 5%, 익절 10%, 일일 손실 2%, 하루 신규 주문 2개, 동시 1종목으로 시작합니다. 수수료와 슬리피지는 각각 10bp의 예시이며 실제 토스 비용표를 대체하지 않습니다. 신규 주문이 접수되면 체결되지 않아도 일일 횟수를 소비합니다.

정수 수량은 현금·예약 현금·손절 거리·왕복 수수료·스프레드·슬리피지를 고려해 내림합니다. 손절은 보장 가격이 아니며 실제 손실이 예산을 넘을 수 있습니다. 휴장·시세 지연·뉴스 지연·미확인 주문·대사 실패·수동 중지에서는 신규 진입을 차단합니다. 보유 포지션은 유효한 시세가 들어올 때 계속 관리합니다.

## 파일과 문서

```text
pennyquant/
  config/       설정과 YAML
  collectors/   읽기 전용 제공자 어댑터
  scanners/     유동성 필터와 동시간 RVOL
  news/         이벤트 분류·중복 제거·시각 검증
  strategies/   네 가지 실험 전략
  trading/      위험·주문·포지션 상태
  backtest/     동일 이벤트 비교·성과 보고서
  database/     SQLite 저널·원자적 상태 저장
  dashboard/    Streamlit 한국어 화면
  engine.py     전략·모의체결 공통 루프
  runtime.py    기록 재생·실시간 파일 소비·복구
  capture.py    제공자별 정규화 캡처 CLI
tests/          전략·체결·장애·복구·화면 회귀 테스트
examples/       합성 테이프와 생성된 예시 보고서
docs/           설치·API·이벤트·운영·검증 문서
```

- [운영·배포 매뉴얼](docs/OPERATIONS.md)
- [Windows·PyCharm 실행 안내](docs/WINDOWS_PYCHARM.md)
- [정규화 이벤트 규격](docs/EVENT_FORMAT.md)
- [API 상태와 공식 근거](docs/API_STATUS.md)
- [토스 API 연결 확인](docs/TOSS_CONNECTION.md)
- [토스만 연결해 실행하기](docs/TOSS_RUN.md)
- [전략의 정확한 규칙](docs/STRATEGIES.md)
- [대시보드 사용법](docs/DASHBOARD.md)
- [검증 기록과 미완료 범위](docs/VALIDATION.md)

```bash
python -m pytest -q
ruff check .
```

성공 기준은 데이터·검증·위험 관리·복구가 확인되는 연구 환경입니다. 충분한 표본과 4주 이상의 전향적 실시간 모의매매 기록은 아직 없으므로 실거래 승인은 보류 상태입니다.

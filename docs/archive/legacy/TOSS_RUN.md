> **과거 기록** · 작성 당시의 기획·구현을 보관한 문서입니다. 현재 프로젝트는 [소개](../../../README.md)와 [시작 안내](../../GETTING_STARTED.md)를 먼저 읽어 주세요.

# 토스만 연결해 로컬에서 실행

`run-toss`는 토스에서 제공하는 실제 현재가를 반복 조회하고 Streamlit 대시보드를 함께 실행합니다. Alpaca 뉴스 API 키가 없어도 사용할 수 있습니다. 이 실행 모드는 **시세 모니터링 전용**입니다. 현재 뉴스 기반 전략은 검증된 뉴스와 완전한 시장 입력을 요구하므로 이 모드에서 자동 진입·모의주문을 실행하지 않습니다.

## Windows에서 시작

토스의 `check-toss` 검사로 인증과 현재가 조회가 되는지 확인한 상태를 기준으로 설명합니다. `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`은 사용자 PC의 환경변수에 있어야 합니다. Windows 사용자 환경변수로 저장했다면 **PyCharm을 완전히 종료한 뒤 다시 실행**합니다. 새 터미널이 저장된 값을 상속받아야 합니다.

1. GitHub에서 Clone한 프로젝트를 PyCharm으로 엽니다.
2. **Git → Update Project** 또는 **Ctrl+T**로 최신 코드를 받습니다.
3. 프로젝트 루트의 **PowerShell 터미널**에서 다음 명령을 실행합니다.

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.lock
.\venv\Scripts\python.exe -m pip install --no-deps -e .
.\venv\Scripts\python.exe -m pennyquant run-toss --symbols AAPL,MSFT
```

위 명령은 가상환경 폴더 이름이 `venv`인 경우입니다. `.venv`로 만들었다면 경로의 `venv`를 `.venv`로 바꿉니다. `pyproject.toml`, `README.md`가 보이는 폴더에서 실행하세요. 환경변수 설정은 [토스 연결 확인](../TOSS_CONNECTION.md)에 설명되어 있습니다.

브라우저에서 **http://localhost:8501**을 엽니다. 수집기는 계속 실행되므로 터미널 창을 유지합니다. 종료는 해당 터미널에서 **Ctrl+C**를 누르면 됩니다. 수집기와 이 명령이 시작한 대시보드가 함께 종료됩니다.

## 화면에서 확인할 내용

- 지정한 종목의 최근 토스 현재가와 통화
- 토스가 제공한 시세 시각과 PC가 수신한 시각
- 조회 시점별로 기록한 가격 추이
- 수집 연결 상태와 오류, 시각이 오래되었거나 미확인인 경우의 표시

가격 추이는 조회해서 관측한 현재가의 기록입니다. 전체 거래소의 체결 틱이나 OHLCV 봉을 복원한 차트가 아닙니다. 호가·거래량·거래 가능 상태처럼 현재 응답에 없는 값은 임의로 채우지 않습니다. 시세 모드에서는 계좌 잔고·손익을 만들지 않으며 매매 활성화·중지 제어를 사용하지 않습니다.

토스의 미국 시세는 일부 거래소 기반으로 **통합 최우선 호가(NBBO)가 아닙니다**. 장이 닫혔거나 해당 종목의 최근 거래가 없으면 제공 시각이 과거일 수 있습니다. 인증·HTTP 조회 성공은 시세의 신선도나 매매 준비 완료를 뜻하지 않습니다. 시세 제공 시각이 없으면 미확인 상태로 표시합니다.

## 실행 옵션

기본 종목은 `AAPL,MSFT`, 조회 간격은 3초, DB는 `data/toss-monitor.db`, 대시보드 포트는 8501입니다. 필요한 항목만 지정할 수 있습니다.

```powershell
# 애플 한 종목을 5초마다 조회
.\venv\Scripts\python.exe -m pennyquant run-toss --symbols AAPL --interval 5

# 8501 포트를 다른 프로그램이 쓰고 있을 때
.\venv\Scripts\python.exe -m pennyquant run-toss --symbols AAPL,MSFT --port 8502

# 별도 DB에 저장
.\venv\Scripts\python.exe -m pennyquant run-toss --symbols AAPL --db data/toss-monitor-2.db

# 대시보드 없이 수집만 실행
.\venv\Scripts\python.exe -m pennyquant run-toss --symbols AAPL --no-dashboard
```

`--port 8502`를 지정했다면 브라우저에서도 `http://localhost:8502`를 엽니다. 종목은 쉼표로 구분하며 최대 20개까지 지정합니다. 조회 간격은 API 요청 속도와 사용 조건에 맞춰 설정합니다.

## PyCharm Run 버튼으로 실행

**Run → Edit Configurations → + → Python**에서 다음처럼 설정할 수 있습니다.

| 항목 | 설정 |
|---|---|
| 실행 방식 | Module name |
| Module name | `pennyquant` |
| Parameters | `run-toss --symbols AAPL,MSFT` |
| Working directory | 현재 프로젝트 루트 |
| Interpreter | 프로젝트의 `venv\Scripts\python.exe` 또는 `.venv\Scripts\python.exe` |
| Environment variables | 저장된 사용자 환경변수를 상속받거나, 본인 PC에서 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` 설정 |

키를 소스·GitHub·채팅에 넣지 않습니다. Run 설정의 환경변수와 별도 터미널의 환경변수는 각각의 실행 환경에 적용됩니다. Python 3.12 기반 환경 설정은 [Windows·PyCharm 안내](WINDOWS_PYCHARM.md)를 참고하세요.

## DB와 중복 실행

기본 모니터링 DB는 데모의 `data/pennyquant.db`와 분리되어 있습니다. 기존 모의매매 엔진 상태가 들어 있는 DB는 시세 모니터링용으로 재사용하지 않습니다. 검사에서 거절되면 새로운 `--db` 경로를 지정하세요. DB별 수집 작성자는 한 개만 실행합니다.

이 실행은 한 토스 클라이언트와 캐시된 토큰을 재사용합니다. 실행 중 같은 Client ID로 `check-toss`나 다른 수집기를 별도로 시작하면 토큰 재발급이 기존 연결에 영향을 줄 수 있습니다. 다른 검사를 하려면 먼저 현재 실행을 Ctrl+C로 종료합니다.

## 오류 확인

- 환경변수 누락: PyCharm을 재시작했는지, 현재 실행 환경에 두 변수가 있는지 확인합니다.
- 인증·권한 오류: 토스 개발자 설정에서 키, 사용 권한, 허용 IP 조건을 확인합니다. 상태 코드만으로 원인을 단정하지 않습니다.
- 포트 사용 중: `--port 8502`처럼 다른 포트를 지정합니다. 기존 프로그램을 강제 종료할 필요는 없습니다.
- 기존 엔진 DB 거절: 시세 모니터링 전용의 새 DB를 지정합니다.
- 시세가 갱신되지 않음: 제공 시각과 수신 시각, 현재 시장 세션을 함께 확인합니다. 최근 거래가 없는 경우와 연결 오류를 구분합니다.

결과를 공유할 때는 화면에 표시된 상태나 정제된 오류 메시지를 확인하고, 시크릿·토큰·환경변수 값은 복사하지 마세요.

개발 환경의 검증은 모의 HTTP 응답과 Linux 실행을 기준으로 하며 사용자 계정의 실제 시세 권한과 Windows PC 실행은 별도 확인 단계입니다. 구체적인 결과와 제한은 [검증 기록](VALIDATION.md)에 있습니다.

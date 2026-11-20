> **과거 기록 · 현재 실행 지침 아님** · 아래 명령·설정·완료 상태는 작성 당시의 기록입니다. 현재 절차는 [프로젝트 소개](../../README.md)와 [문서 안내](../README.md)를 따르세요. [보관 문서 안내](README.md)

# 토스 API 연결 확인 — Windows 로컬

이 단계는 토스 인증과 현재가 조회가 되는지 확인합니다. 잔고·주문·조건주문을 조회하거나 주문을 전송하지 않습니다. 토스 Client ID와 Client Secret은 사용자 PC에서 입력합니다. 채팅·소스·GitHub에 값을 넣지 마세요.

## 최신 코드 받기

PyCharm에서 **Git → Update Project** 또는 **Ctrl+T**로 최신 변경을 받습니다. 프로젝트 루트에서 PowerShell 터미널을 엽니다. 기존 오류 경로의 가상환경 이름은 `venv`였습니다. 아래는 그 경로를 사용하며, 본인이 `.venv`로 만들었다면 명령의 `venv`를 `.venv`로 바꿉니다.

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.lock
.\venv\Scripts\python.exe -m pip install --no-deps -e .
```

## PowerShell에서 입력하고 검사

다음 명령은 실제 키 값을 명령 기록에 쓰지 않고 대화식으로 입력받습니다. Client Secret 입력은 화면에 표시되지 않습니다. 환경변수는 현재 터미널과 그 터미널에서 실행한 프로그램에 적용됩니다.

```powershell
$env:TOSS_CLIENT_ID = Read-Host "Toss Client ID"
$tossSecret = Read-Host "Toss Client Secret" -AsSecureString
$env:TOSS_CLIENT_SECRET = [System.Net.NetworkCredential]::new("", $tossSecret).Password
.\venv\Scripts\python.exe -m pennyquant check-toss --symbol AAPL
```

성공 여부와 조회한 현재가·제공 시각을 JSON으로 출력합니다. 액세스 토큰·클라이언트 시크릿은 출력하지 않습니다. 정상 종료 코드는 0이고 검사 실패는 2입니다. 토스 미국 시세는 일부 거래소 기반 데이터이며 NBBO가 아닙니다. 장이 닫혔거나 최근 거래가 없다면 현재가가 과거 시각일 수 있고, 시각이 제공되지 않으면 신선도를 확인할 수 없습니다. 인증·조회 성공만으로 데이터가 실시간이거나 모의매매에 충분하다고 판단하지 않습니다.

검사를 여러 번 실행하면 토큰이 재발급될 수 있습니다. 공식 API의 클라이언트별 토큰 정책에 따라 기존 토큰이 무효화될 수 있으므로 같은 Client ID로 실행하던 다른 수집 프로그램을 종료한 상태에서 확인합니다.

## PyCharm Run 설정으로 검사

1. **Run → Edit Configurations → + → Python**을 선택합니다.
2. 실행 방식을 **Module name**으로 지정하고 `pennyquant`를 입력합니다.
3. **Parameters**에 `check-toss --symbol AAPL`을 입력합니다.
4. **Working directory**는 프로젝트 루트, Interpreter는 프로젝트의 `venv\Scripts\python.exe`를 선택합니다.
5. **Environment variables**에 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`을 사용자 PC에서 입력하고 실행합니다.

Run 설정의 환경변수와 PowerShell에서 입력한 환경변수는 각각의 실행 환경에 적용됩니다. Run 설정에 넣은 값이 별도 터미널에 자동으로 전달되지는 않습니다. `.idea/`와 `.env`는 Git 추적 제외 대상이며, 이 프로그램은 `.env`를 자동으로 읽지 않습니다.

Windows 사용자 환경변수로 두 값을 저장했다면 PyCharm을 완전히 종료한 뒤 다시 실행해야 새 터미널과 Run 설정에서 상속받을 수 있습니다. 현재 터미널의 `$env:`에만 입력한 값은 해당 터미널과 그 자식 프로그램에서 사용합니다.

## 실패했을 때

- 환경변수 누락: 현재 명령을 실행하는 터미널 또는 Run 설정에 두 변수를 입력했는지 확인합니다.
- 인증 단계의 HTTP 401/403: 키·사용 권한·허용 IP 설정 등 토스 개발자 설정을 확인합니다. 상태 코드만으로 원인을 확정하지 않습니다.
- 시세 단계의 HTTP 401/403: 인증과 별도로 해당 데이터에 접근할 권한이 있는지 확인합니다.
- 연결 오류: PC 네트워크와 제공자 상태를 확인합니다.
- 현재가·제공 시각 미확인: 인증과 조회가 성공했더라도 해당 응답만으로 신선한 시세라고 보지 않습니다.

결과를 공유할 때는 이 명령의 출력만 확인하고, 키·환경변수 설정 화면·토큰을 복사하지 마세요.

## 다음 단계

토스만 연결한 상태로 대시보드를 실행하려면 다음 명령을 사용합니다.

```powershell
.\venv\Scripts\python.exe -m pennyquant run-toss --symbols AAPL,MSFT
```

이 명령은 현재가 반복 조회와 로컬 대시보드를 함께 시작합니다. 브라우저에서 `http://localhost:8501`을 열고, 종료할 때는 터미널에서 Ctrl+C를 누르세요. 자세한 사용법은 [토스만 연결해 실행하기](legacy/TOSS_RUN.md)에 있습니다. 실행 중에는 같은 Client ID로 `check-toss`를 별도로 실행하지 않습니다. 토큰 재발급이 기존 수집 연결에 영향을 줄 수 있습니다.

시세 연결 확인 후 실시간 호가·완료된 1분봉·관련 뉴스를 모의매매 엔진에 연결해야 합니다. 현재 전략에는 검증된 뉴스와 동시간 과거 거래량, 거래 가능 상태가 필요합니다. Alpaca 실시간 뉴스 권한이나 완전한 시장 입력이 없으면 신규 진입은 계속 차단됩니다. `check-toss` 자체는 자동매매나 모의매매를 시작하지 않습니다.

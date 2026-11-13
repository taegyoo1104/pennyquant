# Windows·PyCharm 로컬 실행

Windows PC의 Python 3.12와 PyCharm으로 실행하는 방법입니다. WSL2나 외부 서버 없이 실행할 수 있도록 파일 잠금을 운영체제별 구현을 사용하는 `filelock`으로 처리합니다. 이번 개발 환경은 Linux이며 실제 Windows OS에서의 실행 검증은 아직 수행하지 않았습니다.

## 프로젝트 열기

GitHub에서 최신 ZIP을 다운로드해 새 폴더에 압축을 풀거나, 기존 Git 복제본에서 최신 변경을 가져옵니다. PyCharm의 **Open** 또는 **File → Open**에서 `pyproject.toml`, `README.md`, `main.py`가 보이는 폴더를 선택합니다. GitHub ZIP의 기본 폴더 이름은 보통 `pennyquant-main`입니다.

## 인터프리터 선택

PyCharm의 Python Interpreter 설정에서 **Add Interpreter → Add Local Interpreter → Virtualenv**를 선택해 프로젝트 전용 가상환경을 만듭니다. PyCharm 버전에 따라 메뉴 이름은 조금 다를 수 있습니다.

| 항목 | 선택할 값 |
|---|---|
| Base interpreter | 본인 PC에 설치한 Python 3.12. 예: `C:\Python312\python.exe` |
| Environment location | 현재 프로젝트 폴더 안의 `.venv` |
| 프로젝트가 사용할 Interpreter | 생성된 `.venv\Scripts\python.exe` |

`C:\Python312\python.exe`는 가상환경을 만드는 기준 Python입니다. 가상환경을 만든 다음에는 해당 프로젝트의 `.venv\Scripts\python.exe`를 인터프리터로 사용합니다.

## 설치와 첫 실행

PyCharm 하단 **Terminal**을 열고 PowerShell을 사용합니다. 작업 위치는 프로젝트 루트여야 합니다. PyCharm이 이미 `.venv`를 만들었다면 아래 첫 줄은 생략하세요. Python 설치 위치가 다르면 첫 줄의 경로만 본인 경로로 바꿉니다.

```powershell
& "C:\Python312\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pennyquant doctor
.\.venv\Scripts\python.exe -m pennyquant demo --out data
.\.venv\Scripts\python.exe -m streamlit run pennyquant/dashboard/app.py
```

브라우저에서 **http://localhost:8501**을 엽니다. API 키 없이 합성 데이터의 주문·전략 비교·위험 관리 화면을 확인할 수 있습니다. 대시보드를 종료하려면 실행 중인 터미널에서 Ctrl+C를 누릅니다.

명령마다 가상환경의 Python 경로를 직접 지정하므로 `Activate.ps1`을 실행하거나 PowerShell 실행 정책을 변경할 필요가 없습니다. PyCharm 터미널을 이전에 WSL용으로 설정했다면 **Settings → Tools → Terminal → Shell path**에서 PowerShell로 바꾸고 새 터미널을 여세요.

## PyCharm의 Run 버튼 설정

토스 Client ID와 Client Secret을 발급받았다면 [토스 연결 확인](TOSS_CONNECTION.md)의 읽기 전용 `check-toss` 명령을 먼저 실행할 수 있습니다. 키는 사용자 PC의 환경변수에 입력합니다.

터미널 명령 대신 **Run → Edit Configurations**에서 Python 구성을 추가할 수도 있습니다. 인터프리터는 프로젝트 `.venv\Scripts\python.exe`, Working directory는 프로젝트 루트로 지정합니다.

| 구성 이름 | 실행 방식 | Module name | Parameters |
|---|---|---|---|
| Demo | Module name | `pennyquant` | `demo --out data` |
| Dashboard | Module name | `streamlit` | `run pennyquant/dashboard/app.py` |

처음에는 Demo를 한 번 실행하고, 완료되면 Dashboard를 실행해 브라우저를 엽니다. `main.py`를 인자 없이 실행하면 하위 명령 안내가 나오므로 위처럼 모듈과 매개변수를 지정하세요.

## 데모를 다시 실행할 때

이미 `data/pennyquant.db`가 있으면 데모는 기존 DB를 덮어쓰지 않습니다. 별도의 출력 폴더를 지정하고 대시보드도 그 폴더의 파일을 읽게 합니다.

```powershell
.\.venv\Scripts\python.exe -m pennyquant demo --out data/demo2
.\.venv\Scripts\python.exe -m streamlit run pennyquant/dashboard/app.py -- --db data/demo2/pennyquant.db --report data/demo2/report.json
```

`No module named filelock`이 나오면 최신 프로젝트 루트에서 잠금 파일 설치 명령과 편집 가능 설치 명령을 다시 실행하고 PyCharm 인터프리터가 같은 `.venv`를 가리키는지 확인하세요. 이전 ZIP의 `fcntl` 오류가 계속 나오면 현재 열어 둔 프로젝트가 최신 GitHub 버전인지 확인합니다.

## macOS·Linux·WSL을 사용하는 경우

같은 프로젝트를 사용할 수 있으며 POSIX 셸에서는 다음 순서로 실행합니다.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
python -m pennyquant demo --out data
python -m streamlit run pennyquant/dashboard/app.py
```

가상환경은 운영체제마다 새로 만듭니다. Windows의 `.venv`를 WSL이나 macOS·Linux에서 재사용하지 마세요.

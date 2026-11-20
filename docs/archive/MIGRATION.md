> **과거 기록 · 현재 실행 지침 아님** · 아래 명령·설정·완료 상태는 작성 당시의 기록입니다. 현재 절차는 [프로젝트 소개](../../README.md)와 [문서 안내](../README.md)를 따르세요. [보관 문서 안내](README.md)

# PennyQuant에서 SessionQuant로 이전

## 이름과 저장소

GitHub 원격 저장소 이름은 `taegyoo1104/pennyquant`로 유지합니다. PyCharm 프로젝트 폴더 이름을 바꾸지 않아도 됩니다. Python 프로젝트 이름과 패키지·실행 명령은 `sessionquant`입니다.

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.lock
.\venv\Scripts\python.exe -m pip install --no-deps -e .
.\venv\Scripts\python.exe -m sessionquant --help
```

기존 `python -m pennyquant` 실행 진입점은 호환 목적으로 남겨 두지만, 새 문서와 설정에서는 `python -m sessionquant`를 사용합니다. PyCharm Run Configuration의 모듈 이름도 `sessionquant`로 바꾸세요. 파일 경로로 실행하던 대시보드는 새 화면 `sessionquant/dashboard/session_app.py`를 선택합니다.

## 과거 기록 보존

프로젝트명을 바꾼다는 이유로 다음 자료를 수정하지 않습니다.

- 주문 ID, 체결 ID, 뉴스/이벤트 ID, 내용 해시.
- 이전 SQLite 파일과 정수 수량 장부.
- 과거 백테스트의 입력과 데이터 출처.
- 환경변수에 저장한 토스·Alpaca·OpenAI 자격증명.

새 SessionQuant 실행은 별도 DB·이름공간에서 시작합니다. 과거의 $70 페니스탁 장부에 $100 S&P 500 모의계좌를 덮어쓰지 않습니다. SQLite→MySQL의 **저장소 이전**과 과거 전략→신규 전략의 **거래 규칙 변경**은 다른 작업입니다. 저장소를 이전했다고 기존 포지션이 새 전략의 유효한 포지션이 되는 것은 아닙니다.

기존 데이터베이스를 옮길 때는 엔진을 종료하고 원본을 백업한 후 [MySQL 문서](../reference/MYSQL_LOCAL.md)의 마이그레이션 명령을 사용합니다. 새 대상이 비어 있는지 확인하며 원본 SQLite를 읽기 전용으로 취급합니다. 금융 자료를 삭제하거나 ID를 재발급하는 변환은 하지 않습니다.

## 기존 도구

과거 도구가 필요하면 `python -m sessionquant legacy --help`에서 명시적으로 선택합니다. 종전 명령의 설정 형식은 새 `config/sessionquant.yaml`과 다릅니다. [이전 README 보관본](legacy/LEGACY_README.md)은 과거 버전 이해를 위한 자료이며 새 시스템의 실행 순서를 대신하지 않습니다.

이전 Streamlit 화면은 다음 위치에 보존됩니다.

```powershell
.\venv\Scripts\python.exe -m streamlit run sessionquant/dashboard/app.py -- --db data/기존파일.db
```

기존 기록을 새 대시보드에서 읽더라도 부족한 시장 상태나 소수점 지원 정보를 만들어 내지 않습니다. 새 기능의 화면 검증에는 새 합성 데모를 사용하세요.

## 실제 주문과 GCP

새 Phase 0~4 명령은 Paper Trading만 지원합니다. 과거 실거래 어댑터의 코드가 남아 있다는 사실은 새 S&P 500 소수점 전략의 실제 주문 검증 완료를 뜻하지 않습니다. Phase 5에서 API의 금액 매수·수량 매도·주문 시간·잔량·취소·대사를 검증하고 명시적으로 활성화해야 합니다. GCP 이전은 그 후 Phase 6에서 진행합니다.

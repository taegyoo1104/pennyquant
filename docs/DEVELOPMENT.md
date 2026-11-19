# 개발 안내

코드를 수정하거나 검사를 실행할 때 보는 문서입니다. 사람이 기획하고 AI Codex를 활용해 구현·검증하는 프로젝트입니다. 처음 읽는다면 [프로젝트 소개](../README.md), [매매 흐름](TRADING_PROCESS.md), [용어 설명](GLOSSARY.md)을 먼저 보세요. 다른 안내는 [문서 목록](README.md)에서 찾을 수 있습니다.

## 실행 구성을 먼저 확인하기

현재 추천하는 실험은 **실제 시세·뉴스를 받아 매매만 모의로 하는 단순 눌림목 전략**입니다. 다음 파일을 명시적으로 선택합니다.

```sh
python -m sessionquant run --config config/sessionquant.strategy-lab-websocket.example.yaml
```

추천 설정과 프로그램의 기본값은 다릅니다. `run`에서 `--config`를 생략하면 `config/sessionquant.yaml`을 읽습니다. GCP Compose에서 `SQ_SESSION_CONFIG`를 지정하지 않으면 기존 `sessionquant.realtime-paper.example.yaml`을 사용합니다. 문서나 GitHub 코드를 바꾸는 것만으로 실행 중인 서버의 설정·Docker 이미지가 바뀌지는 않습니다. 배포 절차는 [GCP 안내](GCP_PAPER.md)를 따릅니다.

| 구분 | 추천 WebSocket 실험 프로필 | 기존 REST 프로필 |
| --- | --- | --- |
| 설정 | [strategy-lab-websocket](../config/sessionquant.strategy-lab-websocket.example.yaml) | [realtime-paper](../config/sessionquant.realtime-paper.example.yaml) |
| 전략 | `pullback_simple`: 상승 → 짧은 조정 → 확정봉 반등 | `pullback`: 기존 거래량·시장 조건 포함 |
| 종목 선정 | 확인 가능한 시총 기준 최대 50개 → 거래대금 중심 집중 10개, 정규장 기준 30분마다 재선정 | 과거 분봉 준비 여부로 운영 종목을 줄이고 집중 선정 |
| 시세·분봉 | WebSocket 호가와 REST 분봉·순위 등 보조 조회 | REST 순차·제한적 병렬 조회 |
| 필수 자료 | 당일 분봉·최신 호가·모의 거래 조건 | 과거 20일 비교 이력·RVOL 및 설정된 시장 필터도 확인 |
| 뉴스·LLM | 후보 순위 가점과 별도 설명. 뉴스가 직접 주문·청산하지 않음 | 검증된 악재·경제 일정 등의 진입 제한도 사용 |
| 초기 모의 자금 | 예시 $100, 권장 2종목. 실제 계좌 잔액과 별개 | 설정 파일의 값 사용 |

단순 전략도 충분한 당일 분봉을 모아야 합니다. 과거 20일 RVOL을 필수에서 뺐다는 것이 개장 즉시 주문하거나 최신성·손실 한도를 생략한다는 뜻은 아닙니다. REST로 같은 단순 전략을 비교하려면 [strategy-lab](../config/sessionquant.strategy-lab.example.yaml)을 사용합니다. 다른 전략은 설정으로 하나를 선택하며, 여러 전략을 동시에 실행하는 완성된 포트폴리오로 설명하지 않습니다.

## 코드의 흐름과 위치

실행기가 자료를 수집하고 엔진에 전달합니다. 엔진은 확정된 자료로 전략과 위험 조건을 확인해 모의 주문을 만들고, 결과를 DB에 기록합니다. 대시보드는 그 기록을 읽고 사용자의 시작·중지 요청을 저장합니다.

| 위치 | 역할 |
| --- | --- |
| [session_runtime.py](../sessionquant/session_runtime.py) | 실행 설정, 자료 수집 작업, 엔진과 대시보드 시작 |
| [session_engine.py](../sessionquant/session_engine.py) | 입력 처리, 전략 평가, 위험 검사, 모의 주문·복구 |
| [market](../sessionquant/market) | 분봉·지표·전략·자료 준비·모의 데이터 정책 |
| [toss_websocket.py](../sessionquant/collectors/toss_websocket.py) | 실시간 호가 연결과 수신 |
| [watchlist](../sessionquant/watchlist) | 시총 후보와 집중 종목 선정 |
| [portfolio](../sessionquant/portfolio) | 후보 순위, 자금 배분, 모의 체결, 손절·청산 |
| [news](../sessionquant/news), [macro](../sessionquant/macro) | 기업·경제 뉴스 수집, 분석·비용 관리 |
| [strategy_explanation.py](../sessionquant/strategy_explanation.py) | 저장된 전략 판단을 설명. 주문 입력과 분리 |
| [database](../sessionquant/database) | MySQL/SQLite 저장, 실행 공간·비용 장부·보관 정책 |
| [research](../sessionquant/research) | 당시 입력·판단 기록과 재생 |
| [dashboard](../sessionquant/dashboard) | 홈 집중 카드와 상세 화면, 매매 제어 요청 |
| [deploy/gcp](../deploy/gcp) | 서버 설치·시작·종료·백업 스크립트 |
| [tests](../tests) | 데이터 품질·주문·복구·화면 등의 자동 검사 |

## 변경할 때 유지할 기준

- 모의 장부와 실제 계좌를 구분합니다. `observed_paper_v1`의 소수점 거래 가정은 실제 계좌 지원을 검증한 결과가 아닙니다.
- 누락된 분봉을 거래량 0으로 만들거나 수신 시각을 앞당기지 않습니다. 재생에는 당시 사용 가능했던 자료만 전달합니다.
- 이전 RVOL 프로필의 과거 검사 캐시는 내용 해시·달력·검사 버전이 맞을 때 재사용합니다. 끝까지 조회한 불완전 구간의 6시간 재조회 대기는 통신 실패·당일 분봉 생략과 다릅니다.
- LLM 응답 형식 검증과 분류 정확도를 구분합니다. 뉴스 분류·설명 결과가 주문 API를 직접 호출하지 않도록 유지합니다.
- 프롬프트를 바꾸면 버전과 캐시 구분도 갱신합니다. 본문 없는 기사 제외·원문 근거 검증은 유지합니다.
- 신규 매수 중지와 기존 보유의 손절·청산을 구분합니다. 주문 요청만으로 체결 완료를 기록하지 않습니다.
- 다른 설정의 장부를 임의로 합치지 않습니다. 로컬 키·비밀번호·사용자 SQL을 예제 파일로 덮어쓰지 않습니다.

## 개발 환경과 검사

프로젝트 가상환경에서 실행합니다. 일반 검사는 사용자 API 키와 운영 DB 없이 가능합니다.

```powershell
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m pip check
python -m sessionquant doctor
python -m ruff check sessionquant tests
$env:SQ_TEST_OFFLINE = "1"
$env:PQ_TEST_MYSQL = "0"
python -m pytest -q
```

[CI 설정](../.github/workflows/ci.yml)은 push·PR 때 Python 3.12에서 설치, 의존성 검사, CLI 확인, Ruff, 오프라인 pytest를 실행합니다. MySQL 검사는 자동으로 항상 실행되지 않습니다. GitHub Actions의 수동 실행에서 `run_mysql`을 선택하면 폐기 가능한 MySQL 8.4에 `tests/test_mysql_integration.py`와 `tests/test_retention_mysql.py`를 실행합니다. 로컬에서 같은 검사를 실행할 때도 별도 테스트 DB와 `PQ_TEST_MYSQL=1`, `PQ_MYSQL_*` 접속 변수를 사용합니다.

CI 통과는 사용자 계정 연결·유료 LLM 응답·실제 장중 성과를 증명하지 않습니다. 서버에 자동 배포하는 CD는 구현하지 않았습니다. GitHub 브랜치, VM의 체크아웃 커밋, Docker 이미지, 실제 선택한 설정을 각각 확인해야 합니다.

## 실험 기록과 문서

실제 장중에는 시세 최신성, 처리 지연, 뉴스 응답 근거, 주문·체결·청산 사유를 대조합니다. [운영 안내](REALTIME_PAPER.md)와 [입력 형식](reference/EVENT_FORMAT.md)을 참고하세요. 수익성이나 장기 운영 안정성을 자동 검사만으로 확정하지 않습니다. KIS 연결은 취소된 범위입니다.

현재 사용법은 시작·운영·참고 문서에 반영하고, 당시 단계 계획과 작업 결과는 [보관 기록](archive/README.md)에 남깁니다. 과거 문서의 기본값·테스트 수·미완료 목록을 현재 상태로 재인용하지 않습니다.

# 시세 후보 → SEC·RSS → LLM → 전략 통합 실행

기본 실행은 모의매매입니다. 급등·급락·거래량·거래대금 후보를 최대 20개 감시하며, 보유 종목과 미체결 매수의 합계는 최대 5개입니다. 조건이 맞지 않으면 한 종목도 매매하지 않습니다. 급락 탐지는 감시·위험 확인 용도이며 공매도 주문을 만들지 않습니다.

## Windows·PyCharm 시작

GitHub에서 Clone한 프로젝트를 열고 Ctrl+T로 업데이트합니다. 아래는 기존 가상환경 이름이 `venv`인 경우입니다. `.venv`라면 경로를 바꾸세요.

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.lock
.\venv\Scripts\python.exe -m pip install --no-deps -e .
.\venv\Scripts\python.exe -m pennyquant run-system --pipeline-config config/pipeline.example.yaml
```

브라우저에서 http://localhost:8501 을 엽니다. 종료는 실행 터미널에서 Ctrl+C입니다. 대시보드도 함께 종료됩니다. 기존 `run-toss`는 단순 현재가 모니터이고, 새 흐름은 `run-system`을 사용합니다. 같은 토스 Client ID로 여러 프로그램을 실행하면 토큰 재발급이 기존 연결에 영향을 줄 수 있으므로 이전 수집기를 먼저 종료합니다.

## 환경변수

| 이름 | 용도 |
|---|---|
| `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET` | 토스 인증·후보 순위·시세 |
| `SEC_USER_AGENT` | `PennyQuant Taegyu 본인연락이메일`처럼 ASCII 이름과 연락 이메일 |
| `OPENAI_API_KEY` | 뉴스 사실 추출·구조화 분석 |
| `OPENAI_MODEL` | 기본 `gpt-4.1-mini`, 본인 계정에서 사용 가능한 모델 |
| `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | 선택: Alpaca 무료 REST 보조 뉴스 |
| `TOSS_ACCOUNT_SEQ` | 실제 주문 모드에서만 필요한 계좌 식별자 |

기존 토스·Alpaca 설정은 그대로 사용할 수 있습니다. 새 변수는 PowerShell에서 다음처럼 입력합니다. 따옴표 안 문구는 안내문이며, 명령 실행 후 입력란에 실제 값을 붙여넣습니다.

```powershell
$env:SEC_USER_AGENT = Read-Host "PennyQuant ASCII이름 본인이메일"
$pqSecret = Read-Host "OpenAI API Key" -AsSecureString
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new("", $pqSecret).Password
Remove-Variable pqSecret
$env:OPENAI_MODEL = "gpt-4.1-mini"
[System.Environment]::SetEnvironmentVariable("SEC_USER_AGENT", $env:SEC_USER_AGENT, "User")
[System.Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $env:OPENAI_API_KEY, "User")
[System.Environment]::SetEnvironmentVariable("OPENAI_MODEL", $env:OPENAI_MODEL, "User")
```

지속 저장 후에는 PyCharm을 완전히 종료했다가 다시 엽니다. `.env` 자동 로딩은 없습니다. 키를 소스·설정 YAML·GitHub·채팅에 넣지 않습니다. OpenAI API는 ChatGPT 구독과 별도 과금이며 뉴스 한 건당 비용은 모델·원문 길이에 따라 달라집니다. 현재 분석기는 실행당 최대 200회 호출, 입력 16,000자·출력 1,200토큰 제한과 내용 캐시를 적용하지만 월 지출 한도를 보장하지는 않습니다.

## 실제 동작

1. 토스의 시장 전체 상승·하락·거래량·거래대금 순위로 후보를 찾습니다. 누적 거래량의 관측 변화는 후보 탐지 자료이며, 과거 동시간대 대비 RVOL과 구분합니다.
2. SEC 공식 티커 목록에서 CIK를 자동으로 찾아 기업의 공시 목록과 최근 원문을 조회합니다. 공시 종류만으로 호재·악재를 판단하지 않습니다.
3. 기업별로 검증한 RSS가 있으면 함께 조회합니다. 후보별 원문을 LLM에 전달하고 JSON 구조·원문 근거 문장·티커·발표 시각·확정 여부를 검증합니다. 실패는 UNKNOWN입니다.
4. 확인된 SEC·RSS 호재만 신규 진입 근거로 사용하고, 악재·희석 위험을 우선 차단합니다. Alpaca 지연 뉴스는 배경·위험 확인 자료이며 단독으로 진입을 승인하지 않습니다.
5. 분석 완료 후의 봉·호가로 선택한 기존 전략을 평가합니다. 위험 예산·정수 수량·최대 5개 노출·비용·일일 손실 한도를 통과해야 주문을 만듭니다.

뉴스 분석은 별도 비동기 작업입니다. 이미 완료한 악재 결과는 나머지 뉴스 분석을 기다리지 않고 반영합니다. 분석 지연 때문에 과거의 매매 시점으로 진입을 소급하지 않습니다.

## 설정 파일

`config/pipeline.example.yaml`을 복사해 `config/pipeline.local.yaml` 등으로 관리할 수 있습니다. DB·입력 파일의 상대 경로는 YAML 파일이 있는 폴더를 기준으로 계산됩니다. 시장 입력을 바꾸거나 모의/실거래 모드를 바꾸면 새 DB를 사용해야 합니다.

```yaml
db_path: ../data/system.db
mode: paper
strategy: pullback
max_candidates: 20
poll_seconds: 10
news_refresh_seconds: 60
include_alpaca: true
discover_with_toss: true
input_event_path: null
companies: {}
```

`strategy`는 `momentum`, `pullback`, `gap_breakout`, `vwap` 중 하나입니다. `companies`가 비어 있어도 SEC 기업 매핑을 자동 조회합니다. RSS는 다음처럼 해당 기업만 제공하는 공식 IR 피드를 검증한 경우에 추가합니다. 아래 URL은 자리표시자이므로 실제 URL을 확인한 뒤 사용하세요.

```yaml
companies:
  TICKER:
    cik: '확인한CIK번호'
    company_name: '확인한 기업명'
    rss_urls: ['https://기업공식사이트/실제RSS주소']
    rss_verified: true
```

광역 GlobeNewswire 피드를 특정 기업의 피드로 표시하면 안 됩니다. 일반 RSS의 기사별 기업 식별은 검증되지 않았으므로 신규 진입 근거로 사용하지 않습니다. 동일 사건이 SEC와 RSS에 나타나도 출처가 독립적인 두 사실이라고 단정하지 않습니다.

매매·비용 기본값은 `pennyquant/config/strategy.yaml`입니다. 최대 보유 5개, 하루 승인 진입 10회, 순손실 손절 5%, 순이익 익절 10%, 전략 자금의 일일 손실 한도 2%입니다. 최적화한 수익 목표가 아닌 변경 가능한 초기값입니다. [수수료·위험 안내](FEES_AND_RISK.md)를 참고하세요.

```powershell
.\venv\Scripts\python.exe -m pennyquant --config pennyquant/config/strategy.yaml run-system --pipeline-config config/pipeline.local.yaml
```

## 후보가 관찰 상태인 이유

토스 API만으로 검증된 미국 거래 가능 상태·거래정지·기업행동 처리와 비교 가능한 과거 동시간대 RVOL이 모두 채워지지는 않습니다. 현재 관측 자료에서 이 값을 임의로 만들어 진입시키지 않습니다. 따라서 기본 설정은 후보·공시·AI를 감시할 수 있지만 매매 입력이 부족하면 주문이 0건입니다.

거래용 입력에는 검증된 생산자가 공급하는 `snapshot`, 완성된 `bar`, 최신 `quote` 정규화 JSONL이 필요합니다. [이벤트 형식](EVENT_FORMAT.md)을 따르고 실제 수신 시각을 보존해야 합니다. `input_event_path`에 해당 파일 경로를 지정하면 관측용 토스 자료와 거래 입력을 섞지 않습니다. 현재 저장소에는 시장 전체에 대해 누락된 적격성·RVOL을 독립적으로 검증해 주는 별도 공급 서비스까지 포함되어 있지는 않습니다.

## 실제 주문 모드

실제 주문 어댑터는 공식 API에 맞춘 미국 정수 수량 지정가/DAY 주문, 취소, 체결·잔고 대사와 재시작 저널을 구현했습니다. 이 개발 환경에는 실계정 키가 없어 실제 주문·수수료 정산을 검증하지 않았습니다. 기본 모의매매를 검증하고 완전한 시장 생산자와 계좌를 준비한 후 사용합니다.

모의 DB와 다른 `db_path`, 검증된 `input_event_path`, `TOSS_ACCOUNT_SEQ`가 필요합니다. 실제 주문을 의도한 경우의 실행 형태는 다음과 같습니다.

```powershell
.\venv\Scripts\python.exe -m pennyquant run-system --pipeline-config config/pipeline.live.yaml --live --acknowledge-live-orders
```

현재 계좌의 미국주식 수수료율도 API로 읽습니다. 처음에는 기존 보유·외부 미체결 주문이 없는 전용 전략 계좌 상태를 요구합니다. 전략 자금은 `initial_cash`와 확인된 현금 주문 가능 금액 중 작은 값으로 제한하며, 전체 토스 계좌 평가금액으로 표시하지 않습니다. 자동 주문 이외의 수동 거래로 잔고가 달라지면 신규 주문을 차단합니다.

응답이 불명확한 주문은 재전송하지 않습니다. 주문 결과나 취소 결과가 불명확하면 토스 앱과 실제 주문 내역을 확인해야 합니다. 손절·익절은 로컬 감시에 따른 지정가 주문이므로 프로그램·네트워크가 중단되거나 거래정지·갭이 발생하면 제때 체결되지 않을 수 있습니다. 서버 보관 OCO 보호 주문은 이번 통합 실행에 포함하지 않았습니다. 종료할 때 기존 보유를 자동으로 전량 매도하지 않으며, 보유가 있으면 로컬 경고를 남깁니다.

## 화면·알림·문제 확인

후보 목록의 READY는 현재 입력 조건을 통과했다는 의미이며 주문 체결이나 수익을 보장하지 않습니다. WATCH는 근거·시장 데이터 부족, BLOCKED는 악재·위험 한도 등 차단 사유입니다. 대시보드에 제공 시각과 수신 시각, 자료 출처, LLM 실패 원인을 구분합니다.

일일 손실 한도는 비용을 반영한 실현·평가 손익으로 검사하며 당일 중지 해제로 우회할 수 없습니다. 다음 실제 거래일에 초기화합니다. DB·대시보드·터미널과 Windows 로컬 알림으로 통지하고, 이메일·메신저 전송은 하지 않습니다. Windows 알림은 OS 설정에 따라 표시되지 않을 수 있으므로 대시보드 기록을 함께 확인합니다.

포트 충돌은 `--port 8502`, 화면 없이 실행하려면 `--no-dashboard`, 제한된 횟수 확인은 `--max-cycles 1`을 사용합니다. 실제 데이터 감시 중 장기간 LLM·시장 데이터 문제가 있으면 후보의 차단 사유와 운영 상태를 먼저 확인하세요.

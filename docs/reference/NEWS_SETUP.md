# 뉴스·LLM 설정과 결과 확인

뉴스 연결과 분석 결과를 확인하거나 모델·예산을 바꿀 때 읽습니다. 수집된 기사 수, LLM에 보낸 요청 수, 검증된 응답 수는 서로 다릅니다. 추천 단순 전략은 뉴스 분석을 후보 순위 보조로 쓰고 별도의 AI 설명을 표시합니다. 주문 여부는 가격 전략과 자금·위험 규칙이 결정합니다.

[문서 목록](../README.md) · [매매 흐름](../TRADING_PROCESS.md) · [용어 설명](../GLOSSARY.md)

접속 키는 프로젝트 `.env`, 모델·예산·기업 RSS는 **실행할 때 선택한 YAML**에 둔다. 이 문서의 명령은 추천 `config/sessionquant.strategy-lab-websocket.example.yaml`을 명시한다. `--config`를 생략하면 `config/sessionquant.yaml`을 읽으므로 실행기와 다른 설정을 검사할 수 있다. `.env.example` 자체는 읽지 않는다. 이전 `config/pipeline.example.yaml`의 기업 RSS 매핑은 새 설정에 자동 복사되지 않는다.

## 어떤 뉴스가 실제 분석되는가

기사 발견 → 대상·시각·중복 확인 → 허용된 본문 확보 → LLM 요청 → 응답 근거 검증 순서다. GDELT는 다른 사이트의 기사 링크를 찾아주는 소스이며 검색 결과가 그 사이트 본문을 자유롭게 수집·분석할 허용 근거가 되지는 않는다. ‘이용권한 미확인’은 설정에 본문 취득·사용 근거가 없어 보류했다는 뜻으로, 반드시 그 사이트 접속에 실패했다는 뜻은 아니다. 본문 없는 기사와 근거가 부족한 응답은 분석 성공으로 계산하지 않는다.

대시보드 **뉴스·LLM**에서 공급자 수신, 원문 보강 사유, 요청·응답 상태를 확인한다. 저장된 결과를 추가 생성 없이 파일로 보려면 실행기와 같은 설정을 쓴다. `--output`은 기존 파일을 덮어쓰지 않는 새 이름을 지정한다.

```sh
python -m sessionquant news-status --config config/sessionquant.strategy-lab-websocket.example.yaml --output data/news-check-01.json
```

`validated_responses`는 응답 검증 통과 건수이며 분류 정답률이 아니다. 기사 본문·인용 근거·분류·후보 가점을 대조해야 한다. 전략 판단 설명은 DB 제어 상태 `strategy_explanation_v1`에 따로 저장되므로 기사 분류 건수와 같은 값으로 해석하지 않는다. 추천 전략 실험 예시 두 가지는 설명 간격 최소 5분·하루 최대 80회 시도로 설정한다. 입력된 판단이 바뀌지 않으면 다시 생성하지 않으며, 뉴스 분석과 공유하는 호출·비용 한도도 적용한다.

## 발급할 키와 필요 없는 키

| 소스 | 설정 | 역할 |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | 기업·거시 뉴스 사실 추출과 위험 분석, 사용량 과금 |
| FRED | `FRED_API_KEY` | DGS2·DGS10 국채 수익률, DCOILWTICO WTI 현물 일별 참고 |
| Alpaca | 기존 `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | 무료 REST 기업 뉴스 보조 |
| SEC EDGAR | `SEC_USER_AGENT` | API 키가 아니라 ASCII 프로젝트명 + 실제 연락 이메일 |
| 기업 IR RSS | 기업별 RSS URL | 키 없음, 회사와 피드 관계 확인 필요 |
| Fed·BLS·BEA·EIA | 코드에 등록한 공식 RSS | 현재 RSS 방식에는 키 불필요 |
| U.S. Treasury | 공식 HTML 발표 목록 | 키 없음, 공개 시각 미확인 자료는 UNKNOWN |
| GDELT DOC | 공개 기사 탐색 API | 키 없음, 국제정세·정책 등 보조 탐색 |

BLS·BEA·EIA의 별도 수치 API 키는 현재 RSS 수집에 필요 없다. FRED는 실시간 시세가 아니며 TLT·USO를 금리·WTI 현물로 대체하지 않는다.

## 1. OpenAI

1. [OpenAI API 키](https://platform.openai.com/api-keys)에 로그인한다.
2. 프로젝트를 선택하거나 SessionQuant용 프로젝트를 생성한다.
3. `Create new secret key`로 프로젝트 키를 만든다. 생성/추론을 사용할 권한이 있어야 한다.
4. [결제 설정](https://platform.openai.com/settings/organization/billing/overview)에서 API 결제·크레딧을 준비한다. ChatGPT 구독과 API 요금은 별도다.
5. 키는 아래 setup-news에서 로컬로만 입력한다. 채팅이나 Git에 넣지 않는다.

모델은 본인 프로젝트의 사용 권한을 확인한다. [모델 목록](https://developers.openai.com/api/docs/models), [가격표](https://platform.openai.com/pricing)를 참고한다. 추천 프로필의 1차 모델은 `gpt-4.1-mini`다. 다른 모델을 선택할 때는 현재 계정의 접근·생성 지원과 단가를 확인한다. 과거 문서에 나온 후보 모델을 자동으로 추천하거나 접근 가능하다고 간주하지 않는다.

## 2. FRED

[FRED API 키 페이지](https://fredaccount.stlouisfed.org/apikeys)에 로그인·가입한 뒤 API Keys에서 새 애플리케이션 키를 신청한다. 용도는 예를 들어 개인용 SessionQuant 거시 참고자료 분석이라고 실제 목적을 적는다. [공식 키 안내](https://fred.stlouisfed.org/docs/api/api_key.html)를 따른다. 발급한 키를 `FRED_API_KEY`로 저장한다.

## 3. 한 번에 저장

PyCharm Ctrl+T 업데이트 후 터미널에서 실행한다.

```powershell
.\venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.\venv\Scripts\python.exe -m pip install --no-deps -e .
.\venv\Scripts\python.exe -m sessionquant setup-news
```

OpenAI → FRED → Alpaca Key ID → Alpaca Secret → SEC 식별자 순서로 입력한다. 이미 저장된 값은 Enter로 유지한다. 선택 항목이 없으면 Enter로 건너뛴다. 이 명령은 기존 토스·MySQL 접속 설정을 보존한다.

SEC 식별자는 다음 형식이며 이름·이메일을 본인의 실제 ASCII 연락처로 바꾼다.

```text
SessionQuant YourName your-real-email@example.com
```

파일을 직접 수정한다면 `.env`의 대응 항목을 바꾼다. 비밀번호에 `$`, `#`가 있으면 작은따옴표로 감싸고, 따옴표·역슬래시가 복잡하면 setup-news를 사용한다. 기존 터미널 환경변수가 파일보다 우선하므로 이전 값을 계속 쓰는 경우 해당 실행 구성의 환경변수를 해제하고 새 터미널에서 실행한다.

## 4. 실행 설정의 모델·비용

추천 실험 YAML에는 다음과 같은 제한이 이미 있다. 본인의 실행 파일에서 값을 바꿀 때 기존 `llm` 블록을 수정하고 중복 블록을 만들지 않는다.

```yaml
llm:
  primary_model: gpt-4.1-mini
  enable_escalation: false
  format_retry_limit: 0
  daily_primary_call_cap: 150
  hourly_primary_call_cap: 30
  monthly_soft_budget_usd: '2.0'
  monthly_hard_budget_usd: '3.0'
strategy_explanation:
  enabled: true
  min_interval_seconds: 300
  max_attempts_per_day: 80
```

뉴스 분석과 AI 브리핑을 합쳐 시간당 최대 30회, 하루 최대 150회 요청한다. 브리핑의 하루 80회 시도 한도는 이와 별도로 적용하며 요청 횟수를 추가로 보장하지 않는다. 시간·날짜 경계는 UTC 기준이므로 한국시간에는 매시 정각과 오전 9시에 새 구간이 시작된다. 월간 비용 제한은 기존 2달러·3달러를 유지한다. OpenAI 충전 잔액과 별개인 프로그램 내부 한도다. 설정 파일 변경은 실행 중인 프로그램에 즉시 반영되지 않으며, 업데이트 후 재시작할 때 읽는다. 저장된 사용량은 재시작해도 유지된다.

새 엔진에서 `.env`의 `OPENAI_MODEL`만 변경해도 이 모델 설정은 바뀌지 않는다. `OPENAI_MODEL`은 이전 호환 분석 경로에서 사용한다. 다른 모델을 시험하려면 접근·생성 검증 후 YAML의 `primary_model`을 변경한다. 모델별 단가는 코드의 알려진 표 또는 명시 설정값으로 계산하며 실제 청구액을 확정한 것으로 간주하지 않는다.

예산은 이 운영 분석기의 계산·예약 기준이다. OpenAI 계정 전체의 강제 결제 상한이 아니고 다른 프로젝트·직접 API 호출·모델 평가 비용까지 통제하지 않는다. `llm-evaluate --execute`는 별도 유료 평가다. 이 문서는 영어·한국어 프롬프트의 실제 정확도 비교 결과를 제공하지 않는다. 프롬프트 변경 시 별도 평가와 버전 관리를 한다.

## 5. 기업 SEC·RSS

SEC는 티커→CIK를 공식 매핑에서 찾는다. 기업 RSS는 패키지에 포함한 [시가총액 상위 100개 기업 중 검증된 공식 피드 목록](COMPANY_RSS.md)을 기본으로 적용한다. 확인되지 않은 피드는 제외한다. RSS 기본 목록과 운영 감시목록은 분리한다. 현재 추천 프로필은 시총 후보 최대 50개·거래대금 중심 집중 10개를 사용한다. 뉴스 수집 대상은 보유·집중 우선순위와 호출 예산을 따르며 RSS 카탈로그의 100개를 매 주기 전부 분석하지 않는다. [단계 01](../archive/stages/STAGE01_WATCHLIST.md)의 100→20은 이전 계획이다.

기업별 사용자 설정은 기본 목록의 같은 필드보다 우선한다. 직접 확인한 피드 주소는 아래처럼 `companies`에 추가한다. 목록 전체를 해제하려면 `company_rss_catalog: false`, 특정 기업의 RSS를 해제하려면 그 기업에 `rss_urls: []`를 지정한다. RSS 카탈로그는 새 기업 피드를 자동 발견하거나 운영 시총 순위를 갱신하는 기능이 아니다. 시총·집중 목록 갱신은 별도 수집기가 담당한다.

```yaml
companies:
  AAPL:
    company_name: Apple Inc.
    cik: '0000320193'
    rss_urls: []
    rss_verified: false
```

`rss_urls` 안에는 실제로 확인한 해당 회사 공식 IR RSS HTTPS 주소를 넣는다. 공식 IR·회사 연결을 확인하기 전에는 `rss_verified: true`로 바꾸지 않는다. RSS 자체에 API 키는 없으며, 일반 뉴스 검색 RSS를 회사 공식 IR 피드로 등록하면 안 된다. 기존 회사 여러 개도 같은 방식으로 추가한다. `companies: {}`와 새 companies 블록을 중복해서 두지 않는다.

Alpaca는 기존 키를 재사용할 수 있다. 키가 없다면 [Alpaca 대시보드](https://app.alpaca.markets/)와 [인증 문서](https://docs.alpaca.markets/docs/authentication)를 참고한다. `include_alpaca: true`에서 REST를 보조 자료로 수집하고 실시간 스트림 권한을 가정하지 않는다.

## 6. 자동 등록된 공식·글로벌 소스

- [Fed](https://www.federalreserve.gov/feeds/feeds.htm): `https://www.federalreserve.gov/feeds/press_all.xml`
- [BLS](https://www.bls.gov/feed/): `https://www.bls.gov/feed/bls_latest.rss`
- [BEA](https://www.bea.gov/resources/for-developers): `https://apps.bea.gov/rss/rss.xml`
- [EIA](https://www.eia.gov/tools/rssfeeds/): `https://www.eia.gov/rss/press_rss.xml`
- [Treasury](https://home.treasury.gov/news/press-releases): 공식 발표 HTML 탐색. 정확한 공개 시각을 모르므로 확정 위험으로 사용하지 않는다.
- [GDELT DOC](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/): `https://api.gdeltproject.org/api/v2/doc/doc`

Fed·BLS·BEA·EIA RSS와 Treasury·GDELT는 뉴스 작업 실행 시 등록돼 있어 URL을 따로 넣지 않아도 된다. GDELT의 발견 시각은 기사 원문 공개 시각이 아니며 단독으로 사건 확정을 하지 않는다. 네트워크 오류와 수집 지연은 UNKNOWN으로 남는다.

## 7. 연결 확인: 주문·LLM 생성 없음

```powershell
# 키 존재 여부·모델·예산·피드 설정만 점검. 값은 출력하지 않음.
.\venv\Scripts\python.exe -m sessionquant check-news --config config/sessionquant.strategy-lab-websocket.example.yaml

# OpenAI 모델 메타데이터 GET만 검사. LLM 응답을 생성하지 않음.
.\venv\Scripts\python.exe -m sessionquant check-news --config config/sessionquant.strategy-lab-websocket.example.yaml --connect --provider openai

# FRED 연결
.\venv\Scripts\python.exe -m sessionquant check-news --config config/sessionquant.strategy-lab-websocket.example.yaml --connect --provider fred

# 모든 소스의 읽기 연결 확인
.\venv\Scripts\python.exe -m sessionquant check-news --config config/sessionquant.strategy-lab-websocket.example.yaml --connect
```

OpenAI의 `accessible: true`는 모델 메타데이터 접근 성공이며 실제 JSON 생성·정확도까지 검증한 것은 아니다. HTTP 401은 키·인증, 403은 권한, 404는 모델/경로 접근, 429는 사용 제한을 확인한다. 뉴스 0건 자체는 오류가 아니므로 공급자 상태와 함께 본다. FRED 오류의 URL에는 키가 포함될 수 있어 점검 명령은 URL과 예외 원문을 출력하지 않는다.

## 8. 실행 경로와 추가 설정

```powershell
.\venv\Scripts\python.exe -m sessionquant universe-refresh --output data/sp500-universe.json
.\venv\Scripts\python.exe -m sessionquant run --config config/sessionquant.strategy-lab-websocket.example.yaml
```

`news_enabled: true`, `include_alpaca: true`에서 위 소스를 수집·분석한다. 기업 180초·거시 900초 등 설정 주기는 작업 요청 간격이다. 실제 도착량·처리량은 공급자 응답·중복·허용 본문·예산에 따라 달라지며 그 주기마다 기사나 LLM 호출이 반드시 생기는 것은 아니다. 기존 명시적 `news_refresh_seconds`는 기업/거시 새 키가 없을 때만 fallback으로 사용한다. [독립 주기·권한 설정 예시](../../config/sessionquant.news.example.yaml)와 [06 운영 안내](../archive/stages/STAGE06_NEWS.md)를 참고한다. 화면에서 매매 시작을 누르기 전에도 뉴스·시세 수집은 동작하며 OpenAI 키가 있으면 분석 비용이 발생할 수 있다.

뉴스·LLM과 전략의 연결은 `run`의 PAPER_LOCAL 엔진에서 사용한다. 추천 단순 전략에서 검증된 기업 뉴스 분석은 **후보 순위 가점**, `strategy_explanation`은 **현재 판단 설명** 역할이다. 뉴스가 없어도 가격 전략을 평가하며 LLM이 주문하거나 뉴스 악재만으로 강제 청산하지 않는다. 기존 `pullback` 등 뉴스 위험을 사용하는 프로필과 구분한다. `run-account`는 실제 계좌 조회이고 `live-local`은 Phase 5의 소액 주문 검증이다. 두 경로에 키를 넣었다고 뉴스 기반 완전 자동 실거래가 연결되지는 않는다.

기존 뉴스 위험 프로필의 발표 전후 15분 진입 제한은 확인된 경제 일정이 DB에 있어야 작동한다. 단순 전략에서는 이 일정 제한을 주문 조건으로 쓰지 않는다. BLS·BEA는 별도 공식 ICS에서 명시한 발표 시각만 자동 등록한다. RSS 기사로 향후 일정을 추측하지 않으며 FOMC·EIA·Treasury는 확인한 수동 입력을 유지한다. `check-news --connect --provider calendar`는 생성·주문 없이 공식 일정 읽기 연결만 검사한다. [Fed 일정](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm), [BLS 일정](https://www.bls.gov/schedule/news_release/), [BEA 일정](https://www.bea.gov/news/schedule)을 확인하고 [입력 형식](EVENT_FORMAT.md)에 따라 `calendar-import`로 등록한다. 일정은 확인 전 UNKNOWN이며 예시 날짜를 실제 확정 일정으로 가져오지 않는다.

별도 실제 주문 검증은 [PHASE5_LOCAL.md](PHASE5_LOCAL.md)에 있으며 Paper의 필수 단계가 아니다. GCP에서 실행할 때의 키 보관·배포는 [GCP 안내](../GCP_PAPER.md)를 따른다.


## 잔액이 없을 때와 전체 연결 검사

키 미설정, OpenAI의 `insufficient_quota` / `credit_balance_exhausted` 등 잔액·한도 응답은 뉴스 분석만 제한한다. 차트 기반 진입과 손절·당일 청산은 계속한다. 인증 오류·요청 속도 제한은 잔액 부족과 별도로 표시한다. 잔액 부족 시 10분 동안 반복 생성 요청을 중지하고 이후 필요한 분석 때 다시 확인한다. 충전 후 즉시 새 프로세스를 실행해도 같은 키의 대기 상태는 DB에 보존된다. 새로운 실패 뉴스는 UNKNOWN으로 남긴다. 기존 뉴스 위험 프로필에서는 이미 확인된 악재·일정 제한을 취소하지 않는다. 단순 전략에서는 해당 제한 자체를 주문 입력으로 사용하지 않는다. 신호가 없거나 다른 위험 검사에 실패하면 거래하지 않는다.

뉴스·LLM 화면의 충전 누계는 사용자가 직접 적은 메모이다. 미입력이나 $0 기록은 실제 잔액 고갈을 뜻하지 않으며 주문·분석의 차단 기준으로 쓰지 않는다. 공식 사용량·잔액 링크와 비용 범위는 [MAIN_DASHBOARD.md](MAIN_DASHBOARD.md)에 정리했다.

토스 수집·거래 엔진을 종료한 뒤 한 번에 검사한다.

```powershell
.\venv\Scripts\python.exe -m sessionquant check-connections --config config/sessionquant.strategy-lab-websocket.example.yaml
```

이 명령은 생성 비용 없이 모델 접근과 뉴스 소스 연결을 검사하고 토스 시세·계좌와 DB를 추가 확인한다. `llm_credit_balance_verified: false`는 공식 잔액을 조회하지 않았다는 뜻이다. 표준 API 키만으로 잔액을 확정하거나 키 연결 성공을 충전 성공으로 취급하지 않는다.

## 07 호출 감사·비용 설정

[07 구현 기록](../archive/stages/STAGE07_LLM.md)과 [기능별 설정 예시](../../config/sessionquant.llm.example.yaml)는 세부 계약을 확인하는 참고 자료다. 실제 운영의 활성화·예산 값은 실행 중인 YAML을 따른다. 메타데이터 감사는 기본 적용하며 `llm_debug_audit.enabled`와 `macro_context_llm.enabled`는 기본 false다. 06 원문 보강 정책과 별도로 원문 보관 권한이 필요하다. `SQ_LLM_AUDIT_ALLOW_READ=1`은 허용되어 암호화 보관된 원문을 로컬에서 읽는 추가 권한이며, 원문 취득·저장이나 유료 평가를 자동 활성화하지 않는다.

현재 메뉴는 6개다. 홈은 집중 감시·장부·AI 설명을 요약하고, 호출 필터·실패 필드·정제 결과·비용·수동 잔액 메모는 뉴스·LLM 상세에 표시한다. 공식 조직 비용은 미연결 UNKNOWN, 선불 잔액은 수동 확인 기준의 추정이다. `llm-evaluate` 기본 미리보기에는 유료 생성이 없으며 실제 `--execute`는 사용자가 유료 평가를 선택했을 때 실행한다.

## 연결 결과가 일부 실패하는 경우

`configured: true`는 값이 있다는 뜻이며 인증 성공을 보장하지 않는다. OpenAI 모델 메타데이터 접근 성공도 충전 잔액·실제 생성·주문 권한 검사와 별개다. `llm_credit_balance_verified: false`, `structured_generation_verified: false`, `order_permissions_verified: false`는 이 읽기 전용 점검에서 해당 작업을 하지 않았다는 뜻이다.

FRED는 키 오류에도 HTTP 400을 반환할 수 있다. [공식 오류 설명](https://fred.stlouisfed.org/docs/api/fred/errors.html)에 따라 서버 메시지를 고정된 코드로 변환하며, 키가 포함될 수 있는 원문과 URL은 출력하지 않는다.

| `error_code` | 확인할 항목 |
|---|---|
| `invalid_api_key_format` | FRED API Keys에서 발급한 키 전체를 입력했는지, 공백·따옴표 포함 여부 |
| `api_key_rejected` | 등록된 FRED 키인지 확인하고 `.env`의 `FRED_API_KEY` 수정 |
| `invalid_realtime_window` | 프로젝트 업데이트 후 재검사. 과거 조회라면 지정 기준일과 PC 날짜·시간 확인 |
| `invalid_series_id` | 요청 시리즈의 제공 여부 |
| `request_rejected` | HTTP 상태와 요청·서버 상태 추가 확인 |

[FRED API Keys](https://fredaccount.stlouisfed.org/apikeys)에서 확인한다. 키 값은 공유하지 않는다. 기존 셸에 직접 지정한 환경변수가 있으면 `.env`보다 우선하므로 수정 후 새 터미널의 설정도 확인한다.

```powershell
.\venv\Scripts\python.exe -m sessionquant check-news --config config/sessionquant.strategy-lab-websocket.example.yaml --connect --provider fred
.\venv\Scripts\python.exe -m sessionquant check-news --config config/sessionquant.strategy-lab-websocket.example.yaml --connect --provider official
.\venv\Scripts\python.exe -m sessionquant check-news --config config/sessionquant.strategy-lab-websocket.example.yaml --connect --provider gdelt
```

RSS는 `text/xml` 응답도 수신하도록 요청한다. 공식 피드·GDELT의 결과는 `ok`·`partial`·`unavailable`로 구분하고 실패 항목에 `http_status` 또는 `error_type`을 표시한다. HTML 오류 페이지를 유효한 빈 RSS로 처리하지 않는다. 공급자가 403·503을 반환하거나 연결이 지연될 수 있으므로 수정 후에도 사용자 PC에서 재검사한다. GDELT의 `unavailable`과 성공한 검색의 0건은 다르다.

`company_rss_count: 0`과 `rss: not_configured`는 기본 목록을 해제하고 직접 등록한 피드도 없는 상태를 뜻한다. 기본 목록을 사용하면 `companies: {}`여도 검증된 RSS가 적용된다. Fed 등 공식 기관 RSS와 별도로 관리하며 자동으로 임의의 기업 피드를 추가하지 않는다. Alpaca의 일부 지원하지 않는 태그 생략 메시지는 다른 유효한 티커와 기사 수집이 성공했다면 수집 중단 오류가 아니다.


### FRED 운영 조회 기준일

운영 수집과 연결 검사는 `realtime_start`·`realtime_end`를 생략하여 FRED 서버의 기본 오늘을 사용한다. 한국·UTC 날짜를 강제로 보내 FRED 쪽에서 미래 기준일로 거부하는 문제를 피한다. [공식 기준일 설명](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)을 따른다.

DB에는 UTC `received_at`, 응답의 `realtime_start`·`realtime_end`, `query_mode: provider_today`를 구분해 저장한다. 자료는 여전히 일별 보조 지표다. 운영 모드 변경으로 과거 날짜 조회를 최신 수정치로 대체하지 않는다. `FREDClient.fetch(now=지정시각)`은 기존의 지정 날짜를 요청하며 `query_mode: explicit_asof`, `requested_asof`를 기록하고, 해당 요청이 거부되면 최신 자료로 재시도하지 않는다.

업데이트 후 다음 명령으로 재검사한다. 기존 키는 유지한다.

```powershell
.\venv\Scripts\python.exe -m sessionquant check-news --config config/sessionquant.strategy-lab-websocket.example.yaml --connect --provider fred
```

성공하면 `status: ok`, `query_mode: provider_today`, 시리즈 목록과 FRED가 반환한 `realtime_dates`가 표시된다. 사용자 PC의 실제 키로 이 결과를 확인해야 연결 성공이 확정된다.

# 뉴스·LLM 설정 전체 안내

접속 키는 프로젝트 `.env`, 모델·예산·기업 RSS는 `config/sessionquant.yaml`에 둔다. `.env.example` 자체는 읽지 않는다. 이전 `config/pipeline.example.yaml`의 기업 RSS 매핑은 새 설정에 자동 복사되지 않는다.

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

모델은 본인 프로젝트의 사용 권한을 확인한다. [모델 목록](https://developers.openai.com/api/docs/models), [가격표](https://platform.openai.com/pricing)를 참고한다. 기존 모델의 실제 정확도 비교 전이라 기본 1차는 `gpt-4.1-mini`를 유지한다. `gpt-6-luna`, `gpt-6.1-terra` 후보는 계정 접근과 생성 검증 후 선택한다.

## 2. FRED

[FRED API 키 페이지](https://fredaccount.stlouisfed.org/apikeys)에 로그인·가입한 뒤 API Keys에서 새 애플리케이션 키를 신청한다. 용도는 예를 들어 개인용 SessionQuant 거시 참고자료 분석이라고 실제 목적을 적는다. [공식 키 안내](https://fred.stlouisfed.org/docs/api/api_key.html)를 따른다. 발급한 키를 `FRED_API_KEY`로 저장한다.

## 3. 한 번에 저장

PyCharm Ctrl+T 업데이트 후 터미널에서 실행한다.

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.lock
.\venv\Scripts\python.exe -m pip install --no-deps -e .
.\venv\Scripts\python.exe -m sessionquant setup-news
```

OpenAI → FRED → Alpaca Key ID → Alpaca Secret → SEC 식별자 순서로 입력한다. 이미 저장된 값은 Enter로 유지한다. 선택 항목이 없으면 Enter로 건너뛴다. 이 명령은 기존 토스·MySQL 접속 설정을 보존한다.

SEC 식별자는 다음 형식이며 이름·이메일을 본인의 실제 ASCII 연락처로 바꾼다.

```text
SessionQuant YourName your-real-email@example.com
```

파일을 직접 수정한다면 `.env`의 대응 항목을 바꾼다. 비밀번호에 `$`, `#`가 있으면 작은따옴표로 감싸고, 따옴표·역슬래시가 복잡하면 setup-news를 사용한다. 기존 터미널 환경변수가 파일보다 우선하므로 이전 값을 계속 쓰는 경우 해당 실행 구성의 환경변수를 해제하고 새 터미널에서 실행한다.

## 4. 새 엔진의 모델·비용 설정

`config/sessionquant.yaml`의 `llm: {}`를 아래 블록으로 바꾼다. 파일에 llm을 두 번 만들지 않는다.

```yaml
llm:
  primary_model: gpt-4.1-mini
  escalation_model: gpt-6.1-terra
  daily_primary_call_cap: 45
  daily_escalation_call_cap: 5
  monthly_soft_budget_usd: '2.0'
  monthly_hard_budget_usd: '3.0'
  max_concurrent_calls: 3
  enable_event_cache: true
```

새 엔진에서 `.env`의 `OPENAI_MODEL`만 변경해도 이 모델 설정은 바뀌지 않는다. `OPENAI_MODEL`은 이전 호환 분석 경로에서 사용한다. Luna를 시험하려면 접근·생성 검증 후 YAML의 `primary_model`을 `gpt-6-luna`로 선택한다. 모델별 단가는 코드의 알려진 표 또는 명시 설정값으로 계산하며 실제 청구액을 확정한 것으로 간주하지 않는다.

예산은 이 운영 분석기의 계산·예약 기준이다. OpenAI 계정 전체의 강제 결제 상한이 아니고 다른 프로젝트·직접 API 호출·모델 평가 비용까지 통제하지 않는다. `llm-evaluate --execute`는 별도 유료 평가다. 영어/한국어 프롬프트의 실제 정확도 비교는 수행 전이며 내부 프롬프트는 유지한다.

## 5. 기업 SEC·RSS

SEC는 티커→CIK를 공식 매핑에서 찾는다. 기업 RSS는 패키지에 포함한 [시가총액 상위 100개 기업 중 검증된 공식 피드 목록](COMPANY_RSS.md)을 기본으로 적용한다. 확인되지 않은 피드는 제외한다. RSS 기본 목록과 운영 감시목록은 분리한다. 단계 01부터 운영의 100→20 선정·위험 우선대상 계약은 [STAGE01_WATCHLIST.md](../archive/stages/STAGE01_WATCHLIST.md)를 따른다.

기업별 사용자 설정은 기본 목록의 같은 필드보다 우선한다. 직접 확인한 피드 주소는 아래처럼 `companies`에 추가한다. 목록 전체를 해제하려면 `company_rss_catalog: false`, 특정 기업의 RSS를 해제하려면 그 기업에 `rss_urls: []`를 지정한다. 새 기업의 피드를 운영 중 자동 발견하거나 시총 순위를 자동 갱신하지는 않는다.

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
.\venv\Scripts\python.exe -m sessionquant check-news

# OpenAI 모델 메타데이터 GET만 검사. LLM 응답을 생성하지 않음.
.\venv\Scripts\python.exe -m sessionquant check-news --connect --provider openai

# FRED 연결
.\venv\Scripts\python.exe -m sessionquant check-news --connect --provider fred

# 모든 소스의 읽기 연결 확인
.\venv\Scripts\python.exe -m sessionquant check-news --connect
```

OpenAI의 `accessible: true`는 모델 메타데이터 접근 성공이며 실제 JSON 생성·정확도까지 검증한 것은 아니다. HTTP 401은 키·인증, 403은 권한, 404는 모델/경로 접근, 429는 사용 제한을 확인한다. 뉴스 0건 자체는 오류가 아니므로 공급자 상태와 함께 본다. FRED 오류의 URL에는 키가 포함될 수 있어 점검 명령은 URL과 예외 원문을 출력하지 않는다.

## 8. 실행 경로와 추가 설정

```powershell
.\venv\Scripts\python.exe -m sessionquant universe-refresh --output data/sp500-universe.json
.\venv\Scripts\python.exe -m sessionquant run --config config/sessionquant.yaml
```

`news_enabled: true`, `include_alpaca: true`에서 위 소스를 수집·분석한다. 06단계 기본 주기는 기업 180초·거시 900초·공식 일정 3600초·FRED 86400초다. 기존 명시적 `news_refresh_seconds`는 기업/거시 새 키가 없을 때만 fallback으로 사용한다. [독립 주기·권한 설정 예시](../../config/sessionquant.news.example.yaml)와 [06 운영 안내](../archive/stages/STAGE06_NEWS.md)를 참고한다. 화면에서 매매 시작을 누르기 전에도 뉴스·시세 수집은 동작하며 OpenAI 키가 있으면 분석 비용이 발생할 수 있다.

중요: 현재 뉴스·LLM 위험 통합은 `run`의 PAPER_LOCAL 엔진에서 사용한다. `run-account`는 실제 계좌 조회이고 `live-local`은 Phase 5의 소액 주문 검증이다. 두 경로에 키를 넣었다고 뉴스 기반 완전 자동 실거래가 연결되지는 않는다.

발표 전후 15분 진입 제한은 확인된 경제 일정이 DB에 있어야 작동한다. BLS·BEA는 별도 공식 ICS에서 명시한 발표 시각만 자동 등록한다. RSS 기사로 향후 일정을 추측하지 않으며 FOMC·EIA·Treasury는 확인한 수동 입력을 유지한다. `check-news --connect --provider calendar`는 생성·주문 없이 공식 일정 읽기 연결만 검사한다. [Fed 일정](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm), [BLS 일정](https://www.bls.gov/schedule/news_release/), [BEA 일정](https://www.bea.gov/news/schedule)을 확인하고 [입력 형식](EVENT_FORMAT.md)에 따라 `calendar-import`로 등록한다. 일정은 확인 전 UNKNOWN이며 예시 날짜를 실제 확정 일정으로 가져오지 않는다.

실거래 설정의 최소 주문 금액·소수점 자격 확인·계좌 확인·기존 위험 설정은 [PHASE5_LOCAL.md](PHASE5_LOCAL.md)를 따른다. 현재 단계에서 GCP 키·배포 설정은 필요 없다.


## 잔액이 없을 때와 전체 연결 검사

키 미설정, OpenAI의 `insufficient_quota` / `credit_balance_exhausted` 등 잔액·한도 응답은 뉴스 분석만 제한한다. 차트 기반 진입과 손절·당일 청산은 계속한다. 인증 오류·요청 속도 제한은 잔액 부족과 별도로 표시한다. 잔액 부족 시 10분 동안 반복 생성 요청을 중지하고 이후 필요한 분석 때 다시 확인한다. 충전 후 즉시 새 프로세스를 실행해도 같은 키의 대기 상태는 DB에 보존된다. 새로운 실패 뉴스는 UNKNOWN이며 이전의 확인된 악재·공식 일정 제한은 취소하지 않는다. 신호가 없거나 다른 위험 검사에 실패하면 거래하지 않는다.

메인 화면의 충전 누계는 사용자가 직접 적은 메모이다. 미입력이나 $0 기록은 실제 잔액 고갈을 뜻하지 않으며 주문·분석의 차단 기준으로 쓰지 않는다. 공식 사용량·잔액 링크와 비용 범위는 [MAIN_DASHBOARD.md](MAIN_DASHBOARD.md)에 정리했다.

토스 수집·거래 엔진을 종료한 뒤 한 번에 검사한다.

```powershell
.\venv\Scripts\python.exe -m sessionquant check-connections
```

이 명령은 생성 비용 없이 모델 접근과 뉴스 소스 연결을 검사하고 토스 시세·계좌와 DB를 추가 확인한다. `llm_credit_balance_verified: false`는 공식 잔액을 조회하지 않았다는 뜻이다. 표준 API 키만으로 잔액을 확정하거나 키 연결 성공을 충전 성공으로 취급하지 않는다.

## 07 호출 감사·비용 설정

[07 운영 안내](../archive/stages/STAGE07_LLM.md)와 [PAPER 설정 예시](../../config/sessionquant.llm.example.yaml)를 따른다. 메타데이터 감사는 기본 적용하며 `llm_debug_audit.enabled`와 `macro_context_llm.enabled`는 기본 false다. 06 원문 보강 정책과 별도로 원문 보관 권한이 필요하다. `SQ_LLM_AUDIT_ALLOW_READ=1`은 허용되어 암호화 보관된 원문을 로컬에서 읽는 추가 권한이며, 원문 취득·저장이나 유료 평가를 자동 활성화하지 않는다.

기존 메인 5개 탭을 유지한다. 이번 달 프로그램 추정 비용은 메인 카드에, 호출 필터·실패 필드·정제 결과·수동 잔액 메모는 뉴스 분석의 LLM 상세에 표시한다. 공식 조직 비용은 미연결 UNKNOWN, 선불 잔액은 수동 확인 기준의 추정이다. `llm-evaluate` 기본 미리보기에는 유료 생성이 없으며 실제 `--execute`는 별도 승인 이후에만 사용한다.

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
.\venv\Scripts\python.exe -m sessionquant check-news --connect --provider fred
.\venv\Scripts\python.exe -m sessionquant check-news --connect --provider official
.\venv\Scripts\python.exe -m sessionquant check-news --connect --provider gdelt
```

RSS는 `text/xml` 응답도 수신하도록 요청한다. 공식 피드·GDELT의 결과는 `ok`·`partial`·`unavailable`로 구분하고 실패 항목에 `http_status` 또는 `error_type`을 표시한다. HTML 오류 페이지를 유효한 빈 RSS로 처리하지 않는다. 공급자가 403·503을 반환하거나 연결이 지연될 수 있으므로 수정 후에도 사용자 PC에서 재검사한다. GDELT의 `unavailable`과 성공한 검색의 0건은 다르다.

`company_rss_count: 0`과 `rss: not_configured`는 기본 목록을 해제하고 직접 등록한 피드도 없는 상태를 뜻한다. 기본 목록을 사용하면 `companies: {}`여도 검증된 RSS가 적용된다. Fed 등 공식 기관 RSS와 별도로 관리하며 자동으로 임의의 기업 피드를 추가하지 않는다. Alpaca의 일부 지원하지 않는 태그 생략 메시지는 다른 유효한 티커와 기사 수집이 성공했다면 수집 중단 오류가 아니다.


### FRED 운영 조회 기준일

운영 수집과 연결 검사는 `realtime_start`·`realtime_end`를 생략하여 FRED 서버의 기본 오늘을 사용한다. 한국·UTC 날짜를 강제로 보내 FRED 쪽에서 미래 기준일로 거부하는 문제를 피한다. [공식 기준일 설명](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)을 따른다.

DB에는 UTC `received_at`, 응답의 `realtime_start`·`realtime_end`, `query_mode: provider_today`를 구분해 저장한다. 자료는 여전히 일별 보조 지표다. 운영 모드 변경으로 과거 날짜 조회를 최신 수정치로 대체하지 않는다. `FREDClient.fetch(now=지정시각)`은 기존의 지정 날짜를 요청하며 `query_mode: explicit_asof`, `requested_asof`를 기록하고, 해당 요청이 거부되면 최신 자료로 재시도하지 않는다.

업데이트 후 다음 명령으로 재검사한다. 기존 키는 유지한다.

```powershell
.\venv\Scripts\python.exe -m sessionquant check-news --connect --provider fred
```

성공하면 `status: ok`, `query_mode: provider_today`, 시리즈 목록과 FRED가 반환한 `realtime_dates`가 표시된다. 사용자 PC의 실제 키로 이 결과를 확인해야 연결 성공이 확정된다.

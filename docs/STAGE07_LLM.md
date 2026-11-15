# 07 LLM 근거·호출 감사·비용 운영

기준: 2026-11-15, `wip/stage06-handoff-20261115`, 06 완료 커밋 `0fa5ad4` 이후. [프로젝트 방향](PROJECT_DIRECTION.md)과 함께 읽는다. 이 단계는 구현 및 격리 검증이며 실제 모델 품질·실거래 운영 승인이 아니다.

## 연결 경로와 파일

`session_runtime` → `NewsPipeline` → 06 수집·원문 적격성 → `BudgetedAnalyzer` → 공통 `BudgetClient` → 예산 예약 + 요청 감사 → HTTP 1회 → 사용량 정산 + 응답 감사 → 근거 검증 → 최종 분석을 연결한다. 기업 `NewsService`의 저장소 사용 경로, 기존 `pipeline.py`, 평가 실행도 공통 감사 경계를 사용한다. 저장소 없는 저수준 `NewsAnalyzer` 라이브러리 API는 기존 호환 경로로 남긴다.

| 요구사항 | 주요 파일 |
|---|---|
| 호출별 원본·사용량·최종 판단 연결 | `llm/audit.py`, `llm/client.py`, `macro/analysis.py`, `macro/budget.py` |
| 실제 전송 본문·문단·해시·오류 분리 | `llm/evidence.py`, `news/analysis.py`, `news/intake.py` |
| 06 원문 계약·버전 캐시 | `news/enrichment.py`, `macro/models.py`, `macro/service.py` |
| 선택적 금리·유가 사건 해설 | `macro/context_analysis.py` |
| 평가·무료 미리보기 | `macro/evaluation.py`, `examples/llm_eval.synthetic.jsonl`, `cli.py` |
| 비용·수동 잔액·조회 화면 | `llm/costs.py`, `dashboard/llm_panel.py`, `dashboard/summary_panels.py` |
| 추가 마이그레이션·격리·정리 | `database/repository.py`, `database/mysql_repository.py`, `sql/llm_call_audit.sql` |
| 설정·기동·점검 | `session_config.py`, `session_runtime.py`, `news_setup.py`, `config/sessionquant.llm.example.yaml` |
| 검증 | `tests/test_stage07_*.py`, `tests/test_mysql_integration.py`, 기존 회귀 테스트 |

## 감사 생애주기

실제 HTTP 시도마다 UUID `llm_call_id`를 발급한다. 같은 기사의 primary/escalation/허용한 형식 재시도는 서로 다른 ID를 갖고 `logical_analysis_id`로 묶인다. `llm_usage`의 ID와 `llm_call_id`, 분석의 `llm_call_ids`, 최종 채택한 `adopted_llm_call_id`로 연결한다. 실패한 2차 호출도 별도로 남는다.

사전 제외·캐시·예산 차단에는 `precheck_id`만 있고 `llm_call_id=null`, `network_attempted=false`다. 해당 기록의 0토큰은 요청 전 진단값이며 실제 사용량에 0건 호출을 추가하지 않는다. 캐시는 원래 호출 ID를 참조하며 새 사용량을 만들지 않는다. 과거 연결 키가 없는 사용량은 `LEGACY_UNMAPPED`로 표시한다.

| 상태 | 의미 |
|---|---|
| `SKIPPED_PRECHECK` | 출처·본문·게시일·권한·중복·분석 진행 등으로 요청 전 제외 |
| `CACHE_HIT` | 동일 버전의 저장 결과 재사용 |
| `BUDGET_BLOCKED` | 자체 시간·일·월 한도로 요청하지 않음 |
| `REQUESTED` | 예산과 감사 기록을 확정한 뒤 전송 경계에 진입 |
| `RESPONSE_RECEIVED` | HTTP 성공 응답 수신, 의미 검증 전 중간 상태 |
| `HTTP_FAILED` / `RATE_LIMITED` | HTTP 오류 / 429 |
| `REFUSED` / `INCOMPLETE` | 모델 거부 / 정상 종료되지 않은 응답 |
| `JSON_PARSE_FAILED` / `SCHEMA_VALIDATION_FAILED` | JSON 문법 / 필드·타입·enum 오류 |
| `EVIDENCE_MISMATCH` / `BUSINESS_RULE_REJECTED` | 본문 인용 불일치 / 의미·운영 규칙 위반 |
| `SUCCEEDED` | 해당 호출의 출력 검증 통과. 실제 매매 자격은 별도 |
| `OUTCOME_UNKNOWN` | timeout·통신 단절 등으로 결과 미확정 |

HTTP 코드·finish_reason·provider request ID·각 시각·지연·모델·버전·해시·실패 필드를 보관한다. 예외 원문을 자동 출력하지 않는다. 요청 전 감사 저장 실패는 전송을 막는다. 요청 후 정산 실패는 성공 판단을 막고 예약액을 유지하며 즉시 중복 요청하지 않는다. 전송 직전 프로세스가 종료되면 `REQUESTED`가 실제 도달을 확증하지 못한다. 미정산 요청은 SQL로 대사하고 임의 환불하지 않는다.

예산은 DB 원자 갱신으로 독립 실행자 간에도 예약·멱등 정산한다. 기본 시간당 primary 10/escalation 2, 일일 45/5, 월 soft $2/hard $3, 출력 상한 1,200토큰이다. 재시도 기본 0, 설정 상한 1회이며 JSON/스키마 형식 오류만 대상이다. HTTP·근거·의미 오류를 자동 유료 재시도하지 않는다. 분석 점유 5분으로 동시·재시작 중복을 막으며 결과 미확정 때는 점유 만료까지 유지한다. 월 경계의 이전 예약을 자동 청구액으로 바꾸지 않는다.

## 기사 적격성과 근거

기업 24시간/거시 72시간 정책을 유지한다. 24~72시간 거시는 배경으로 구분한다. 게시일·수신·최초 이용 가능·분석 시각이 미래로 역전된 자료를 사용하지 않는다. 과거 기준시각보다 늦게 생성된 캐시도 과거 판단에 재사용하지 않는다.

GDELT 제목/URL 목록은 발견 자료다. 실제 발행사 본문, 게시시각 근거, 최초 이용 가능 시각, 원문 이용 허용과 06 enrichment 버전이 없는 레거시 자료는 요청 전 제외한다. 보강 원장의 `discovered_at`에 발견 시각을 남기고, 보강된 기사의 `received_at`·`first_available_at`은 본문 확보 시각으로 기록한다. 본문 확보 전 시점에는 분석하지 않는다. `official=True`를 만들어 통과시키지 않는다. 일반 언론 본문은 `publisher_verified`와 공식 1차 확인을 구분하며 독립 확인 수를 부풀리지 않는다. 공식 URL의 실제 짧은 발표문은 허용하되 제목만 복제한 본문은 제외한다.

제목과 본문을 별도 필드로 전송하고 추출기가 남긴 동일 제목 줄을 본문에서 제거한다. `full_content_hash`는 정규화한 전체 본문, `sent_content_hash`는 제목 제거·길이 제한 후 실제 전송 본문의 해시다. `full_char_count`·`sent_char_count`·`heading_removed`·`truncated`·전략 버전을 기록한다. 긴 본문은 문장 경계까지의 결정론적 앞부분을 사용하며 문장 중간을 잘라 부정·조건절을 숨기지 않는다. 생략한 원문을 모델이 읽었다고 간주하지 않는다.

인용은 전송한 본문 문단에서 정확히 일치해야 한다. 문단 ID/해시와 정규화 문단 기준 start/end/quote hash를 반환한다. 제목만 인용한 확정 판정, 같은 문장의 부정·루머·예정·조건부를 확정으로 바꾸는 출력은 거절한다. 이는 보수적인 코드 검증이며 모든 자연어 의미를 증명하지는 못한다. 출력의 모델 `CONFIRMED`와 공식·운영 확인 상태를 구분한다. 모델·프롬프트·원문/추출 버전 변경은 재검증하고 같은 본문을 다른 URL로 받은 경우 유료 중복을 막는다. 중복 기사에는 자신의 ID와 원래 기사 참조를 보존한다.

## 요청·응답 보관과 조회

기본은 메타데이터, 시스템 프롬프트·옵션·스키마, 해시, 정제된 결과를 보관한다. 전체 user payload와 원본 응답은 저장하지 않는다. 메타데이터의 정제 결과에는 제한된 근거 인용이 포함될 수 있다. 본문 전송 허용과 저장 권한은 운영자가 발행사별로 확인해야 한다.

`llm_debug_audit.enabled=true`는 **PAPER_LOCAL 한정** opt-in이다. 보관을 허용한 모든 기사 host와 HTTPS 이용 조건 근거를 `publishers`에 명시해야 원문이 저장된다. 06 `news_enrichment.publishers`의 원문 취득·전송 정책과 별개다. 기본 두 목록은 모두 비어 있다.

원문은 인증정보·계좌 식별자·쿠키·키 등을 마스킹한 뒤 Fernet 암호문으로 보관한다. 별도 키 파일은 POSIX 소유자 전용 0600이며 Git에 넣지 않는다. Windows ACL은 사용자 PC에서 확인해야 한다. DB 접근권한과 키·백업 보관권한도 별도로 제한한다. 암호화는 로컬 관리자 접근을 차단하는 보증이 아니다. 원문 크기는 기본 128KB 상한, 보관 기본 7일(허용 1~30일)이다.

화면에서 원문을 읽으려면 별도로 `SQ_LLM_AUDIT_ALLOW_READ=1`, 유효한 Paper 정책·비공개 키·미만료 암호문이 필요하다. 조회 함수는 저장·정리·LLM 호출을 수행하지 않는다. 감사 시작/시간별 정리와 아래 명령은 만료된 암호문만 지우며 호출·비용·주문 기록은 보존한다.

```bash
# 기본은 삭제 예정 건수 조회. --backend는 운영 설정에 맞춘다.
python -m sessionquant llm-audit-prune --backend mysql --db data/sessionquant-paper-local.db
# 운영자가 만료 암호문 정리를 실행할 때만 --apply 추가
```

기존 메인 5개 탭과 주문 버튼 동작을 유지했다. 메인은 이번 달 프로그램 추정 비용 카드 하나와 자체 예산·수동 기준 추정 잔액을 표시한다. 기존 뉴스 분석의 LLM 상세에는 최근 최대 500건의 UTC 날짜·종목/주제·모델·단계·상태 필터, 호출별 옵션/정제 결과/실패 필드, 허용한 원문 조회, legacy 사용량, 수동 메모를 둔다. 전체 조회·대사는 [SELECT 전용 SQL](../sql/llm_call_audit.sql)을 사용한다. 출처별 원문 보강→적격→요청→분류 전환율은 기록된 원문 버전 표본 기준이며 GDELT 검색 결과 전체의 비율과 구분한다. 본문 암호문과 인증정보를 SELECT하지 않는다. 08A 전체 탭 개편은 다음 단계다.

## 비용과 잔액의 의미

- **프로그램 비용**: 제공자 보고 토큰과 설정 단가로 계산한 `TOKEN_BASED_ESTIMATE`. 캐시 입력 할인도 분리한다. 토큰 보고는 실제 청구액 증명이 아니며 `provider_billed_cost_usd=null`이다.
- **미확정 비용**: usage 없는 오류/timeout은 보수적 예약액으로 `UNCONFIRMED_RESERVED_ESTIMATE`를 기록한다. 오류를 실제 비용 $0으로 표시하지 않는다. 과거 `cost_is_estimate`는 미측정 토큰 여부의 호환 플래그이며 공식 청구 확인 여부로 사용하지 않는다.
- **자체 예산**: 월 hard 한도에서 자체 정산액과 예약액을 뺀다. OpenAI 결제 시스템의 강제 한도를 설정하는 기능은 아니다.
- **수동 확인 잔액**: 마지막 확인 금액·시간 이후 프로그램 추정 비용·진행 중 예약을 차감한다. 확인 시각을 걸친 요청이나 과거 시각 미확인 자료는 추정 잔액 UNKNOWN으로 둔다. 다른 앱·조정·환불은 관측하지 못한다.
- **충전 누계**: 사용자가 적은 누적 충전 메모다. 잔액으로 환산하지 않는다.
- **공식 조직 비용**: 선택적 공식 집계 스냅샷 읽기 형식만 지원한다. 이번 단계에서 Admin Usage/Costs 수집기는 연결하지 않았다. 기본 UNKNOWN이며 실시간 선불 잔액도 조회하지 않는다. Admin 키를 전략/UI에 추가하지 않는다.

## 모델 스펙과 평가

2026-11-15 공개 공식 문서에서 아래 Chat Completions/Structured Outputs 지원과 단가를 확인했다. 계정 권한·지연·정확도·실제 청구는 확인하지 않았다. 기존 기본 primary=`gpt-4.1-mini`, escalation=`gpt-6.1-terra`를 유지한다. 품질 비교 전 모델을 자동 교체하지 않는다. `pricing_verified=false`도 유지한다.

| 모델 | 입력 / 캐시입력 / 출력 (USD, 100만 토큰) | 공식 문서 |
|---|---|---|
| gpt-4.1-mini | 0.40 / 0.10 / 1.60 | [모델](https://developers.openai.com/api/docs/models/gpt-4.1-mini) |
| gpt-6.1-terra | 1.00 / 0.05 / 5.00 | [모델](https://developers.openai.com/api/docs/models/gpt-6.1-terra) |
| gpt-6-luna | 0.10 / 0.01 / 0.50 | [모델](https://developers.openai.com/api/docs/models/gpt-6-luna) |

[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [Usage/Costs](https://developers.openai.com/api/reference/resources/organization/subresources/usage), [선불 결제](https://help.openai.com/en/articles/8264644-what-is-prepaid-billing)도 운영 시 재확인한다. 모델 출력 상한은 이번 설정 1,200토큰이며 공식 최대 상한보다 작다.

라벨 예시는 기업 22·거시 16, 총 38건의 **합성 자료**다. 실제 시장 성과나 모델 정확도로 제시하지 않는다. 기본 무료 미리보기는 라벨·구간만 검사하고 결과/품질 점수를 생성하지 않는다. 한 번에 기본 20·최대 200, 전체 파일 최대 2,000건이며 보고서를 덮어쓰지 않는다.

```bash
python -m sessionquant llm-evaluate --input examples/llm_eval.synthetic.jsonl --output work/eval-preview-1.json --offset 0 --limit 20
python -m sessionquant llm-evaluate --input examples/llm_eval.synthetic.jsonl --output work/eval-preview-2.json --offset 20 --limit 20
```

실제 `--execute`는 사용자 별도 유료 호출 승인 이후에만 실행한다. 이번 작업에서 실행하지 않았다. 유료 실행 경로는 단일 모델 단계, 별도 감사 SQLite, 호출 상한·공통 예산을 사용한다. 기업/거시별 precision/recall/F1, 악재 FN·루머 FP, 인용/검증 실패, 캐시·비용·지연을 집계한다. 합성 비시간 사례만 평가 당시로 시간을 이동하고 보고서에 표시한다. 과거/미래 시간 검증 사례는 이동하지 않는다. mock 평가 점수는 계산기 검증일 뿐 실제 모델 품질이 아니다.

## 선택적 사건 기반 금리·유가 해설

`macro_context_llm.enabled=false`가 기본이다. 켠 경우에도 일반 ETF 갱신마다 호출하지 않는다. 실시간 GOOD 데이터, 완전한 시간창·표본·원본 ID, 최근 관련 검증 본문이 필요하다. 기준 가설은 IEF 15분 0.30%, TLT 15분 0.70%, USO 30분 2.0%와 z-score 3 이상 또는 새 공식 고중요 사건이다. 최소 cooldown 60분, 사건/증거/버전 캐시, 일·월 공통 예산을 적용한다. 새로운 검증 본문이 생긴 경우에만 성공 사건의 cooldown 예외를 허용한다.

출력은 원본 시장 ID와 본문 인용을 대조한다. 생성한 bp·원유 현물 환산·원문 없는 수치를 거절한다. 일별 FRED와 장중 ETF를 분리하고 인과 확인은 false, 추가 확인 필요는 true다. 항상 `force_liquidation=false`, `entry_eligible=false`이며 거래 엔진의 위험/순위/주문에 연결하지 않는다. 수치 감시 작업은 이 선택적 LLM과 별개다.

## DB 호환과 재현 검증

스키마 6은 `llm_calls`, `llm_audit_bodies`, `macro_context_analysis` 원장만 추가한다(MySQL `pq_` 접두사). 기존 주문·기사 ID와 장부는 유지한다. SQLite user_version와 MySQL schema version을 추가 갱신한다. namespace/record hash/순번·수집시각 인덱스를 사용하고 UI는 조회 건수를 제한한다. 대규모 장기간 성능은 미측정이다. 암호문 만료 삭제는 지정 namespace에만 적용된다.

최종 전체 검사 결과는 [HANDOFF 최신 절](../HANDOFF.md)에 기록한다. 검사 명령은 다음과 같다.

```bash
python -m pytest -q
python -m ruff check sessionquant tests
python -m sessionquant doctor
git diff --check
python -m sessionquant check-news --config config/sessionquant.llm.example.yaml
```

MySQL 검사는 `PQ_TEST_MYSQL=1`과 이 세션이 만든 격리 MySQL 8.4 DB를 지정했다. 사용자 DB를 검사 대상으로 사용하지 않았다. 네트워크 생성은 전부 MockTransport·더미 키이며 실제 유료 호출·실주문은 0건이다. 동시 예약·멱등 정산·namespace·기존 주문 보존, 감사 전/후 DB 실패, timeout/429, refusal/형식/의미/근거 오류, 원문 암호화·마스킹·TTL, 캐시·미래 정보 차단, 선택 기능 기본 OFF·일반 갱신 100회 무호출, Streamlit 조회를 검증한다.

미검증 항목은 실제 계정의 모델 접근·품질·잔액/공식 비용, 허용 발행사 실약관/응답, 사용자 PC의 MySQL 마이그레이션·Windows ACL, 장기간 Paper 및 실거래 운영이다. 과거 실제 실패 응답 원본은 보존되지 않아 정확한 원인 재현을 주장하지 않는다. 현재 저장소의 CI 워크플로 부재도 남아 있다. **08/08A 및 실거래·GCP는 다음 승인 대기**다.

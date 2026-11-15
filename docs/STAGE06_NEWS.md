# 단계 06 — 뉴스 신선도·공급자 장애·원문 검증·경제 일정

`wip/stage06-handoff-20261115`의 인수인계 체크포인트 `c564da8`에서 이어서 보완했다. 06은 PAPER 수집·검증 범위다. 실거래 운영 승인, 07 LLM 재설계, 후속 UI 개편, KIS 연결, GCP 이전은 포함하지 않는다. 이전 05 완료 기준은 `9f9b1be`, 토스 시총 보완 안정 기준은 `b3010e6`이다.

## 실제 실행 경로

`run → 별도 NewsPipeline 비동기 작업 → 기업 SEC/IR RSS·Alpaca / 공식 거시 RSS·Treasury·GDELT / 공식 BLS·BEA 일정 / FRED 일별 자료 → intake → 검증 통과분만 analyzer`다. 가격 수신·Paper 체결·손절·당일 청산은 뉴스 작업 완료를 기다리지 않는다. LLM이 없어도 기존 기술 전략과 위험 관리가 계속된다.

| 작업 | 기본 주기 | 범위 |
|---|---:|---|
| 기업 | 180초 | 보유·집중 우선 최대 20 + 감시목록 배경 2 순환 |
| 거시 | 900초 | 각 공급자 독립, GDELT는 주기당 최대 2개 주제·주제당 25개 발견 메타데이터 |
| 경제 일정 | 3600초 | BLS·BEA 공식 ICS, FOMC·EIA·Treasury는 확인한 수동 입력 유지 |
| FRED | 86400초 | DGS2·DGS10·DCOILWTICO 일별 참고값 |

공급자별 시도·성공·오류·403·429·대기를 `news_provider_schedule_v1`에 보존한다. SQLite 쓰기 트랜잭션과 MySQL namespace/제어 행의 배타 잠금으로 독립 연결의 갱신을 병합하고 요청 점유를 원자 획득한다. 중단된 점유는 5분 뒤 만료되며, 이전 요청의 늦은 성공이 새 요청의 대기 상태를 지우지 않는다. 이 점유는 비정상 종료 복구 장치이며 여러 운영 엔진을 동시에 실행하라는 뜻이 아니다.

403은 최소 1시간, 429는 최소 10분을 기다리고 유효한 `Retry-After` 초/HTTP 날짜가 더 길면 존중한다. 같은 요청 안에서 자동 반복하지 않는다. RSS는 ETag/Last-Modified와 304를 지원하며 304·정상 0건·403 장애를 구분한다. GDELT 429가 다른 주제의 즉시 요청이나 재시작 폭주로 이어지지 않는다. 날짜 오류가 있는 RSS/Alpaca 기사도 제외 사유를 남긴다.

## 분석 전 경계와 기록

기업 신규 분석은 24시간, 거시는 72시간 이내다. 최근 2시간 거시를 우선하고 24~72시간은 배경 맥락으로만 보존한다. 장중 위험 판단에는 다시 사용하지 않는다. `published_at ≤ received_at ≤ 판단 시각`과 발행사·기업 연결·본문을 확인한다. 게시시각 미확인을 수신시각·GDELT seendate·수정시각으로 채우지 않는다. `NewsService`와 analyzer 양쪽에서 경계를 검사하며, 서비스가 제외 사유를 발견하면 analyzer를 호출하기 전에 UNKNOWN을 반환한다.

`news_ingestions`는 기사 ID·콘텐츠 해시·수신 시각·ticker·제외 사유별 수신 감사 기록이다. 재수신과 콘텐츠 수정을 구분하고, 동일 콘텐츠의 최초 사용 가능 시각을 DB 원자 갱신으로 보존한다. 수신 중복만으로 실패한 분석을 소진하지 않는다. 분석이 정상 완료된 콘텐츠에만 별도 완료 표시를 남겨 캐시 중복을 건너뛴다. 예산·통신 실패나 프로세스 중단 뒤에는 다음 허용 주기에 본문을 다시 확인할 수 있다.

새 MacroArticle 선택 필드가 기본값일 때 기존 05 content_hash는 바뀌지 않는다. 불변 연구 기록·기존 주문 ID·Decimal 수량은 보존한다. 기존 장부를 삭제하거나 초기화하지 않는다.

## GDELT 원문 검증

기본 발행사 허용 목록은 비어 있다. 발견 메타데이터만으로 사실을 확정하거나 원문을 임의 방문·LLM 전송하지 않는다. 발행사별 `publisher_name`, 이용 조건의 `terms_url`, 명시적인 `allow_fetch`와 `allow_llm`을 운영자가 검토한 경우에만 설정한다. 유료벽·로그인·CAPTCHA 우회는 지원하지 않는다.

HTTPS·허용 호스트·공개 IP·DNS 결과 전체를 검사하고, 검증한 IP로 직접 연결하면서 원래 Host와 TLS SNI를 유지한다. 매 리디렉션에서 주소·DNS·robots를 다시 확인하고 다른 호스트로 이동하지 않는다. robots는 가장 구체적인 사용자 에이전트/경로, 와일드카드·끝 앵커, Crawl-delay/Request-rate를 적용한다. 20초를 넘는 robots 대기는 요청을 중단한다. 네트워크·압축 해제 후 본문 모두 기본 2MB 상한이며 redirect는 최대 3회다.

trafilatura 2.3.1의 `fast` 옵션과 문서별 중복 캐시를 사용한다. 문서별 캐시는 재시작/재시도에 필요한 동일 본문을 전역 캐시가 버리지 않도록 한다. 명시된 JSON-LD/OG 게시시각, 제목·canonical·언어, 최소 300자·3문장·반복 비율을 검사한다. 본문/제목/게시시각의 수정은 새 문서 버전으로 취급한다. 리디렉션 후에도 발견 기사의 원래 ID/URL을 유지하고 최종 주소·경로는 별도 메타데이터로 남겨 완료 캐시가 연결되게 한다.

원문은 분석 중 메모리에서만 사용하고 `news_enrichments`에는 본문 대신 해시·길이·출처·HTTP/검증 상태·재시도 정보를 남긴다. 새 키 `article-enrichment-v2`는 이전 추출 정책 캐시와 구분한다. 304는 분석 완료된 캐시에만 적용한다. 미완료 분석은 조건부 헤더로 본문 회수를 막지 않는다.

## 경제 일정과 수치 맥락

공식 ICS의 UTC 또는 명시된 TZID만 사용한다. 뉴욕 DST 변환을 검증하고 중복·존재하지 않는 로컬 시각, 시간대 미확인, 잘못된 상태·수정 번호를 제외한다. 원본 UID 기반 logical_id와 일정/상태별 버전 ID를 보존한다. 같은 버전을 다시 받아도 최초 known_at을 늦추지 않는다.

수정·취소 이력을 함께 복구하고 조회 시점까지 확인한 최신 버전만 선택한다. 16시에 처음 알게 된 일정을 13시30분 판단에 쓰지 않는다. CANCELLED는 blackout을 해제하고, HIGH 중요도의 확인된 일정은 기존 전후 15분 신규 진입 제한을 유지한다. 공식 URL이라는 사실과 사건 결과 확정은 별개다. FOMC·EIA·Treasury 자동 확정 일정은 미구현이며 기존 `calendar-import`로 확인한 일정만 입력한다.

`macro_context_snapshots`는 FRED 일별 원본과 ETF를 분리한다. FRED 게시시각/point-in-time 증거가 없으면 장중 tick이나 확인된 원인으로 쓰지 않는다. ETF는 기존 `market_reference_view_v1.items` 사전 구조를 사용하고 available_at·event_at·valid_until·시간창 길이·표본 수·GOOD 품질이 맞는 창만 포함한다. ETF 수익률을 금리 bp·원유 현물로 바꾸지 않고 인과 확인은 항상 false로 남긴다.

## 점검과 화면

[설정 예시](../config/sessionquant.news.example.yaml)를 참고한다. 새 기업·거시 키가 우선이며 기존 YAML의 명시적 `news_refresh_seconds`는 없는 키의 fallback이다. 기존 `600.0`, `600.5` 같은 유한 float 주기를 보존한다. 기업 최소 180초, 거시 최소 300초, 일정 최소 3600초이며 bool·NaN·무한대는 거부한다. 기본 설정 파일도 독립 주기를 명시한다.

```text
python -m sessionquant doctor
python -m sessionquant check-news --config config/sessionquant.news.example.yaml
python -m sessionquant check-news --config config/sessionquant.news.example.yaml --connect --provider calendar
```

기본 `check-news`는 네트워크/DB 연결 없이 설정·주기·나이 제한·일정 공급자·원문 권한 기본값을 점검한다. `--connect`는 읽기 HTTP만 사용하고 LLM 생성·주문·원문 발행사 방문을 하지 않는다. 실제 연결 성공은 이 작업의 모의 HTTP 테스트로 보증하지 않는다. `run`을 실행하면 설정된 키에 따라 분석 비용이 발생할 수 있으므로 읽기 점검과 구분한다.

메인 5개 탭은 유지한다. 뉴스의 기본 기업 목록은 현재 보유·유효 집중 종목의 검증된 최근 24시간 기사만 표시하고, 과거·오류·전체 기업은 상세에 둔다. 거시 원장과 분석을 연결해 제목을 표시한다. 별도 상세 영역에 공급자별 상태·403/429·나이 구간·유료 분석 전 제외 사유·원문 검증 결과를 표시한다. 표시 통계는 최근 최대 2,000건의 수신 기준이며 고유 사건 수·유료 호출 수와 다르다. 화면 열기만으로 수집·분석하지 않는다.

## DB 진단과 검증

스키마 5는 `news_ingestions`, `news_enrichments`, `macro_context_snapshots`를 추가한다. SQLite는 user_version=5, MySQL은 `sq_schema_versions.version=5`다. MySQL 실제 테이블명은 `pq_` 접두사를 쓴다. Repository 초기화는 기존 스키마 생성/추가 동작을 수행하므로 사용자 DB 점검 전에 백업과 기존 실행 종료 등 기존 운영 절차를 따른다. 이번 개발 검사는 전용 격리 MySQL 8.4에서만 실행했다.

[진단 SQL](../sql/news_enrichment_diagnostics.sql)은 SELECT 전용이다. `db-stats`의 정확한 PAPER namespace를 확인한 뒤 모든 `REPLACE_WITH_PAPER_NAMESPACE`를 바꾸고 읽기 계정으로 실행한다. 스키마 변경·정리·주문·LLM 호출이 없으며 다른 namespace와 합산하지 않는다. JSON 시각은 원래 offset을 표시하며 SQL에서 게시시각을 추측하지 않는다.

검증 범위는 `test_stage06_intake`, `test_stage06_enrichment`, `test_stage06_regressions`, 뉴스/대시보드/설정 검사와 전체 회귀, 격리 MySQL 통합이다. 알려진 최초 실패 4건은 출처 검사를 유지한 공식 기사 fixture, 발표 전 확인한 일정 fixture와 과거 시점 차단 검사, 스키마 5/새 테이블/기존 주문 ID 검사로 수정했다. 최종 전체 검사(격리 MySQL 8.4 포함)는 1,057 passed / 0 failed / 0 skipped이며 기존 pandas 경고 8건이다. Ruff·doctor·오프라인 설정 점검도 통과했다. 상세 실행 근거는 HANDOFF.md의 최신 후속 검증을 따른다.

실제 공급자의 BLS403/GDELT429 회복·지연·권한·쿼터, 실제 발행사 약관/robots 허용 여부, 실제 계좌 검증·장기간 Paper 관측·외표본 성과는 미검증이다. CI 워크플로는 현재 저장소에 없으며 이번 검증은 로컬 실행 결과다. 07단계는 별도 승인 대기다.

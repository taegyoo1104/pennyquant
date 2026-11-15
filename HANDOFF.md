# SessionQuant 개발 인수인계

작성일: 2026-11-15 (한국시간). 저장소: `taegyoo1104/pennyquant`. Python 3.12 이상.

## 토스 과거 분봉 로컬 검사 추가

`python -m sessionquant.candle_probe --symbol AAPL --sessions 20`으로 기존 로컬 환경변수를 사용해 최근 완료 20거래일의 분봉 확보율을 직접 검사할 수 있다. [실행 안내와 한계](docs/TOSS_HISTORY_PROBE.md)를 참고한다. 인증 외에는 시세 조회만 하며 DB·주문·LLM은 사용하지 않는다. 클라우드에는 토스 키가 없으므로 실제 20일 조회 결과는 아직 미확인이다. 누락/중복/충돌/조회 한도를 보고하고, 데이터가 확보되어도 RVOL 비교 가능성이나 계좌 적격성을 VERIFIED로 변경하지 않는다.

검증: `tests/test_candle_probe.py tests/test_collectors.py` 42 passed (달력 라이브러리 경고 12건), Ruff 및 diff 검사 통과. 실제 토스 조회는 사용자 로컬 실행 결과를 기다린다.

## 2026-11-15 08A 통합 대시보드 — 최신 상태

사용자가 08A 수행을 승인했다. 시작 HEAD `bd7ce6e`, 첨부 기획서 기준이다. [08A 구현·한계·09 인계](docs/STAGE08A_DASHBOARD.md)를 따른다. 기존 단계의 승인 대기 문구는 당시 이력이다.

- 6개 화면(대시보드/시장·종목/전략·백테스트/매매 내역/뉴스·LLM/시스템·설정)으로 분리했다. 내비게이션은 radio 기반 지연 렌더링이며 메인만 5초 갱신한다. 메인은 4카드·최대5 보유/체결/관련 뉴스다. 세부 표·전체 계좌·LLM/오류는 상세로 이동했다.
- H/F·뉴스는 동일 DB 스냅샷으로 읽고, 활성 장부의 H와 유효한 현재 집중 F를 합친다. 뉴스의 정확한 원본 ID·명시적 종목·검증된 최근 24시간 발표만 사용하고 기사 ID 중복을 제거한다. Paper/실계좌를 합산하지 않는다. 수집/집중 미확인은 별도 안내다.
- 매매 시작/일괄 종료/관리 종목 매도는 실행별 확인→기존 큐→엔진 ACK/체결 경로다. `request_command`가 update_control 잠금 아래 환경/실행/armed/heartbeat·관리 수량·중복 매도를 검사한다. 복수 연결 멱등 요청과 실제 주문 0건을 검증한다. 실제 계좌 조회 전용 화면은 readonly, LIVE 개별 매도는 관리 출처 미연결로 차단한다.
- Paper 매수 가능액은 cash−reserved. 시세 만료·평균 매수가 미확인은 N/A이며 매수가를 현재가로 대입하지 않는다. 오늘 순실현손익은 일별 완전성 증거가 없어 N/A이고 누적값으로 대체하지 않는다. LLM 월 추정 사용/자체 예산 잔여는 메인, 수동 크레딧/모델·공식 비용은 상세다.
- 메인 조회는 각 300건 상한이며 큰 이력의 관련 기사 누락 가능성을 명시한다. 상세 2,000건/CSV, 전체 기록은 record-export다. 소규모 격리 SQLite의 메인 조회·뷰 20회 중앙값0.95ms/최대42.0ms다. 대용량 MySQL/네트워크 SLA는 미검증이다.
- 새 08A 회귀22건, MySQL 연결 간 멱등성1건. 기존 01/02/06/07/08 UI 검사는 새 화면 이동 후 원래 조건을 검사한다. UI·제어 집중114건, 추가 미확인/조회 전용 집중28건 통과. Ruff/doctor(`live_orders_enabled=false`)/diff 검사 통과.
- 헤드리스 Chromium으로 빈 화면/명시적 합성 DB, 1,440px/390px, 여섯 화면을 확인했다. 오류0, 카드4, 메뉴6, 모바일 scrollWidth=viewportWidth=390. 공유 데스크톱 도구 대신 반복 가능한 로컬 브라우저를 사용했다.

최종 검증: 새 CI 잠금 가상환경(Python3.12.14), umask022, 외부 연결 차단, 별도 MySQL8.4에서 `SQ_TEST_OFFLINE=1 PQ_TEST_MYSQL=1 ... python -m pytest -q` → **1,195 passed, 0 failed, 0 skipped, 8 warnings (197.67초)**. 전용 MySQL22건 포함. Ruff·pip check·doctor·문서 링크·변경 파일 비밀정보·diff 검사 통과. 원격 CI는 해당 구현 커밋의 Actions 체크에서 확인하며 기본 job의 MySQL22 skip은 통과 수와 구분한다. 기존 pandas 시간대 경고8건은 기능 실패와 구분한다.

실주문·유료 LLM·사용자 DB 접근은 0건이다. 로컬 우선·향후 GCP·KIS 제외를 유지한다. 09 개발용 기존 큐 인터페이스는 정리했지만 실거래/10 운영 승인은 보류한다. 일별 완전성 집계·전체 이력 페이징/관련 기사 SQL·LIVE 관리 포지션 provenance는 후속 보완 대상이다. 실제 공급자 자료·장기간/대용량 운영·Windows ACL/실기기 사용은 미검증이다.

## 2026-11-15 00~08 종합 검토 — 이전 검증 이력

사용자가 08 종료 후 기존 작업 전체의 문제점·보완점 검토와 수정 가능한 사항의 보완을 승인했다. 08 구현 커밋은 `8a328e7`이며 아래의 08 결과를 유지하고 후속 검토를 분리했다. [00~08 종합 검토](docs/REVIEW_00_08.md)에 단계별 코드·회귀 근거, 재현한 결함과 남은 실사용 확인을 정리했다.

- **P1 시계/일일 손실:** 과거 거래일로 전환한 뒤 오늘로 복귀하면 손실 중지가 초기화되는 것을 재현했다. `PaperBroker.roll_session`은 과거 거래일을 거절한다. `SessionEngine`은 수신 커서와 별도 `last_decision_at`을 저장·복구해 빈 tick의 과거 처리도 변경 전에 차단한다. 원래의 오래된 수신 자료는 현재 처리 시각으로 안전하게 평가할 수 있다.
- **P2 LLM 비용:** 05 보고서가 단가 확인+측정 토큰을 공식 청구처럼 합산할 수 있었다. `research/costs.py`에서 프로그램 추정과 명시적 청구 대사를 구분하고 호출 ID별 개정 중복 합산을 막았다. 기존 검증 청구 키는 유지하되 실제 증거가 없으면 null, 추정 합계는 새 키로 제공한다. 원래 사용량을 삭제하거나 의미를 소급 변경하지 않는다.
- **P2 CI:** 누락된 `.github/workflows/ci.yml`을 추가했다. Python3.12, 해시 잠금 설치/import/CLI/doctor/Ruff/pytest, 선택적 임시 MySQL이다. contents:read·checkout 인증 유지 OFF·공식 action SHA 고정·운영 secrets 미사용·테스트 외부 연결 차단을 적용했다. `requirements-dev.lock` 66개는 기존 운영 잠금 59개 버전을 유지한다.
- **P2 설정 권한:** `dc8eef5`의 첫 원격 CI가 기존 `.env` 0644 권한 보존 문제를 발견했다(1 failed, 1,148 passed, 21 skipped). POSIX에서 새 값을 쓰기 전 0600을 적용하고 권한 변경 실패 시 미기록하도록 보완했다. 입력 권한을 명시한 회귀와 매 재작성 전후·실패 경로를 추가했다. 로컬 umask 077 의존을 제거하고 umask 022에서 재검증했다. Windows ACL은 별도 확인 대상이다.
- **문서:** 기존 STRATEGIES/SYSTEM_RUN의 PennyQuant 레거시 표식과 현재 실행 링크를 보완했다. 거래량·호가 bool 검사는 기존 코드에서 정상 거절을 확인해 변경하지 않았다.
- 시계 가드가 드러낸 기존 위험 테스트의 과거 이동 fixture는 진입 시간까지의 테이프로 분리했다. UNKNOWN 차단·대기 매수 취소·청산 검증은 유지한다.

최종 로컬 검증(새 CI 잠금 가상환경, Python3.12.14, umask 022):

- `SQ_TEST_OFFLINE=1 PQ_TEST_MYSQL=1 ... python -m pytest -q` → **1,172 passed, 0 failed, 0 skipped, 8 warnings (191.12초)**. 전용 MySQL8.4 21건 포함. 기존 pandas 시간대 경고 8건을 기능 실패와 구분한다.
- 앞선 보완 집중 80 passed, 외부 연결 차단 집중 78 passed, 새 잠금 시계·위험 집중 39 passed, umask 022 권한·시계·비용 집중 19 passed. `requirements-dev.lock`의 해시 설치·editable 설치·pip check·doctor, CI YAML/권한/선택 job/SHA 정적 확인, Ruff·CLI·문서 링크·diff 검사 통과.
- 원격 CI는 이 후속 커밋의 Actions 체크에서 확인한다. 기본 job의 MySQL skip은 통과 수에 포함하지 않으며 선택 job은 폐기 가능한 서비스만 사용한다.

**실주문·유료 LLM·사용자 DB 접근 0건.** 실제 토스 권한·분할/RVOL 근거·섹터 전체 커버리지·지연/성능·발행사 조건·장기 실제 기록/외표본·실비/모델 품질은 미검증이다. 실제 기록이 없어 08 실데이터 가점 비교는 미실행이며 합성 결과로 수익성을 주장하지 않는다. 다음 08A·09/10·GCP와 유료 평가·실거래 활성화는 별도 승인 대기, KIS 제외 상태다.

## 2026-11-15 08단계 — 구현 완료 기준

사용자가 첨부한 `08_종목순위_뉴스_포트폴리오.txt`의 수행을 승인했다. 시작 HEAD `329ea7a`에서 이어서 구현했다. 세부 작업 기준은 단계 첨부이며 일반 공유 대화는 배경 참고다. 운영 순서는 초기 로컬 PC/MySQL → 이후 별도 GCP 이전, KIS 취소를 유지한다. 최신 절은 [08 운영 안내](docs/STAGE08_CANDIDATES.md), [설정 예시](config/sessionquant.candidates.example.yaml), [SELECT 전용 진단](sql/portfolio_candidate_diagnostics.sql)을 따른다.

- `PortfolioCandidateSelector`가 새 후보 호가 수신 시 전체 집중 목록을 같은 평가 시각으로 비교한다. 현재 완료 5분 경계·호가 신선도/정렬과 기존 적격성·RVOL·감시/뉴스/시장 게이트를 검사하며 순위로 우회하지 않는다.
- 검증된 기업 사실의 가점과 매수 차단 위험을 분리했다. 07 분석의 출처·본문 인용·시각·모델·채택 호출을 확인하고 미확인/루머/실패/과거는 UNKNOWN·가점 없음이다. 검증된 중립 0.5와 구분하며 거시 LLM 해설을 종목 점수로 바꾸지 않는다.
- 업종 상대강도는 당시 S&P 구성의 완전한 동일 업종(최소 2종목)과 SPY의 동일 15분 구간·출처·기업행동/조정 비교 근거로 계산한다. 불완전한 일부 종목·null 업종·미래/혼합/비교 근거 부족은 UNKNOWN이다. 새 섹터 ETF 수집은 추가하지 않았다.
- 보유+매수 예약 2종목 중심, hard 5종목·20/20/40/80%·위험0.75%/일일2%를 구분한다. 권장 수 초과 허용·동일 업종·높은 SPY 상관 중복 제한은 명시적 옵션이다. 분산 제외 후보는 최종 검사 한도를 소진하지 않는다. 기존 추가매수·비용·손절/청산·수동 제어를 유지한다.
- 참고 ETF의 BUY를 PortfolioManager에서도 차단한다. 04 필터 OFF/shadow는 영향 0, 오프라인 합성 ON의 위험/UNKNOWN 차단은 점수와 분리했다. 보유 청산은 먼저 처리하며 빈 위험 tick에 전체 후보 평가를 반복하지 않는다.
- 기존 `candidates` ID와 schema6을 유지한다. `pipeline_status(kind=portfolio_selection)`와 control에 선정 프레임을 추가하고 주문 의도까지 selection ID·순위·점수·거절/채택 이유를 연결했다. 기존 정책이 저장된 실행에서 옵션을 바꾸려면 새 DB/namespace를 사용한다.
- 기존 메인 5개 탭의 종목 현황에 최종 후보·신규/추가 승인·사유·입력 시각·중복 노출을 표시한다. 호가·자격·집중·비교 근거의 만료를 반영하고 오래된 기록은 현재 순위로 표시하지 않는다. 08A 재배치는 하지 않았다.
- 기록에 당시 집중 범위·뉴스 가점·선정/설정을 포함하고 `evaluate-candidates`로 동일 묶음의 무가점/기업뉴스/업종/결합 4조합을 비교한다. 유니버스의 실제 기록 이용 가능 시각을 보존하며 재생 선정은 `source_selection_id`로 원본과 연결한다. 같은 묶음의 두 재생에서 주문·선정 결과가 동일하다.

최종 검증: 전용 MySQL 8.4를 지정한 `PQ_TEST_MYSQL=1 ... python -m pytest -q` → **1,164 passed, 0 failed, 0 skipped, 8 warnings (197.55초)**. 08 집중 52건과 MySQL 통합 21건·SQL 실행을 포함한다. Ruff·doctor·의존성 호환·새 예시 오프라인 check-news·무료 비교 CLI·문서 링크·형식 검사 통과. 경고 8건은 기존 pandas 시간대 변환 경고다. 실제 주문·유료 LLM·사용자 DB 접근은 0건이다.

실제 시장 기록이 제공되지 않아 **실데이터의 가점 적용 전/후 성과 비교는 미실행**이다. 합성 비교는 진단 경로만 검증했다. 실제 토스 계좌 적격성·기업행동/RVOL 근거, 업종 전체 커버리지·지연·처리 성능, 장기간 Paper/외표본·실비는 미검증이다. 자료를 임의 생성해 해당 조건을 통과시키지 않았다.

사용자는 08 종료 후 **지금까지의 00~08 전체 검토와 수정 가능한 문제 보완**도 승인했다. 후속 검토는 08 구현과 분리해 기록한다. 08A·09/10·GCP 및 유료 모델 평가·실주문 활성화는 계속 별도 승인 대기다.

## 2026-11-15 07단계 — 이전 검증 이력

사용자가 과거 대화와 16개 단계별 첨부를 다시 제공하고 **07 수행**을 승인했다. 06 완료 커밋 `0fa5ad4`에서 이어서 작업했다. [프로젝트 방향](docs/PROJECT_DIRECTION.md)에 토스 기반 S&P 500 장중 추세·100→20·로컬 MySQL/Paper 우선과 **KIS 취소**를 반영했다. 첫 공유 대화는 공개 본문을 확인했다. 두 번째 기획 링크는 최초 503/timeout 이후 사용자가 다시 공유한 뒤 공개 공유 데이터 경로에서 본문을 확보해 대조했다.

07 완료 커밋 `75f1a27` 이후 후속 확인: 기획 대화의 06·07 개정과 사건 기반 LLM·입출력/비용 감사 방향은 현재 구현과 일치한다. 사용자가 **초기에는 로컬에서 실행하고 나중에 GCP로 이전**한다고 재확인했다. 또한 **일반 대화는 프로젝트 개요 참고이고 실제 작업 계획은 첨부한 단계별 기획서가 기준**이라고 명시했다. 대화의 예시·옛 지시문으로 첨부 계획을 덮어쓰지 않는다. GCP 이전·실거래 활성화와 단계별 승인은 구분한다. 이 후속 변경은 방향·근거 문서만 보완하며 코드·설정·주문 동작은 변경하지 않았다. 기존 대화 전문·접속정보는 저장소에 추가하지 않는다.

최신 구현·운영 절차는 [07 LLM 감사·근거·비용](docs/STAGE07_LLM.md), [설정 예시](config/sessionquant.llm.example.yaml), [SELECT 전용 호출 진단](sql/llm_call_audit.sql)을 따른다.

- 공통 `llm/BudgetClient`에서 실제 HTTP 시도별 UUID, 기사/버전/논리 분석/모델/단계/요청·응답 해시·사용량·최종 채택을 연결했다. 사전 제외/캐시/예산 차단은 실제 호출과 별도 기록하고 과거 매핑 불가 사용량은 보존한다.
- timeout·거부·불완전 응답·JSON·스키마·인용·의미 오류를 구분했다. 요청 전 감사 실패는 무전송, 요청 후 정산 실패는 UNKNOWN/예약 유지다. 독립 연결의 동시 예산 예약·멱등 정산, 실패한 별도 escalation 비용을 검사했다.
- 06 GDELT 원문·게시시각·권한·버전 계약을 필수화했다. 발견 시각과 나중에 본문을 확보한 이용 가능 시각을 분리한다. 제목을 본문 근거로 재사용하지 않고 실제 전송 본문의 문장 경계·문단 ID/해시/인용 위치를 검증한다. 부정·루머·예정을 확정으로 바꾸지 않는다. 원문/모델/프롬프트 변경 및 동일 본문 중복·미래 캐시를 구분한다.
- 기본 메타데이터 감사와 선택적 Paper 원문 감사를 분리했다. 원문은 명시적 발행사 보관 정책·비밀정보 마스킹·Fernet·비공개 키·기본 7일 TTL·별도 로컬 읽기 권한을 적용한다. `llm-audit-prune` 기본은 미리보기이며 지정 namespace의 만료 암호문만 정리한다.
- 프로그램 토큰 기반 추정 비용·자체 예산·공식 조직 비용 UNKNOWN·수동 확인 시각 기반 추정 잔액·충전 누계를 구분했다. usage 없는 오류를 실제 비용 0으로 만들지 않는다. Admin 키/실시간 잔액/공식 비용 수집기는 연결하지 않았다.
- 기존 메인 5개 탭을 보존하고 비용 카드 한 개와 LLM 상세 감사/필터/수동 메모를 연결했다. 기존 뉴스 화면의 title 전달도 보완했다. 08A 전체 개편과 주문 제어 변경은 하지 않았다.
- 기업 22/거시 16의 합성 평가 자료와 분할 무료 미리보기, precision/recall/F1·악재 FN/루머 FP·검증/근거·캐시/비용/지연 계산을 추가했다. 실제 `llm-evaluate --execute`는 실행하지 않았다. mock 평가 경로만 검증했다.
- 금리·유가 사건 기반 맥락 LLM은 기본 OFF다. 검증된 실시간 시간창·최근 관련 본문·새 사건·60분 cooldown·공통 예산을 적용하며, 출력은 항상 참고/추가 검증 상태이고 자동 주문·강제 청산으로 연결하지 않는다.
- SQLite/MySQL 스키마 6에 감사/암호문/맥락 원장을 추가했다. 기존 주문 ID·기사 ID·장부·PAPER/LIVE namespace를 유지한다. 사용자 DB는 열거나 변경하지 않았다.

최종 검증(2026-11-15, Python 3.12.14):

- 이 세션이 만든 전용 MySQL 8.4 DB를 지정한 `PQ_TEST_MYSQL=1 ... python -m pytest -q` → **1,111 passed, 0 failed, 0 skipped, 8 warnings (177.98초)**. MySQL 통합 20건 포함. 출처별 원문 버전 전환율 및 호출 연결 진단 SQL도 실제 격리 MySQL에서 실행했다.
- `python -m ruff check sessionquant tests`, `python -m sessionquant doctor`, 의존성 호환 검사 통과. doctor: `live_orders_enabled=false`.
- 기본/새 예시 설정의 오프라인 `check-news`, 무료 평가 미리보기 20+18건, 감사 정리 dry-run, editable 설치·문서 링크·변경 파일 비밀정보·`git diff --check` 검사 통과.
- 경고 8건은 기존 exchange-calendars/pandas 시간대 변환 관련 Pandas4Warning이다.

실제 토스/뉴스/LLM 계정·사용자 DB 접근, 실주문·유료 LLM 생성은 **0건**이다. 공개 모델 스펙·단가 확인과 실제 계정 권한·품질·청구를 구분한다. 실제 공급자·발행사 정책·잔액/공식 비용·사용자 PC Windows ACL·장기간 Paper/실거래는 미검증이다. 과거 원본 응답 미보존으로 기존 실제 오류를 정확히 복원하지 못한다. CI 워크플로 부재도 남아 있다.

**07 구현·격리 검증 범위는 완료했다. 08/08A·09/10·GCP는 다음 승인 대기다. KIS는 취소 상태를 유지한다.** 아래 06 및 최초 체크포인트의 승인 대기·실패 수치는 당시 이력이며 최신 상태는 이 절이 우선한다.

## 2026-11-15 06 후속 작업 — 이전 검증 이력

요청한 WIP 브랜치의 `c564da8`에서 이어서 06 미완료 항목을 보완했다. 아래의 원래 체크포인트 실패 기록은 개발 이력이며 현재 미해결 목록이 아니다. 최신 구현과 운영 절차는 [docs/STAGE06_NEWS.md](docs/STAGE06_NEWS.md)를 따른다.

- 기존 실패 4건: 시간 검사를 완화하지 않고 발표 전/후 확인 fixture를 분리했다. 크레딧 부족 검사는 검증된 공식 기사로 mock OpenAI 429에 도달하도록 고쳤다. 스키마 5·신규 테이블·기존 주문 ID 보존을 함께 확인한다.
- `NewsService`가 intake 제외 사유를 찾으면 analyzer 호출 전 UNKNOWN을 반환한다. 모의 analyzer 호출 0건을 검증했다.
- 공급자 상태와 최초 수신 제어 값을 DB에서 원자 갱신한다. MySQL 공유/배타 잠금 전환 교착을 피하고 독립 연결의 동시 요청 점유·카운터·namespace 격리를 검사했다. 늦은 이전 요청 결과가 새 cooldown을 지우지 않는다.
- GDELT는 15분 최소 간격·최대 2개 주제·순환 커서를 복구한다. Retry-After 초/HTTP 날짜, 403/429, RSS304와 정상 0건을 구분한다.
- 원문 검증은 `fast`와 문서별 trafilatura 중복 캐시를 사용한다. 분석 미완료 콘텐츠 재시도, 완료 캐시304, 제목/게시시각 수정 버전, DNS pinning·압축 상한, 리디렉션 시 원래 기사 ID/완료 캐시 보존·robots 재검사·구체 경로 우선·재시작 대기를 검증했다. 본문 메모리 사용/해시 저장과 기본 빈 발행사 정책은 유지한다.
- 경제 일정 상태·수정 번호·UTC/로컬 시간을 검증하고, 수정/취소 버전과 최초 known_at을 보존해 당시 확인된 버전으로만 blackout을 판단한다. FRED 시각 역전/미래 관측과 ETF dict의 available_at·valid_until·시간창/표본 수 오류를 제외한다.
- 기존 메인 5개 탭 안에 현재 보유/유효 집중 종목의 최신 검증 기업 뉴스, 전체·과거·오류 상세, 공급자/나이 구간/원문 검증 정보를 연결했다. Streamlit 빈 화면·기록이 있는 화면을 모두 검사했다.
- `check-news`의 독립 주기·게시 나이·원문 권한 기본값·경제 일정 점검과 `--provider calendar`를 추가했다. 생성·주문 없이 GET만 수행하는 테스트를 추가했다.
- [설정 예시](config/sessionquant.news.example.yaml), [SELECT 전용 MySQL 진단](sql/news_enrichment_diagnostics.sql), 06 운영 문서를 추가했다. 기존 float 주기 fallback과 새 키 우선순위를 검사했다.

최종 검증(2026-11-15, Python 3.12.14):

- 전용 로컬 MySQL 8.4를 지정한 `PQ_TEST_MYSQL=1 ... python -m pytest -q` → **1,057 passed, 0 failed, 0 skipped, 8 warnings (176.61초)**. 기존 MySQL 통합과 새 06 검사 17건을 포함한다.
- `python -m ruff check sessionquant tests`, `python -m sessionquant doctor`, `git diff --check` 통과. doctor의 실주문 활성화는 false다.
- 기본/새 예시 YAML의 오프라인 `check-news`, 의존성 호환 검사, 새 운영 문서/인수인계의 로컬 링크 검사 통과.
- 경고 8건은 기존 exchange-calendars/pandas 시간대 변환 관련 Pandas4Warning이다. trafilatura no_fallback 폐기 경고는 해소했다.

아래 원래 수치는 c564da8 당시의 이력이다. 후속 변경은 같은 WIP 브랜치에 저장하며 main 안정 기준과 구분한다.

실제 토스/뉴스/LLM 계정·사용자 DB에는 연결하지 않았고 **실주문·유료 LLM 생성 0건**이다. MySQL은 이 세션에서 만든 로컬 전용 8.4 컨테이너의 격리 DB/임의 namespace만 사용했다. 실제 공급자 권한·지연·쿼터·장기간 Paper 운영, 발행사별 약관/robots의 실응답은 미검증이다. FOMC/EIA/Treasury 자동 확정 일정은 수동 import 경로를 유지한다. 현재 저장소에 CI 워크플로가 없어 이번 검증은 로컬 실행 결과다.

**06 구현 보완 및 격리 회귀 검증 범위로 보고한다. 실거래 운영 승인을 뜻하지 않는다. 07 이후·KIS·후속 UI 개편·GCP는 별도 승인 대기다.**

## 원래 체크포인트(c564da8)에서 가장 먼저 읽을 사항

main 안정 기준은 `b3010e6`(토스 시총 선정), 그 이전 05단계 기준은 `9f9b1be`다. **c564da8의 06은 테스트 실패가 남은 개발 체크포인트였다.** 후속 수정·검증 상태는 위 최신 절을 따른다. 실거래 활성화는 승인하지 않았다. 기존 AGENTS.md를 발견하면 먼저 읽고 보존한다. 이번 작업에서는 AGENTS.md를 추가·수정하지 않았다. 이전 대화의 첨부 문서는 새 계정에 자동 전달되지 않으므로 필요하면 사용자가 다시 제공해야 한다.

사용자는 00~05 구현 후 토스 시총 100종목 연결 및 단계 작업 계속을 승인했다. 현재 작업 범위를 06까지로 알렸으며 **07 이후는 별도 승인 대기**다. KIS는 적합성 검토만 진행했고 사용자가 토스로 충분하면 KIS를 쓰지 않겠다고 하여 보류했다. KIS 주문·데이터 연결을 임의로 추가하지 않는다.

## 최초 기획 의도와 발전 과정

최초 PennyQuant는 미국 페니스탁 뉴스 호재 기반 초단기 매매였다. SessionQuant 2.0은 S&P 500 장중 추세 매매로 전환한다. 기업 뉴스 없이도 기술적 조건으로 후보가 될 수 있다. LLM은 사실·위험 분석을 보조하고 주문을 직접 결정하지 않는다. 시장이 불리하거나 데이터가 불확실하면 현금 유지가 정상 동작이다.

5분·15분 확정봉, Pullback 기본 전략과 momentum/gap_breakout/vwap, SPY·QQQ 시장 국면, Decimal 소수점 주문 모의, 최초 비중 20%, 추가매수 1회, 종목 최대 40%, 전체 80%, 최대 보유 5개(초기 권장 2개), 종목 위험 0.75%, 일일 손실 2%가 기본 가설이다. 목표 익절 10%를 반드시 기다리는 구조가 아니며 악재·추세 하락·손절·시간 청산이 먼저 작동할 수 있다. 당일 청산과 마감 직전 불필요한 신규 매수 방지가 중요하다.

개발은 로컬 MySQL 8 + Paper, 이후 명시적 승인에 따른 토스 소액 실거래, 마지막 GCP 이전 순서다. 토스는 주문·계좌·체결·청산 경로를 유지한다. 실거래 기능 존재와 실제 주문 권한 검증·운영 승인은 별개다.

## 구조와 실제 연결 경로

- `sessionquant/cli.py`, `__main__.py`: CLI. `python -m sessionquant --help`로 실제 옵션을 확인한다.
- `session_config.py`, `session_runtime.py`, `session_engine.py`: PAPER 설정, 독립 비동기 수집 작업, 전략·위험 평가 및 재시작 복구.
- `market/`, `market_reference/`: 봉·거래 적격성·RVOL·ETF 원본/지표·위험 필터. ETF를 금리 bp 또는 원유 현물로 환산하지 않는다.
- `watchlist/`: 시총 최대 100 → 집중 최대 20, 시점별 선정 이력·공정 조회·호출 예산.
- `collectors/`: 토스 REST/WS, Alpaca 보조 뉴스, SEC/공식 RSS 등.
- `news/`, `macro/`: 기업/거시 분석, 예산 원장, 수집·검증·경제 일정. 06 신규 파일은 아래 참조.
- `portfolio/`, `brokers/` 등 주문 관련 모듈: Paper 체결·비중·위험·청산 및 실거래 안전 경로. 정확한 폴더는 현재 트리를 기준으로 탐색한다.
- `research/`: 실제 수신/처리 시점의 불변 기록, 데이터 동결·재생·정책 비교·백테스트. 미래 정보 유입 금지.
- `database/`: SQLite 호환과 MySQL 8 JSON 원장/DECIMAL 투영/namespace/트랜잭션/잠금.
- `dashboard/session_app.py`: Streamlit 기존 5개 탭과 실제 계좌·거래 제어. 자동 기동과 매매 시작을 구분한다.
- `config/`, `examples/`, `docs/`, `tests/`: 실행 예시·운영 문서·격리 테스트. `pennyquant/`는 과거 호환 경로다.

## 단계별 현황

| 단계 | 상태 및 근거 |
|---|---|
| 00 기준선·문서·CI | 종합 검토에서 CI 구성·잠금 설치·로컬 동등 회귀 및 레거시 표식 보완 |
| 01 시총100/집중20 | 구현 및 토스 보완 완료. `docs/STAGE01_WATCHLIST.md` |
| 02 시장데이터·적격성·RVOL | 기존 구현/개정 보완 유지. `docs/STAGE02_MARKET_DATA.md` |
| 03 Paper 시장가 체결 현실화 | 기존 구현 유지. `docs/STAGE03_PAPER_FILLS.md` |
| 04 전략·위험·청산 | 기존 개정 보완 유지. `docs/STAGE04_STRATEGY_RISK_EXIT.md` |
| 05 실데이터 기록·백테스트 | `9f9b1be` 완료 기준. `docs/STAGE05_RECORDING_BACKTEST.md` |
| 06 뉴스 신선도·BLS403·GDELT429·경제 일정 | **후속 구현 보완·격리 회귀 검증 완료 / 실공급자·운영 미검증**. 최신 절 참조 |
| 07 LLM 분류·비용·크레딧 | **사용자 승인 후 구현·격리 회귀 검증 완료 / 실제 모델·계정·운영 미검증**. 최신 절 참조 |
| 08 종목순위·뉴스·포트폴리오 | **사용자 승인 후 구현·격리 검증. 실제 시장 기록 효과 비교는 미검증**. 최신 절 참조 |
| 08A UI·제어 | 사용자 승인 후 6개 화면·기존 큐 제어 구현·격리 검증. 최신 절 참조 |
| 09 공통주문·실거래 연결 / 10 로컬 검증·운영승인 | 신규 기획 구현 미승인. 과거 Phase5 기능과 단계 번호를 혼동하지 말 것 |

전체 방향은 `docs/SESSIONQUANT_CURRENT.md`, `SESSIONQUANT.md`, `PHASES.md`, `REVISION_00_04_AUDIT.md`도 참조한다. 이전 기획서 본문·단계 06~10 상세 첨부는 저장소 문서만으로 완전히 대체되지 않는다. 사용자에게 원본 첨부 재제공을 요청할 수 있다.

## 토스 시총 결정 — 완료 커밋 b3010e6

공식 OpenAPI 1.2.28의 `GET /api/v1/sectors`, `GET /api/v1/sectors/{sectorId}/stocks?marketCountry=US`를 사용한다. 대분류 응답은 하위 섹터 종목을 포함한다. 미국 시총도 KRW 환산·일별 갱신이다. 같은 통화로 순위를 계산하며 USD·환율·발행사 총액·정확한 평가 시각을 추측하지 않는다. GOOG/GOOGL은 별도 티커다. 100종목이 100개 발행사라는 뜻은 아니다.

편입 원장 전체 커버리지, 양의 Decimal, 중복·충돌·통화·동일 뉴욕 날짜·15분 이내 수집을 검사한다. 유효기간 24시간. `provider_as_of_at=null`, `as_of_at`은 수집 시작이라는 의미를 명시한다. 부분 자료는 PARTIAL로 신규 진입을 차단한다. 이전 정상 목록을 부실한 새 목록으로 덮어쓰지 않는다.

`market_cap_provider: auto`는 기존 파일이 있으면 파일, 없고 source=toss면 토스다. 명시적 `toss`/`file` 선택 가능. 기존 USD issuer_total 파일 계약은 보존했다. 런타임은 위험 호가 전달 뒤 공유 클라이언트·읽기 예산으로 주기당 섹터 요청 하나를 진행하고 중간 상태를 DB에 저장한다. 별도 인증 클라이언트를 만들지 않는다. `watchlist-refresh` 단독 실행은 엔진·계좌 조회기를 종료한 뒤 사용한다. 실제 계정의 커버리지·응답 지연·쿼터는 미검증이다.

검증: 전체 943 passed / 14 skipped / 기존 pandas 경고 8건. 별도 격리 MySQL 14 passed. Ruff·doctor 통과. 이 커밋은 원격 main에 업로드됐다.

## 원래 체크포인트의 06 변경

- `news/intake.py`: 기업 24h/거시72h 시간 검사, 출처·티커·본문 오류 구분, 수신/콘텐츠 버전별 감사 기록.
- `news/schedule.py`: 공급자별 주기·실패·Retry-After·재시작 상태. 여러 인스턴스가 공유 제어 원장을 덮어쓰지 않도록 병합한다.
- `news/enrichment.py`: 허용 발행사 정책, robots, URL/DNS/공개 IP 검사, IP 직접 연결 + Host/TLS SNI, 리디렉션 재검사, 크기/압축 상한, trafilatura 2.3.1 본문 추출, 명시적 게시시각·제목·canonical·언어·본문 품질 확인. 기본 발행사 목록은 비어 있어 임의 원문 방문·LLM 전송을 하지 않는다. 본문은 기본적으로 메모리만 사용하고 메타데이터/해시를 저장한다.
- `macro/calendar.py`: 공식 BLS/BEA ICS, UTC/뉴욕/DST, 수정·취소·원본 UID 이력. FOMC/EIA/Treasury 자동 확정 일정은 미구현이며 기존 calendar-import 수동 경로를 유지한다.
- `macro/context.py`: FRED 일별 자료와 ETF 지표 분리, 미래·시간창 불일치 제외. FRED 게시 시각 미확인은 장중 tick/확정 원인으로 사용하지 않는다.
- Alpaca: desc 및 명시적 조회 범위, 무료 최근15분 제외 요청, 반복 페이지 토큰 제한, 잘못된 기사 메타데이터 보존 경로. 실제 응답의 created_at 최신성은 별도로 검사한다.
- RSS: 조건부 요청/304, 공급자 대기·상태, 잘못된 날짜 기록.
- `macro/service.py`: 기업180/거시900/일정3600/FRED86400초 독립 주기, 우선20 + 배경2 순환, 수집/분석 경계. 24~72h는 배경이며 장중 위험 신호로 재사용하지 않는다.
- DB 스키마 버전5 추가: `news_ingestions`, `news_enrichments`, `macro_context_snapshots`. 사용자 DB에서 실행하지 않았다. 기존 ID·주문 기록 초기화 없음.
- `requirements.txt`, `requirements.lock`, `pyproject.toml`: trafilatura>=2.3,<3 추가. 공식 문서 기준 Apache2.0.

## 원래 체크포인트에서 발견한 문제와 후속 작업 (처리 내역은 최신 절 참조)

1. **검증 전 06 완료 처리 금지라는 당시 지시.** 최종 전체 테스트 로그 결과와 아래 문제부터 검토한다. 기존 안정 커밋과 WIP를 구분한다.
2. 마지막 집중 테스트: **121 passed, 1 failed**. `test_calendar_dst_cancel_and_unknown_timezone`: fixture는 16:00에 처음 알게 된 일정을 13:30 위험 판단에 사용하려 하여 실패한다. `known_at <= now` 검사를 완화하지 말고, 발표 전에 확인한 별도 fixture로 수정하고 늦게 확인한 일정은 과거 blackout에 사용되지 않는 테스트를 추가한다.
3. `NewsService._analyze`는 intake 이유를 기록하지만 여전히 analyzer를 호출한다. 회사별 max_age 설정이 내부 analyzer와 달라지면 차단되지 않을 수 있다. **이유가 있으면 유료 분석 호출 전에 명시적으로 UNKNOWN 결과를 반환**하도록 보완하고 mock 호출 0건을 검증한다.
4. `NewsSchedule` 공유 상태 병합·동시성, Retry-After/쿨다운, GDELT 토픽 예산, 원문 cache 재시작/수정 기사, DNS pinning/압축 상한/robots pacing을 추가 리뷰한다. 보안 경계를 완화해 성공률을 올리지 않는다.
5. trafilatura `no_fallback` 폐기 경고가 있다. 최신 문서의 `fast` 옵션으로 변경 시 추출 fixture 회귀를 확인한다.
6. 대시보드에 06 공급자 상태·오류·나이 구간·원문 검증 결과가 아직 연결되지 않았다. 기존 메인 5개 탭을 보존하고, 메인 뉴스는 보유·집중 종목의 검증된 최신 기업 뉴스만 표시한다. 전체 거시/과거/오류는 상세 뉴스 영역에 둔다.
7. `check-news`의 새 주기/경제일정 검사는 아직 보완하지 않았다. 읽기 검사 자체가 유료 LLM 생성·주문을 하지 않아야 한다.
8. `sql/news_enrichment_diagnostics.sql`과 `docs/STAGE06_NEWS.md`가 아직 없다. 실제 테이블/namespace 기준 SELECT 전용 SQL 및 운영 안내를 작성한다.
9. 신규 EconomicEvent 상태/시간 검증, 일정 수정·취소 재시작 복구, MacroContextSnapshot의 실제 ETF dict 구조·available_at·window 품질 검사를 추가 검증한다.
10. 기존 YAML에서 명시적 news_refresh_seconds는 새 기업/거시 옵션의 fallback이고 새 키가 우선이다. 오래된 float 값/최솟값 호환도 검사한다. config 예시 문서 갱신 필요.
11. Stage05 불변 기록/재생과 기존 전략/Paper 체결이 06 변경으로 달라지지 않는지 MySQL 포함 검증한다. 새 MacroArticle 필드는 기존 content_hash를 무조건 바꾸지 않도록 처리했으며 회귀 테스트가 필요하다.
12. 06 완료 후 변경 파일·테스트·미검증 항목을 보고하고 **07 승인 대기**. 후속 UI/실거래/GCP를 묶어 구현하지 않는다.

## 테스트 재현

표준 개발 환경에서:

```text
python -m pip install -r requirements.txt
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check sessionquant tests
python -m sessionquant doctor
```

이 작업 환경 인터프리터는 `/workspace/work/pennyquant/.venv/bin/python`이며 저장소는 `/workspace/work/pennyquant-upload`다. 새 계정에서는 자신의 clone/venv 경로를 사용한다. tests는 더미 키·모의 HTTP·tmp SQLite를 사용한다. 실제 MySQL은 `PQ_TEST_MYSQL=1`을 명시하고 **전용 격리 DB**에서만 검증한다. 사용자 기존 DB에 테스트 데이터를 쓰거나 장부를 지우지 않는다.

과거 05 완료 전체 MySQL 포함 결과는 947 passed였다. 토스 보완 이후는 위 943+14다. 06 초기 집중 검사에서 발견된 5개 실패 중 4개는 출처 차단 계약에 맞는 fixture/기대값과 runtime의 mock 호환을 보완했다. 남은 calendar fixture 오류는 위 설명과 같다. 이 체크포인트의 전체 테스트 결과는 아래 추기한다.

## 민감정보와 운영 원칙

키 값·비밀번호·계좌번호·접속 비밀은 기록하거나 커밋하지 않는다. `.env`는 사용자 로컬에서 준비한다. 변수 이름은 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT_SEQ`, `OPENAI_API_KEY`, `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `SEC_USER_AGENT`, `FRED_API_KEY` 및 MySQL `PQ_MYSQL_HOST`, `PQ_MYSQL_PORT`, `PQ_MYSQL_USER`, `PQ_MYSQL_PASSWORD`, `PQ_MYSQL_DATABASE`를 사용한다. 실제 정확한 별칭·namespace·환경 키는 `sessionquant/database/config.py`, `.env.example`, `local_env.py`를 확인한다. LLM 접근 가능 여부와 충전 잔액은 다르며 잔액 미확인을 충전액으로 꾸미지 않는다.

실제 주문·실거래 활성화·새 유료 서비스 설정·GitHub 저장소 이름 변경은 별도 승인 대상이다. 현재 작업 중 실제 주문과 유료 LLM 호출은 0건이다. 토스 OAuth 재발급이 기존 클라이언트 인증을 무효화하므로 운영 중 별도 조회기를 무분별하게 띄우지 않는다. 뉴스 장애가 손절·청산 시계를 막지 않도록 작업을 분리한다.

## 원래 체크포인트 검증 및 새 계정 시작 방법 (이력)

2026-11-15 전체 검사: `python -m pytest -q` → **983 passed, 4 failed, 14 skipped, 12 warnings (113.74초)**. Ruff 통과, doctor 통과, diff 공백 검사 통과. 06 체크포인트에서 격리 MySQL 검사를 다시 실행하지 않았으며 skipped 14건은 정상 통과로 계산하지 않는다.

전체 검사에서 추가로 확인한 실패:

- `tests/test_llm_fallback.py::test_exhausted_credit_warns_and_cools_down_across_restart[insufficient_quota]`
- 동일 테스트의 `[credit_balance_exhausted]`

두 fixture는 `example.com`, 미확인 발행사 기사이므로 새로운 출처 사전 차단에 걸려 mock OpenAI 429 호출까지 도달하지 않는다. 출력은 health UNKNOWN이며 기대값은 NO_CREDIT이다. 실제 충전액 부족 대응 코드가 동작하는지는 **유효하게 검증된 공식 기사 fixture로 검사를 다시 수행**해야 한다. 출처 검사를 제거해 테스트를 통과시키지 않는다.

- `tests/test_market_reference.py::test_schema_upgrade_preserves_existing_order_ids`: 기존 기대 버전4와 새 버전5가 달라 실패한다. 버전5가 맞는지 신규 테이블과 기존 주문 ID 보존을 함께 검증한 뒤 기대값을 갱신한다.
- `tests/test_stage06_intake.py::test_calendar_dst_cancel_and_unknown_timezone`: 위의 known_at fixture 오류.

체크포인트 브랜치: **`wip/stage06-handoff-20261115`**. main의 안정 기준은 b3010e6을 유지한다. 새 Codex는 이 브랜치를 체크아웃하고 HANDOFF.md 및 기존 지침부터 읽는다.

```text
git clone https://github.com/taegyoo1104/pennyquant.git
cd pennyquant
git switch wip/stage06-handoff-20261115
```

새 Codex에 전달할 시작 지시 예시:

> HANDOFF.md와 저장소의 기존 지침을 먼저 읽어 주세요. 안정 기준 b3010e6과 현재 06 WIP를 비교하고, 기록된 테스트 실패·분석 사전 차단·대시보드/SQL/문서 미완료부터 해결해 주세요. 기존 전략·장부·주문 안전장치는 보존하며 실제 주문과 유료 LLM 호출을 실행하지 마세요. 06 완료를 검증·보고한 뒤 07단계는 승인 대기하세요. 상세 기획서가 필요하면 재첨부를 요청하세요.

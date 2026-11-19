> **과거 기록 · 현재 실행 지침 아님** · 아래 명령·설정·완료 상태는 작성 당시의 기록입니다. 현재 절차는 [프로젝트 소개](../../README.md)와 [문서 안내](../README.md)를 따르세요. [보관 문서 안내](README.md)

# 00~08 종합 검토 및 보완

검토일: 2026-11-15. 사용자 요청으로 08 구현 커밋 `8a328e7` 이후 지금까지의 작업을 단계 첨부와 실제 코드·회귀 검사 기준으로 검토했다. 실제 공급자·계좌·장기간 성과 검증과 구분하며, 08A/09/10·클라우드 배포·KIS를 추가하지 않았다.

## 재현한 문제와 수정

| 우선순위 | 재현과 영향 | 수정·회귀 근거 |
|---|---|---|
| P1 | Paper 장부가 전날로 이동한 뒤 다시 오늘로 이동하면 일일 기준 자산과 손실 중지가 초기화됨. 빈 엔진 tick은 수신 커서만 검사하므로 처리 시각 역전도 허용했음 | `portfolio/broker.py`에서 과거 거래일 전환을 거절. `session_engine.py`가 수신 시각과 별도로 `last_decision_at`을 보존·복구하고 과거 처리 시각을 변경 전에 거절. 손실 중지·저장 상태 보존 및 직전 tick 이전에 수신한 자료를 현재 시각으로 처리하는 호환 검증 |
| P2 | 05 연구 보고서가 `pricing_verified=true`와 구 `cost_is_estimate=false`를 실제 청구 확인으로 오인. 07의 측정된 토큰 추정도 검증 청구 합계에 들어갈 수 있었음 | `research/costs.py`가 프로그램 추정과 명시적 청구 대사를 구분. `llm_recorded_estimated_usd` 추가, 기존 `llm_recorded_verified_usd`는 실제 청구 대사 증거가 없으면 null. usage 개정은 호출 ID별 마지막 버전만 집계하고 과거 미매핑은 별도 표기 |
| P2 | 00에서 요청한 CI가 없고 로컬 검사 이력만 존재 | `.github/workflows/ci.yml` 추가. 고정 Python 3.12·해시 잠금·import/CLI/doctor·Ruff·pytest, 선택적 임시 MySQL job. 운영 secrets·실주문 명령을 사용하지 않음. `tests/conftest.py`의 CI 전용 외부 연결 차단 |
| P2 | 첫 원격 CI에서 기존 `.env`의 0644 권한이 비밀값 저장 후에도 남는 것을 확인. 로컬 umask 077에서는 기존 테스트의 입력도 0600이어서 드러나지 않았음 | `local_env.py`가 POSIX에서 쓰기 전에 0600으로 제한. 권한 변경 실패 시 새 비밀값을 쓰지 않음. 기존 테스트에 0644를 명시하고 0666에서 매 재작성 전후의 제한·권한 실패 시 미기록 회귀 추가. Windows ACL은 별도 운영 확인 |
| P3 | `docs/STRATEGIES.md`, `SYSTEM_RUN.md`가 과거 PennyQuant 1분봉/뉴스 필수 흐름임을 첫 화면에서 구분하기 어려움 | 원본·링크를 보존하고 레거시 표식 및 현재 SessionQuant 진입 링크 추가 |

시계 검증을 추가하자 기존 `test_active_unknown_blocks_new_signals_and_cancels_pending_buy`가 청산까지 끝낸 테이프에서 과거 진입 시각으로 돌아가고 있음이 드러났다. fixture를 진입 시간까지의 입력으로 분리했다. UNKNOWN 위험이 유효 신규 신호를 막고 대기 매수를 취소하는 검사는 유지하며 시간 검증을 완화하지 않았다.

거래량·호가 수량의 bool 입력은 기존 `models.whole`에서 이미 거절하는 것을 확인했다. 정상인 입력 검증을 다시 구현하지 않았다.

## 단계별 추적

| 단계 | 확인한 실제 경로·주요 회귀 | 결과와 남은 확인 |
|---|---|---|
| 00 기준선·문서·CI | `SESSIONQUANT_CURRENT`, 레거시 안내, 설치/도움말/doctor, 새 CI·`local_env.py` | CI 구성·잠금 설치·로컬 동등 검사 추가. 원격에서 발견한 기존 설정 파일 권한 보완. 원격 결과는 해당 커밋의 Actions 체크에서 확인 |
| 01 감시 100/집중 20 | `watchlist/capitalization.py`, `selection.py`, 공정 조회·시총/편입·만료·실보유 우선 검사 | 임의 시총·지수 편입·참고 ETF 혼입 없음. 실제 토스 커버리지/쿼터/수집 지연 미검증 |
| 02 시장 데이터·자격·RVOL | `market/{source,eligibility,quality}.py`, `test_market_data_quality`, `test_relative_volume` | 동일 경과 분 20일·미확정/미래/충돌·기업행동 근거·bool 차단 유지. 실제 계좌/소수점/분할 비교 증거 미확보는 UNKNOWN |
| 03 Paper 체결 | `portfolio/{broker,market_fills}.py`, `test_paper_market_fills` | 다음 유효 호가·접수/취소 지연·부분/미확정·잔량·멱등성 유지. 실제 브로커 체결·수수료 재현 보증 없음 |
| 04 전략·위험·청산 | `portfolio/manager.py`, `session.py`, `exit_policy.py`, 네 전략 및 위험 행렬 | 처리 시각/거래일 역전과 일일 손실 초기화 문제 수정. 손절·악재·수동·당일 청산 독립성, 2/5·비중·예약 위험 검사 유지 |
| 05 기록·재생·평가 | `research/{journal,bundle,replay,evaluation,costs}.py`, 동결·수신/처리 시각·홀드아웃 검사 | 청구 혼동·usage 버전 중복 집계 수정. 원본 선정 ID 연결과 같은 묶음 재생 확인. 실제 시장 장기 기록의 성과 비교 미실행 |
| 06 뉴스·원문·일정 | `news/{intake,enrichment,schedule}.py`, `macro/{calendar,context,service}.py`, 06 전용 검사 | 오래된/제목만/게시일 미확인 제외, 403/429/중복/rights·robots·DNS·경제 일정 버전 유지. 실제 발행사 조건·공급자 응답은 별도 확인. FOMC/EIA/Treasury 수동 검증 import 경로 유지 |
| 07 LLM·감사·비용 | `llm/`, `macro/{analysis,budget}.py`, 07 감사/맥락/보고 검사 | 스킵/실제 요청·오류·캐시·정산·만료·마스킹 분리 유지. 원문 감사/맥락 LLM 기본 OFF. 실제 모델 품질·권한·청구/잔액·유료 평가 미검증 |
| 08 최종 후보 | `portfolio/{selection,strength}.py`, `news/ranking.py`, 새 52건·MySQL/SQL | 동시 시점 후보→거절/승인→의도 연결, 참고 ETF 제외, UNKNOWN과 검증 중립 분리. 실제 섹터 전체 자료·비교 근거·운영 처리 성능·가점 효과 미검증 |

전체 단계에서 PAPER/LIVE namespace와 기존 주문·기사 ID를 유지했다. 사용자 장부를 열어 검증하거나 초기화하지 않았다. 네 전략은 단독 실험 구조를 유지하며 다중 전략·실거래 통합을 켜지 않았다.

## 비용 필드 호환

보고서의 기존 `llm_recorded_verified_usd` 키는 유지하고 확인되지 않은 실제 청구는 null로 둔다. 측정 토큰과 공개/설정 단가의 곱은 새 `llm_recorded_estimated_usd`다. `TOKEN_BASED_ESTIMATE`·`UNCONFIRMED_RESERVED_ESTIMATE`와 legacy 미확인을 상태별로 집계한다. 이전 `cost_is_estimate`의 의미를 소급해 바꾸거나 과거 사용량을 삭제하지 않는다.

실제 청구 합계를 채우려면 모든 대상 호출에 명시적 `PROVIDER_BILLED_VERIFIED` 상태, `provider_billed_cost_verified=true`, USD 통화, 대사 출처와 청구 금액이 필요하다. 이번 작업은 해당 집계를 수집하지 않았으므로 기본 UNKNOWN이다. 조직 전체 Costs API 금액을 개별 호출 비용으로 임의 배분하지 않는다.

## CI와 로컬 재현

운영 `requirements.lock`의 59개 버전을 유지하고 검사 도구를 포함한 66개 `requirements-dev.lock`을 생성했다. 현재 로컬 개발 환경과 잠금 환경의 exchange-calendars 패치 버전 차이도 확인해, 새 가상환경에 잠금 그대로 설치하여 검사했다. CI는 같은 해시 잠금을 사용한다.

```bash
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m pip check
python -m ruff check sessionquant tests
python -m sessionquant --help
python -m sessionquant doctor

# 기본 검사. CI는 외부 연결을 차단하고 MockTransport·loopback만 허용한다.
SQ_TEST_OFFLINE=1 python -m pytest -q
```

MySQL은 `PQ_TEST_MYSQL=1`과 **테스트 전용 격리 DB**를 지정한 경우에만 검사한다. 사용자 DB를 연결하지 않는다. 이 개발 세션은 별도로 생성한 MySQL 8.4 컨테이너와 임의 namespace를 사용했다. 기본 CI의 MySQL skip은 통과 수에 포함하지 않는다. Actions의 `workflow_dispatch → run_mysql=true`는 폐기 가능한 MySQL 서비스에서 별도 21건을 실행한다.

워크플로 권한은 contents:read, checkout 인증 유지 OFF다. [checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1)과 [setup-python v7.1.0](https://github.com/actions/setup-python/releases/tag/v7.1.0)의 공식 태그·action 정의를 확인하고 commit SHA를 고정했다. 배포·주문·유료 LLM 생성 단계는 없다.

`dc8eef5`의 [첫 원격 CI](https://github.com/taegyoo1104/pennyquant/actions/runs/43676057626)는 설치·doctor·Ruff 통과 후 파일 권한 1건이 실패했다(1,148 passed, 21 skipped). 위의 권한 보완을 적용하고 관련 19건을 umask 022에서 다시 통과했다. 이 실패를 통과로 처리하거나 검사 기준을 완화하지 않았다.

최종 검사 횟수·시간·커밋은 [HANDOFF 최신 절](../../HANDOFF.md)에 기록한다. CI YAML·권한·선택 MySQL·SHA 정적 검사, 새 가상환경 해시 설치/패키지 설치/pip check, 오프라인 집중 검사도 수행했다. 기존 pandas 시간대 경고는 기능 실패와 구분한다.

## 남은 우선순위

1. 로컬 Paper를 실행하기 전에 실제 토스 계좌 자격과 RVOL/기업행동 비교 근거, 시총·편입·실시간/지연 자료를 확보한다. UNKNOWN을 성공으로 바꾸어 매수를 늘리지 않는다.
2. 08 기록 예시에서 실제 장중 자료를 수집하고 품질 보고를 확인한 뒤 `evaluate-candidates`로 같은 기록의 가점 적용 전/후를 비교한다. 충분한 기간·거래·외표본과 실비가 있어야 운영 효과를 논의할 수 있다.
3. 권한을 확인한 발행사만 원문 수집/전송/보관 정책을 설정한다. 실제 모델 평가·유료 `--execute`는 별도 승인 후 소량으로 진행한다.
4. 실제 처리 성능·지연·장애/재시작·미청산 대응과 사용자 PC의 Windows 권한을 관찰한다. 합성 검사의 통과를 장기간 운영 승인으로 해석하지 않는다.
5. 다음 개발은 08A 첨부를 별도 승인받아 진행한다. 실제 공통주문·실거래 검증은 09/10, GCP 이전은 이후 별도 범위다.

구현과 재현 가능한 결함 보완은 완료 범위로 보고하되, 실데이터 효과·실계좌·운영 검증은 남아 있다. 실제 주문·유료 LLM 호출·사용자 DB 접근은 이번 검토에서도 0건이다.

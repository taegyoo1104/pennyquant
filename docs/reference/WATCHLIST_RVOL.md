# 감시 50종목 과거 분봉 저장·RVOL

실제 시세·뉴스를 받아 **매매만 모의로** 실행하는 최신 절차는 [실데이터 Paper 안내](../REALTIME_PAPER.md)를 따른다. 아래의 비교 근거 없으면 전략에 RVOL을 전달하지 않는 설명은 기본 `strict` 정책이다. 별도 `observed_paper_v1` 예시는 완전한 동일 제공처·동일 경과 분의 계산값을 `EXPERIMENTAL_UNVERIFIED`로 전략에 전달하며, 실제 계좌 자격이나 거래량 비교 근거를 검증됨으로 바꾸지 않는다.

2026-11-15 사용자 요청에 따라 기본 시총 후보는 **시총이 확인된 S&P 500 종목 중 최대 50종목**, 실제 운영은 그중 분봉 준비가 끝난 종목, 집중 분석은 그 안에서 최대 20종목이다. `watchlist_size`로 최대 100까지 선택할 수 있다. 보유 목표 2종목과 위험 한도는 그대로 유지한다. 과거 단계 문서의 100종목은 이전 기본값이다. 사용자 YAML에 `watchlist_size: 100`이 명시돼 있으면 50으로 수정해야 한다.

## 로컬 실행

같은 토스 클라이언트를 사용하는 수집기·계좌 조회기를 먼저 종료한다. 토큰 재발급은 이전 토큰을 무효화할 수 있다. 기존 로컬 환경변수 또는 `.env`의 토스 키와 MySQL 설정을 사용한다. 아래 명령에는 같은 `--config`를 사용해야 같은 DB 네임스페이스를 읽는다. MySQL에서 `db_path`는 실행 공간을 식별하며 SQLite 파일을 대신 만드는 옵션이 아니다.

```sh
git pull --ff-only origin wip/stage06-handoff-20261115
python -m sessionquant watchlist-refresh --config config/sessionquant.candidates.example.yaml
python -m sessionquant history-backfill --config config/sessionquant.candidates.example.yaml
python -m sessionquant history-status --config config/sessionquant.candidates.example.yaml
```

유효한 S&P 500 편입 원장이 없다면 먼저 `python -m sessionquant universe-refresh --output data/sp500-universe.json`으로 준비한다(사용자 설정의 `universe_path`가 다르면 해당 경로 사용). 감시목록이 UNKNOWN/PARTIAL/만료이면 임의로 50개를 고르지 않고 수집을 중단한다. 시총 순위는 기존 선택 정책과 출처를 따른다.

`history-backfill`은 감시목록의 완료된 과거 20개 정상 길이 거래일과 사이에 있는 조기 폐장일을 조회한다. 이를 통해 장 후반 RVOL에서도 비교 가능한 20일을 확보하도록 준비한다. 정규장은 390분, 조기 폐장은 실제 달력의 분 수를 사용하며 누락을 0으로 보충하지 않는다. UTC와 뉴욕 거래일을 구분해 서머타임도 반영한다.

하루 2페이지가 필요한 경우 최초 50종목×20일은 약 2,000회, 초당 1회 속도 제한에서는 **최소 약 34분**이다. 서버·DB·재시도·조기 폐장 추가분에 따라 더 걸릴 수 있다. 기본 예산은 재시도를 포함한 읽기 요청 5,000회이며 `--max-requests`로 조정한다. 각 종목·날짜별 진행 상황을 즉시 출력한다.

Ctrl+C, 호출 예산 소진, 오류로 중단해도 저장된 페이지와 다음 커서를 유지한다. 같은 명령을 다시 실행하면 이어받는다. 이미 완전히 저장된 거래일은 건너뛰므로 다음 날에는 새 완료일·신규 편입 종목·부족 구간 위주로 수집한다. 완료일의 공급자 정정을 재확인하려면 `--refresh-complete`를 지정한다. 정정으로 충돌한 두 버전은 둘 다 남기며 자동으로 한쪽을 확정하지 않는다. HTTP 429는 공급자 재시도 대기시각을 저장하고 종료한다.

`history-status`의 `backfill.status=COMPLETE`는 요청한 종목×거래일의 분봉 확보를 뜻한다. 오래된 실행의 완료 상태는 `started_at`와 `session_dates`를 함께 봐야 한다. 명령 종료 코드 0은 수집 완료, 1은 부분 완료/중단, 2는 설정 오류다. 수집 명령은 주문·계좌·뉴스·LLM API를 호출하지 않는다.

`INCOMPLETE`인데 `error:null`이면 API 예외 없이 실행을 마쳤어도 일부 종목×거래일에서 완전한 분봉을 확보하지 못했다는 뜻이다. 재조회 전에 저장된 데이터를 검사한다.

```sh
python -m sessionquant history-status --config config/sessionquant.candidates.example.yaml --details --output data/history-quality-report.json
```

이 검사는 추가 API 요청 없이 마지막 수집 계획의 모든 종목×날짜를 DB에서 읽는다. 날짜별 누락·충돌 분, 최초 문제 분, 누락 구간, 페이지 종료 사유, 종목별 완전한 거래일 수와 5/75/120/240/390분까지의 확보 거래일 수를 출력한다. 분 번호 1은 개장 1분 후 끝나는 봉이다. 파일은 새 경로만 허용하며 원본 분봉·수신 시각·수집 상태는 수정하지 않는다. 완료된 종목×날짜 비율은 분봉 자체의 누락 비율과 다르다. 누락 분을 거래량 0으로 채우거나 비교 거래일을 임의로 건너뛰지 않는다.

### 시총 누락 종목 제외 — 기본 정책

`watchlist_missing_cap_policy: exclude`가 기본값이다. 전체 편입 종목 중 시총 자료가 없거나 값이 비어 있는 종목은 제외하고, 남은 종목의 시총 순으로 최대 `watchlist_size`개를 선정한다. 검증된 종목이 50개보다 적으면 그 수를 사용한다. 토스 섹터 수집이 진행 중인 상태, 시총 충돌·중복·잘못된 통화·출처·음수/비정상 수치·만료·편입 불일치는 누락으로 간주하지 않고 기존 차단을 유지한다. 파일 입력은 발행사/대표 주식 클래스 근거 검증도 유지한다.

업데이트 후 기존 캐시를 새 정책으로 다시 선정한다. 과거 스냅숏과 최초 수신 시각은 변경하지 않고 새 선택 시각의 이력을 추가한다.

```sh
git pull --ff-only origin wip/stage06-handoff-20261115
python -m sessionquant watchlist-refresh --config config/sessionquant.candidates.example.yaml
python -m sessionquant history-status --config config/sessionquant.candidates.example.yaml --details --output data/history-readiness-cap-excluded.json
```

시총 누락만 있고 나머지 검사가 유효하면 다음처럼 표시한다.

- `status: VALID`: 현재 **제외 정책에 따른 후보 선정**이 유효하다.
- `market_cap_coverage: PARTIAL`, `ranking_verified: false`, `ranking_scope: available_market_caps`: 전체 편입 종목의 시총 순위는 미확정이다.
- `excluded_market_cap_symbols`: 시총 누락으로 제외한 티커. 스냅숏의 `market_cap_exclusions`에는 사유도 저장한다.
- 대시보드에서 전체 시총 순위 칸은 비워 두고, 확인된 자료 내 순위와 제외 티커를 표시한다.

사용자 보고서의 BNY·FITB·FTV·HON·KHC·SKYD·VYLR 7개는 원래 알려진 시총 후보 50개에 들어 있지 않았다. 따라서 50에서 7을 다시 빼지 않는다. 시총 자료가 같다면 후보 50개를 유지하고, 분봉 준비 26개만 운영 대상으로 남는다. 숫자는 매번 실제 자료로 계산한다. 시총 제외 정책은 RVOL 분할/거래량 비교 근거 및 계좌 적격성 검사를 대체하지 않는다.

### 기존 엄격 정책 또는 이전 PARTIAL 기록

전체 시총을 확인해야만 선정하려면 YAML에 `watchlist_missing_cap_policy: block`을 명시한다. 이전 정책으로 저장된 PARTIAL 기록도 갱신 전까지 보존된다. 아래 설명과 사전 수집 옵션은 이 경우에 적용한다.

`watchlist_size: 50`과 `status: PARTIAL`이 함께 나오면 확보된 자료로 후보 50개를 만들었지만 전체 상위 50 순위는 미확정이다. `market_cap_missing`은 섹터 자료에서 해당 티커의 유효한 시총을 확보하지 못했다는 뜻이며, 상장폐지·거래 불가 또는 상위 50 밖이라는 뜻이 아니다. 완료된 토스 시총 수집은 하루 동안 캐시되므로 `watchlist-refresh` 반복만으로 누락이 해소되지는 않는다.

```sh
python -m sessionquant watchlist-diagnose --config config/sessionquant.candidates.example.yaml --connect
```

진단은 저장된 섹터 자료의 누락 여부와 누락 티커의 기본 종목 정보를 구분한다. `--connect`를 빼면 DB만 읽고, 지정하면 최대 20개의 누락 티커를 개별 StockInfo GET으로 조회한다. 새 인증에 따른 토큰 충돌을 피하도록 기존 토스 수집기를 종료한 상태에서 실행한다. 이름·ISIN·상장 상태가 조회돼도 시총 누락을 보완하거나 티커를 자동 치환하지 않는다. 실패 응답 본문·인증키·계좌정보는 출력하지 않는다.

순위 검증 전에도 후보의 **분봉만 사전 저장**하려면 다음 옵션을 명시한다.

```sh
python -m sessionquant history-backfill --config config/sessionquant.candidates.example.yaml --allow-partial-watchlist
```

이 옵션은 만료되지 않은 토스 PARTIAL 목록에서 문제가 시총 누락뿐이고 설정한 수만큼의 후보가 있을 때만 허용한다. 분봉 수집 보고서의 `selection.status=PARTIAL`, `selection.ranking_verified=false`, 누락 티커 목록을 보존한다. `backfill.status=COMPLETE`가 되어도 **감시목록은 PARTIAL이며 매수 차단·장중 집중 선정 조건은 바뀌지 않는다.** 시총 검증이 완료되어 정상 감시목록에 포함된 종목은 사전 저장한 분봉을 재사용할 수 있다. 충돌·출처 오류·만료·편입 불일치는 이 옵션으로 우회하지 않는다.

## 분봉이 준비된 50−n종목 운영

토스 감시목록을 사용하는 `run`에 자동 적용된다. 원래 시총 후보 50개와 순위는 보존하며, 과거 20개 정상장 및 사이의 조기 폐장일에 분봉이 하나라도 누락·충돌하거나 출처/수신 시각이 잘못된 종목은 운영 목록에서 제외한다. 다른 종목을 추가해 50개를 채우지 않는다. 준비된 종목이 0개이면 일반 후보 조회·신규 진입 없이 위험 관리만 유지한다.

- 장전 실행은 다음 실제 거래일의 준비 목록을 계산한다. 주말·휴장·서머타임·조기 폐장 달력을 적용한다. 장전 재시작 및 개장 후 첫 검사에서 DB를 다시 읽는다.
- 개장 후 첫 검사에서 운영 목록을 고정하고 같은 거래일 재시작 시 복구한다. 장중에 누락이 복구되거나 새 시총 후보가 생겨도 다음 거래일 검사 전에는 추가하지 않는다. 시총/편입 목록에서 빠진 종목은 즉시 일반 운영에서 제외한다.
- 집중 분석, 기업 뉴스 후보, 일반 현재가·분봉·과거 보충 조회는 운영 목록에 한정한다. **보유·미체결·청산 예정 종목과 참고 ETF의 위험 조회는 계속한다.** 장중 새 분봉 누락·지연 및 거래량 비교 근거는 기존 매수 검사에서 별도로 차단한다.
- 대시보드 시장·종목 화면은 원래 후보, 분봉 준비 수, 운영 수, 제외 사유와 검사 거래일/시각을 표시한다. `watchlist_history_selection_v1` 제어 상태와 `pipeline_status`에 선정 근거를 저장하고 연구 기록에도 포함한다. DB 스키마 변경은 없다.

이미 수집했다면 전체 재수집 없이 다음 명령으로 **현재 DB 기준 준비 목록을 미리 확인**할 수 있다. 이 읽기 검사는 운영 목록을 고정하거나 변경하지 않는다.

```sh
python -m sessionquant history-status --config config/sessionquant.candidates.example.yaml --details
```

`history_readiness_preview.included_symbols`와 `excluded_count`가 현재 후보의 분봉 준비 결과이고, `daily_watchlist`는 수집기가 마지막 저장한 준비/고정 목록이다. 두 값은 검사 시각과 거래일이 다를 수 있다. `--output data/history-readiness-report.json`을 붙이면 새 파일로 저장하며 콘솔에도 준비 종목과 제외 수를 표시한다.

사용자가 제공한 최초 진단은 50개 중 26개가 20일 전체 분봉을 확보했고, 24개는 누락이 있었다. 이 숫자를 코드에 고정하지 않는다. **선정 `status` 자체가 PARTIAL이면 운영·집중 선정 및 신규 매수는 차단된다.** 제외 정책으로 `status: VALID`, `market_cap_coverage: PARTIAL`인 경우는 확인된 시총 범위에서 운영 가능하다는 뜻이다. RVOL 분할·거래량 비교 근거와 계좌 적격성은 별도로 검사한다.

## 장중 연결

```sh
python -m sessionquant run --config config/sessionquant.candidates.example.yaml
```

기존 `run`의 뉴스·LLM 설정은 그대로 적용된다. 시장 수집기는 같은 DB의 과거 이력을 자동으로 읽고, 당일 확정 분봉을 계속 저장한다. 다시 실행해도 과거 분봉을 매번 API에서 다시 받지 않는다. 매일 장 시작 전에 `history-backfill`을 실행하는 운영을 권장한다. 자동 장전 스케줄러나 GCP 작업은 이번 변경에 포함하지 않는다.

보유·청산 위험 호가를 먼저 전달하고 집중 후보를 수집한 뒤, 운영 목록의 나머지 종목을 `watchlist_candles_per_cycle`(기본 4개)씩 순환한다. 전체 호출 예산과 시간 제한 안에서만 실행되므로 **운영 종목 모두를 매분 조회한다는 보장은 없다.** 수집이 늦거나 새 분이 누락되면 그 종목의 최신 RVOL은 미확인으로 표시한다. 장중 늦게 시작한 경우 운영 종목의 개장 이후 누락 페이지도 점진적으로 보충한다.

수집된 당일 분봉으로 분 단위 관찰용 RVOL을 계산한다. 매매 엔진은 기존대로 완료된 5분봉 시점의 RVOL을 사용한다. 두 값은 기준 시각이 다르면 다를 수 있다.

```text
RVOL = 오늘 개장부터 계산 기준 분까지의 누적 거래량
       / 과거 20개 비교 거래일의 같은 경과 분 평균 누적 거래량
```

대시보드 **시장·종목 → 감시목록 과거 분봉·RVOL**에서 수집 완료 현황, 종목별 기준 시각, 확보 거래일, 누적 거래량, 평균, 계산값을 볼 수 있다. 오래된 값은 숨기고 사유를 표시한다. CLI `history-status`도 같은 상태를 읽는다.

## 저장·검증 경계

- MySQL `pq_market_history_sessions` / SQLite `market_history_sessions`: 종목·뉴욕 거래일·출처·조정 방식별 묶음에 정규장 원본 OHLCV, 실제 최초 수신 시각, 정정 버전, 확보율, 다음 페이지 커서를 저장한다. 현재 엔진의 `market_bars` 장부와 분리한 영속 비교 이력이며 기본 7일 정리 대상이 아니다.
- 동일 OHLCV 재조회는 최초 수신 시각을 유지한다. 미래에 수신한 자료를 과거에 알고 있었던 것으로 소급하지 않는다. 과거 비교 이력은 연구 기록에도 사용 시점과 함께 포함되어 오프라인 재생에서 복구된다.
- `calculated_value`: 누락·충돌 없이 동일 출처/조정 방식의 입력으로 계산한 수치다.
- `value` / `status=VERIFIED`: 기존 분할·거래량 비교 근거까지 유효할 때만 매매 엔진에 전달하는 값이다. 토스 이력 수집 성공만으로 이 검증을 만들지 않는다.

따라서 과거 이력이 확보되어 `calculated_value`가 표시되어도, 비교 근거가 없으면 `value=null`, `status=UNKNOWN`을 유지한다. 실제 계좌 적격성·소수점·거래정지·기업행동 검증은 별도이며, 이번 변경이 이 매수 차단 조건을 해제하지 않는다. 사용자 제공 진단의 26개 완전성 결과도 매매 적격성 검증을 뜻하지 않는다.

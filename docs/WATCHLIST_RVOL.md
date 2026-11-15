# 감시 50종목 과거 분봉 저장·RVOL

2026-11-15 사용자 요청에 따라 기본 감시는 S&P 500 시총 상위 **50종목**, 집중 분석 최대 20종목이다. `watchlist_size`로 최대 100까지 선택할 수 있다. 보유 목표 2종목과 위험 한도는 그대로 유지한다. 과거 단계 문서의 100종목은 이전 기본값이다. 사용자 YAML에 `watchlist_size: 100`이 명시돼 있으면 50으로 수정해야 한다.

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

## 장중 연결

```sh
python -m sessionquant run --config config/sessionquant.candidates.example.yaml
```

기존 `run`의 뉴스·LLM 설정은 그대로 적용된다. 시장 수집기는 같은 DB의 과거 이력을 자동으로 읽고, 당일 확정 분봉을 계속 저장한다. 다시 실행해도 과거 분봉을 매번 API에서 다시 받지 않는다. 매일 장 시작 전에 `history-backfill`을 실행하는 운영을 권장한다. 자동 장전 스케줄러나 GCP 작업은 이번 변경에 포함하지 않는다.

보유·청산 위험 호가를 먼저 전달하고 집중 후보를 수집한 뒤, 감시목록의 나머지 종목을 `watchlist_candles_per_cycle`(기본 4개)씩 순환한다. 전체 호출 예산과 시간 제한 안에서만 실행되므로 **50종목 모두를 매분 조회한다는 보장은 없다.** 수집이 늦거나 새 분이 누락되면 그 종목의 최신 RVOL은 미확인으로 표시한다. 장중 늦게 시작한 경우 개장 이후 누락 페이지도 점진적으로 보충한다.

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

따라서 과거 이력이 확보되어 `calculated_value`가 표시되어도, 비교 근거가 없으면 `value=null`, `status=UNKNOWN`을 유지한다. 실제 계좌 적격성·소수점·거래정지·기업행동 검증은 별도이며, 이번 변경이 이 매수 차단 조건을 해제하지 않는다. 로컬에서 받은 AAPL 20일·7,800분봉 조회 성공은 확인했지만 나머지 감시 종목의 실제 확보 결과는 사용자 PC 실행 후 확인해야 한다.

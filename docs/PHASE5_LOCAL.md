# Phase 5: 첫 소수점 실주문 검증

Phase 5의 첫 단계인 **로컬 1종목 소액 주문·체결·잔고 대사 경로**다. 일반 `run`은 기존 Paper 전용을 유지한다. 네 전략의 완전 자동 실거래 운용을 완료했다고 의미하지 않는다. 실제 계좌의 최소 금액·권한·체결·수수료 검증을 마친 뒤 자동 전략 연결, 추가매수, 장애별 실계좌 검증을 확대한다. GCP 실거래는 이 단계에 포함하지 않는다.

공식 명세 `1.2.28`을 다시 조회했다. [원문](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)의 MARKET/DAY 금액 매수 `orderAmount`, 소수점 수량 매도 `quantity`를 사용한다. 지정가 가격 보호는 제공되지 않으며 실제 시장가 체결 가격·손실을 보장하지 않는다. 접수는 정규장 시작~종료 1시간 전이다. 계좌별 최소 금액과 거래 가능 여부는 문서만으로 확정할 수 없으므로 빈 설정을 임의의 값으로 채우지 않는다.

## 먼저 주문 없는 사전 검사

기존 프로그램을 종료하고 PyCharm에서 Ctrl+T로 업데이트한다. `setup-env --from-current`로 한 번 저장한 `.env` 또는 현재 PowerShell에 기존 토스 키·MySQL 연결 변수가 있어야 한다. 접속 설정 자동 로드는 실주문 활성화를 의미하지 않는다. API 키·비밀번호·계좌번호를 Git이나 채팅에 올리지 않는다.

```powershell
.\venv\Scripts\python.exe -m pip install --no-deps -e .
.\venv\Scripts\python.exe -m sessionquant universe-refresh --output data/sp500-universe.json
.\venv\Scripts\python.exe -m sessionquant live-local --config config/live.local.yaml
```

기본 명령은 OAuth 인증과 계좌·보유·미체결·현금·수수료·종목 마스터 조회만 한다. 실제 주문 POST는 호출하지 않는다. `account_verified`, `reconciled`, `account_seq`, `buy_blockers`를 확인한다. `actual_order_permissions_verified=false`는 읽기 성공이 실제 주문 권한 검증을 뜻하지 않는다는 의미다. 계좌번호와 키는 출력하지 않는다.

계좌가 여러 개라면 본인 PC에서 `TOSS_ACCOUNT_SEQ`로 계좌를 선택한다. 하나인 경우 그 종합매매 계좌를 조회하되 실제 주문에는 해당 `account_seq`를 별도로 확인해야 한다. 계좌번호와 account_seq는 다르다.

`LIVE_LOCAL`과 `data/sessionquant-live-local.db`로 별도 MySQL 네임스페이스를 사용한다. 이전 Paper DB는 사용할 수 없다. `SQ_DB_NAMESPACE`, `PQ_DB_NAMESPACE` 수동 덮어쓰기는 해제해야 한다. 같은 MySQL 서버에서 동일 account_seq 엔진을 중복 실행하지 못하도록 계좌 잠금을 사용한다. 서로 다른 MySQL 서버의 전역 잠금은 Phase 6에서 다룬다.

## 검증 설정

`config/live.local.yaml`의 기본값은 매수 $1, 최대 $5, 추가매수 0회다. 최소 금액은 `null`이며 실행하려면 **본인 계좌의 토스 앱/공식 계좌 안내에서 확인한 값**을 적는다. 최소 금액이 $5보다 크면 먼저 설정과 손실 예산을 검토해야 한다. 코드의 첫 주문 검증 상한은 $25다.

- `symbol`: 현재 S&P 500에 편입돼 있고 해당 계좌에서 소수점 매매 가능한 종목.
- `minimum_amount`: 본인 계좌에서 확인한 최소 달러 주문 금액.
- `eligibility_checked_at`: 해당 계좌의 소수점 지원·거래정지·기업행동 제한을 확인한 ISO 시각. 신규 매수 시 30분 이내여야 한다.
- `eligibility_evidence`: 실제 확인 근거를 설명하는 문장. 계좌번호·키는 넣지 않는다.
- `strategy_cash`: 이번 전략에 별도로 배정한 자금. 전체 계좌 평가금액이라고 가정하지 않는다.
- `stop_pct`, `target_pct`, `daily_loss_pct`, `risk_per_symbol`: 초기 실험의 손실·목표 예산. 갭·시장가 슬리피지로 실제 손실이 초과할 수 있다.
- `max_hold_seconds`: 기본 600초 뒤 청산 시도. 손절·목표·14:40 청산 시각이 더 빠르면 그때 시도한다.

UTC 확인 시각은 다음으로 확인할 수 있다. 실제 확인하지 않은 내용을 시각만 갱신해서 통과시키면 안 된다.

```powershell
[DateTime]::UtcNow.ToString("o")
```

현재 미국 주식 전용 기준 잔고만 지원한다. 국내주식 보유·외부 미체결·알 수 없는 주문 상태가 발견되면 차단한다. 기존 미국 보유는 기준 잔고로 보존하고, 시험 종목을 이미 보유한 경우 신규 매수를 막는다. 첫 테스트에 매수는 한 번만 허용하며 매도는 최대 3회 시도한다. 주문이 거부됐다고 새 DB나 새 ID로 매수를 반복하지 않는다.

## 별도 활성화 후 실제 주문

사전 검사 결과와 설정을 검토한 뒤 **본인이 실제 주문 실행을 선택한 경우에만** 다음 형식으로 실행한다. `확인한계좌순번`은 사전 검사에서 본 숫자로 바꾼다. 이 명령은 **시장가 매수와 이후 시장가 매도를 실제로 전송**하며 모의 실행이 아니다.

```powershell
.\venv\Scripts\python.exe -m sessionquant live-local --config config/live.local.yaml --execute --acknowledge-live-orders --confirm-account-seq 확인한계좌순번
```

매 실행마다 이 플래그가 필요하다. DB에는 재실행을 자동 활성화하는 상태를 저장하지 않는다. 신규 매수는 뉴욕 10:45~13:30만 허용한다. 조기 폐장은 거래소 달력에 따라 앞당긴다. 재시작 후 기존 전략 잔량이 있으면 추가 매수하지 않고 청산 대상으로 취급한다. 현재 단계의 종료 조건은 체결과 잔고 확인이며 전략 수익성을 검증한 것으로 해석하지 않는다.

`Ctrl+C`, `--max-polls` 종료, 네트워크·인증·DB 장애가 자동 청산 성공을 의미하지 않는다. 코드가 미청산 경고를 남기며 **토스 앱의 미체결·실제 보유 수량을 직접 확인해야 한다.** 접수 마감 이후·거래정지·API 장애로 청산하지 못할 수 있다. 주문 결과가 불명확하면 엔진은 재전송하지 않고 중단한다. 자동으로 UNKNOWN을 해제하거나 DB의 주문 행을 지우면 안 된다.

## 저장·조회

같은 MySQL의 `live_local_sessionquant-live-local` 네임스페이스에 저장한다.

- `pq_orders`: 실제 접수 상태·브로커 ID·누적 체결.
- `pq_executions`: 브로커 주문 ID별 누적 체결 확인(upsert). 재조회마다 체결을 추가 생성하지 않는다.
- `pq_positions`: 이 검증에서 실제로 매수하고 남은 수량만 저장. 외부 보유는 기준 잔고로 보존.
- `pq_reconciliations`, `pq_alerts`, `pq_daily_performance`: 대사·잔량 경고·배정 자금 기준 추정 손익.
- `pq_controls`: `fractional_live_v1`, `live_preflight`, `live_test_settings`, `broker_state`.

수수료·세금이 미확정이면 `fees_pending=true`이며 손익도 추정치다. 재조회로 확정 값을 반영한다. 현금 매수 가능 금액과 계좌 전체 평가금액을 동일시하지 않는다. 대시보드는 조회 전용이며 실행 중인 CLI가 활성화돼 있는지는 별도로 확인한다.

```powershell
$env:SQ_ENVIRONMENT = "LIVE_LOCAL"
.\venv\Scripts\python.exe -m sessionquant dashboard --backend mysql --db data/sessionquant-live-local.db
```

## 개발 검증과 다음 단계

모의 HTTP 검사에서 금액/수량 스키마, 활성화 기본 차단, 접수 전 영속화, 타임아웃·5xx 미재전송, 재시작 미확정 처리, 실제 누적 부분 체결·수수료 확정, 외부 잔고·미체결 차단, 초과 매도 방지, 취소 결과 미확정, DB 실패의 주문 차단을 검증한다. 실제 계좌의 인증·소수점 체결·최소 금액·실제 비용·Windows 장애 복구는 사용자 PC에서 검사해야 한다. 개발 서버에 사용자 키나 로컬 MySQL 연결이 없어 실제 주문은 수행하지 않는다.

첫 실거래 검증 이후 남는 Phase 5 작업은 완전 자동 전략과 실주문 장부 연결, 실계좌에서 손절·일일 제한·부분 체결·조기 폐장 장애 반복 검증이다. GCP 및 서로 다른 서버의 단일 활성 엔진 검증은 Phase 6이다.

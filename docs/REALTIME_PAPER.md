# 실제 시세·뉴스를 사용하는 Paper 운영

목표는 토스 시세와 실제 기업/거시 뉴스를 수집하고, OpenAI 분석을 기술 전략·후보 선정에 결합하되 매매는 가상 계좌에서만 실행하는 것이다. 로컬 MySQL부터 사용한다. KIS나 실거래 활성화는 포함하지 않는다.

## 1. 기존 분봉을 재사용해 준비

기존 `run`, 분봉 수집, 토스 연결 검사 프로그램을 중지한 뒤 PyCharm 터미널에서 실행한다. 사용자가 수정한 `scripts/setup_mysql.sql`은 이 변경에 포함하지 않는다.

```powershell
git switch wip/stage06-handoff-20261115
git pull --ff-only origin wip/stage06-handoff-20261115
python -m sessionquant prepare-paper --source-config config/sessionquant.candidates.example.yaml --config config/sessionquant.realtime-paper.example.yaml
```

`PREPARED`이면 준비가 끝났다. 이 명령은 **처음 한 번만** 실행한다. 기존 DB의 과거 분봉과 시총 수집 자료를 새 `sessionquant-realtime-paper-v1` 실행 공간으로 복사한다. 최초 수신 시각을 유지하며, 기존 거래·포지션·엔진 상태·매매 시작 요청은 복사하지 않는다. 복사 실패는 대상 트랜잭션을 되돌린다. 이미 준비한 대상에는 덮어쓰지 않는다. 원본은 그대로 남는다.

원본과 대상 설정은 같은 `universe_path`를 사용해야 한다. `SQ_DB_NAMESPACE` 또는 `PQ_DB_NAMESPACE`로 두 설정을 같은 MySQL 공간에 강제한 경우 복사를 거부한다. 일반적인 기존 설정은 `db_path` 이름으로 공간을 구분하므로 추가 설정이 필요 없다. 사용자별 포트폴리오·뉴스 설정을 변경했다면 새 YAML에도 반영한다. 비밀키는 기존 환경변수/로컬 `.env`에서 읽는다.

```powershell
python -m sessionquant watchlist-refresh --config config/sessionquant.realtime-paper.example.yaml
python -m sessionquant watchlist-status --config config/sessionquant.realtime-paper.example.yaml
```

시총 수집이 `collecting`이면 표시된 재시도 시각 이후 다시 실행한다. 기본 50개 중 시총을 확인할 수 없는 기업은 제외하고, 직전 20개 정상 거래일 및 사이 조기 폐장일의 분봉이 준비된 종목만 일일 운영 대상으로 사용한다. 기존 자료가 같다면 이전에 준비된 26개를 재사용할 수 있으나, 실제 개수는 날짜와 DB 검사 결과에 따라 달라진다. 빈자리를 다른 종목으로 채우지 않는다.

거래일이 바뀌면 비교 기간도 바뀐다. **이후 보충 수집은 새 설정으로** 장전에 실행한다. 완료한 거래일은 건너뛰며 준비된 종목이 없으면 신규 진입을 막는다.

```powershell
python -m sessionquant history-backfill --config config/sessionquant.realtime-paper.example.yaml
python -m sessionquant history-status --config config/sessionquant.realtime-paper.example.yaml --details --output history-before-open.json
```

`--output`은 새 파일에 저장한다. 같은 이름을 재사용하려면 다른 파일명을 지정한다. 전체 수집이 `INCOMPLETE`여도 일일 운영 목록에 준비된 종목이 있으면 그 종목들로 운영할 수 있다. 장중에는 가격·현재 분봉을 갱신하며 매번 전체 20거래일을 다시 받지 않는다.

## 2. 실제 뉴스와 LLM을 단독 진단

먼저 엔진을 중지한 상태에서 연결과 설정을 확인한다.

```powershell
python -m sessionquant check-news --config config/sessionquant.realtime-paper.example.yaml --connect --provider openai
python -m sessionquant paper-news-probe --config config/sessionquant.realtime-paper.example.yaml --symbol NVDA
```

첫 명령은 모델 접근만 검사한다. 두 번째 명령은 지정 기업의 실제 SEC/RSS/선택적 Alpaca 수집을 사용하고, 검증 가능한 최신 기사에 **실제 LLM 생성 요청을 실패 포함 최대 1회** 보낸다. 과금이 발생할 수 있으며 기존 월/일/시간 예산과 감사 원장을 함께 사용한다. 재시도·상위 모델 분석도 이 1회 제한을 넘지 못한다. 주문은 생성하지 않는다.

| 진단 상태 | 의미 |
| --- | --- |
| `VALID_RESPONSE_AND_STRATEGY_CONTEXT` | 유효한 분석이 기업 뉴스 가점/위험 조회에 연결됨. `analysis_ids`와 `llm_call_ids`로 추적 가능 |
| `VALID_RESPONSE_REVIEW_CONTEXT` | 응답은 유효하지만 뉴스 가점에 사용할 확정 근거가 부족하거나 아직 조회 가능한 시각이 아님 |
| `LLM_ATTEMPT_FAILED_OR_INVALID` | 요청은 했으나 HTTP/형식/내용 검증 등을 통과하지 못함. 오류와 호출 상태 확인 |
| `NO_ELIGIBLE_NEWS_OR_BUDGET_BLOCKED` | 최신 원문이 없거나 수집/예산/공급자 대기 조건으로 요청하지 못함. `issues`·`analysis_errors` 확인 |
| `NO_OPENAI_KEY` | 현재 프로세스에 OpenAI 키가 없음 |

기본 진단 기업은 공식 RSS가 등록된 NVDA다. AAPL 등 기본 RSS가 없는 기업은 SEC 등 다른 소스가 필요하다. 최근 24시간 기사·본문·출처/종목 연결이 없으면 오래된 기사나 제목만으로 생성을 강행하지 않는다. 주말에 분석 요청 0회가 나오는 것은 정상일 수 있다. `SEC_USER_AGENT`, OpenAI 등 설정은 [뉴스 설정](NEWS_SETUP.md)을 따른다.

뉴스 진단 성공은 장중 거래 검증 성공과 다르다. 또한 뉴스 부재/LLM 오류가 차트 전략 전체를 중단시키지는 않는다. 미확인 뉴스 가점은 적용하지 않고, 이미 확인된 악재·경제 일정·손절·청산 규칙은 유지한다.

## 3. 장중 실행

```powershell
python -m sessionquant run --config config/sessionquant.realtime-paper.example.yaml
```

브라우저에서 **http://localhost:8501**에 접속하고 상단이 **실데이터 Paper 실험**인지 확인한다. 초기 가상 현금은 $100이다. 실제 계좌 보유/현금은 사용하지 않으며 이 예시에서는 실제 계좌 조회도 꺼져 있다. **매매 시작** 버튼을 눌러야 신규 진입을 판단한다. 기다리는 동안에도 수집은 계속된다.

- 정규장 개장 후 75분부터 신규 진입을 허용한다. 5분/15분 확정봉, 강세장, 유동성·스프레드·RVOL·전략 신호·위험 예산을 모두 통과해야 한다. 신호가 없으면 거래 0건이 정상이다.
- 최대 50−n개 운영 후보에서 최대 20개를 집중 분석하고, 권장 보유 수는 2개다. 소수점 시장가 체결은 가상의 지연·호가 유동성·슬리피지·수수료를 적용한다.
- 일반적인 16:00 미국 동부시간 폐장일에는 13:30 신규/추가매수 마감, 14:40 청산 시작, 15:00 소수점 접수 마감이다. 조기 폐장은 달력에 따라 앞당긴다.
- 전량 매도/매매 종료를 요청했다면 **체결과 잔량 0을 확인한 뒤** 프로그램을 종료한다. 프로세스가 꺼진 동안에는 시세·모의 손절·청산을 처리할 수 없다.

실행 중 별도 터미널에서 외부 호출 없이 요약을 확인할 수 있다.

```powershell
python -m sessionquant paper-status --config config/sessionquant.realtime-paper.example.yaml
python -m sessionquant paper-status --config config/sessionquant.realtime-paper.example.yaml --output paper-status-after-close.json
```

`engine.heartbeat_fresh`는 엔진 연결, `market.quote_fresh_count`는 최근 호가, `strategy_rvol_count`는 현재 전략 전달값, `blocker_counts`는 진입 차단 사유다. `latest_candidates`는 각 평가 시각의 결정이며 현재 유효함을 뜻하지 않는다. `namespace_totals`는 **원장 전체 누계**이며 오늘만의 결과가 아니다. 명령이 보고하는 `order_writes: 0`, `llm_generation_calls: 0`은 상태 조회 자체가 외부 호출/주문을 하지 않았다는 의미다.

대시보드에서는 시장 데이터 표의 **Paper 거래 조건·Paper 가정·전략에 전달한 RVOL·전략 RVOL 근거 상태**를 확인한다. 실제 계좌 자격/RVOL 비교 근거 확보율이 0이어도 Paper 실험 전달값은 별도로 표시한다. 뉴스 분석 화면에서는 기사→분석→호출·오류·토큰/추정 비용을, 거래/전략 화면에서는 후보 차단→신호→주문→가상 체결→청산 사유를 확인한다.

## 4. 장 마감 후 전략 실행 분석

Paper 잔량을 정리하고 엔진을 중지한 뒤 실행한다. 경로는 매번 새 이름을 사용한다.

```powershell
python -m sessionquant record-export --config config/sessionquant.realtime-paper.example.yaml --out data/paper-session-review-01
python -m sessionquant record-quality --bundle data/paper-session-review-01
python -m sessionquant replay-recording --bundle data/paper-session-review-01 --db data/paper-session-replay-01.db
python -m sessionquant evaluate-candidates --help
python -m sessionquant evaluate-recording --help
```

재생은 기록 당시 수신·분석 완료 시각, 일일 감시 대상, Paper 정책과 RVOL 가정을 유지하며 외부 API/LLM을 다시 호출하지 않는다. 거래 조건이 다른 원장의 재시작이나 혼합 정책 기록은 거부한다. 초기 포지션이 없는 시점부터 완전하게 기록해야 재생할 수 있다.

우선 확인할 것은 실제 수익률보다 **진입 조건이 충족됐는지, 뉴스 근거가 판단 시점에 이미 있었는지, 주문과 체결이 구분되는지, 손절/당일 청산이 실행됐는지**다. [후보 선정 검증](STAGE08_CANDIDATES.md)과 [실데이터 기록 연구](STAGE05_RECORDING_BACKTEST.md)의 비교 명령으로 뉴스 가점 유무·전략 차이를 분석할 수 있다. 하루의 거래 결과는 전략 수익성 검증으로 간주하지 않는다.

## Paper 조건과 유지되는 제한

기본값은 여전히 `paper_data_policy: strict`다. 새 예시만 `observed_paper_v1`을 사용한다.

| 입력 | 실데이터 Paper 처리 |
| --- | --- |
| 실제 계좌 거래 가능·소수점 지원 미확인 | 실제 권한은 UNKNOWN 그대로 저장하고 가상의 소수점 거래를 허용 |
| 상장 상태·거래소 | 24시간 이내 수신하고 유효기간이 남은 ACTIVE·지원 거래소 근거 필요 |
| 거래 불가·소수점 불가·거래정지·기업행동이 명확히 확인됨 | 신규 진입 및 Paper 매수 체결 차단 |
| 거래정지·기업행동 상태 미확인 | 미확인 가정을 기록. 최신 정상 호가·분봉 검사는 계속 필요 |
| 동일 제공처·가격 조정 방식·20거래일 동일 경과 분의 완전한 거래량 | 계산 RVOL 사용. 분할 비교 근거가 없으면 `EXPERIMENTAL_UNVERIFIED`로 표시 |
| 분봉 누락/충돌·출처 혼합·비교 거래량 0·시세 만료 | 계산/신규 진입 계속 차단. 보간이나 RVOL 상수 대체 없음 |

원본 적격성, `Quote.tradable`, 검증된 RVOL 필드를 true/VERIFIED로 바꾸지 않는다. 주문 의도와 연구 기록에 실제 적용한 정책·가정·RVOL을 남긴다. 모든 전략 주문은 `PaperBroker`로만 전달한다. 새 정책은 `market_v2` 모의 체결에만 적용한다.

## 이번 환경에서 확인한 범위

- 합성 시세·공식 응답 형태의 모의 RSS/LLM으로 수집→분석 검증→뉴스 가점→기술 신호→가상 매수→체결→손절/세션 청산을 검사했다. 같은 기록 재생의 계좌 상태 일치도 검사했다.
- 공개 NVIDIA RSS는 실제로 20건 수집·해석했다. 검사 당시 최근 24시간 기사는 0건, 최신 발행 시각은 2026-11-12 13:48 UTC였다. 유료 생성을 호출하지 않았다.
- 이 개발 환경에는 토스/OpenAI/SEC 사용자 키가 없었다. **사용자 PC의 인증된 시세·실제 LLM 생성·장중 전체 운영은 아직 검증하지 않았다.** 위 명령으로 로컬 검증 결과를 남길 수 있다.

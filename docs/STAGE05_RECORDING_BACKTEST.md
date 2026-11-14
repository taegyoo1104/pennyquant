# 단계 05 — 실시간 시장 기록·재생·백테스트 평가 체계

최신 `05_실데이터_기록_백테스트.txt`를 기준으로 구현했다. 이번 단계 05는 **실데이터 연구 단계**이며 기존 로컬 실거래 경로의 이름인 Phase 5와 구분한다. 실주문·운영 위험 필터 활성화·06 이후 수집/LLM 재설계는 하지 않는다.

## 실제 연결 경로와 변경 파일

`run → TossSessionSource/기존 뉴스 작업 → SessionEngine의 실제 처리 시계 → research_events → record-export → 오프라인 재생/평가` 경로다. 시장 데이터 API는 기존 토스 `/api/v1/candles`와 현재가·호가를 사용한다. 캔들 1회 200개 제한은 전체 이력 제한이 아니며 `before/nextBefore`로 이전 페이지를 요청한다. 기존 02의 제한적 과거 페이지 보충을 유지하고 20일 확보·분할 거래량 비교 가능성을 임의 승인하지 않는다.

| 영역 | 파일 | 변경 |
|---|---|---|
| 불변 기록 | `sessionquant/research/journal.py` | 원본 장부와 별개로 처리 배치·분석/수신 버전·시계·수동 제어 상태 기록, 계좌 범위 익명화·인증정보 제외 |
| 내보내기·무결성 | `sessionquant/research/bundle.py` | 순번 기반 페이지 읽기·임시 디스크 정렬·새 파일만 생성·파일 및 이벤트 해시 검사 |
| 품질 | `sessionquant/research/quality.py` | 거래일·종목별 최종 수신 커버리지·누락·호가 지연·판단 당시 자격/RVOL 확보율·뉴스 버전·시계 공백 |
| 재생 | `sessionquant/research/replay.py` | 실제 평가 시각 재현, 저장된 뉴스/감시 제한·ETF 상태만 노출, 재시작 커서·설정/입력 바인딩 |
| 평가 | `sessionquant/research/evaluation.py` | 네 단독 전략·A/B/C/D 정책·뉴스/수치 기여 제거 비교, 시간순 검증·최종 홀드아웃 분리 |
| 대용 지표 | `sessionquant/research/proxies.py` | 공개 시점·출처가 맞는 일별 자료에 한정한 상관·표준화 오차·방향 괴리; 장중 금리/현물 변환 금지 |
| 통합 | `sessionquant/session_engine.py`, `session_runtime.py`, `session_config.py`, `macro/service.py` | 실제 사용한 뉴스 모델/프롬프트/분석 시각과 위험 상태를 읽기 전용으로 기록, 시작/종료 경계 |
| DB | `sessionquant/database/{repository,mysql_repository}.py` | `research_events` 원장·스키마 4·기존 기록과 기록 사본의 원자 저장·페이지 조회 |
| CLI·호환 | `sessionquant/cli.py`, `session_backtest.py` | 신규 네 명령, 기존 백테스트도 기존 보고서 덮어쓰기 거부 |
| 설정·검사 | `config/sessionquant.recording.example.yaml`, `tests/test_research_recording.py`, `tests/test_mysql_integration.py`, `tests/test_market_reference.py` | 새 PAPER 기록 예시·결정론·시점/지연/분할·복구·DB·기존 ID 검사 |

## 시각과 재현 계약

각 연구 사건에는 `event_at`, `published_at`, `received_at`, `ingested_at`, `available_at`을 둔다. 알 수 없는 발생·발표 시각은 `null`이며 FRED의 관측 날짜를 발표 시각으로 만들지 않는다. `engine_batch`는 원래 가격 입력과 각 입력의 `timing`, 실제 `decision_at`, 빈 청산 시계, 당시 시작/중지 상태·수동 매도 의도·뉴스 근거·감시 제한·후보 판단을 포함한다. 원본 수신 시각을 현재 시각으로 갱신하지 않는다.

뉴스 공개·수신·분석 완료·해당 처리 시각의 순서와 모델/프롬프트 버전이 있어야 해당 결론을 사용한다. 누락된 과거 분석을 새 LLM 호출로 채우지 않는다. 근거 미확인은 UNKNOWN이며 이전 확인된 진입 제한을 오류만으로 해제하지 않는다. 확인되지 않은 악재를 새로 생성하지 않는다.

품질 보고의 `news_source_verified_count`와 `news_fact_confirmed_rate`를 구분한다. 공식 출처라도 루머·미확정 사건이면 사실 확정으로 세지 않는다. 확정률의 분모는 당시 저장한 분석 버전이며 실제 사건의 정답 정확도나 주가 상승 확률이 아니다.

ETF 참고 자료는 실제 사용 가능 시각 이후에만 재생에 노출한다. 현재 수신된 15분 지연 값은 실시간 값이 되지 않는다. 정정·배당/분할 비교 미확인·권한 미확인도 유지한다. FRED는 별도 일별 기록으로 남으며 당일 새로 확인한 값을 전날 전략에 넣지 않는다. 전체 유니버스 이력의 `effective_at`·`known_at`도 보존하고, 연구 기록에서 실제 확인한 시각보다 앞당겨 노출하지 않는다.

재생은 신규 SQLite Paper 장부만 사용한다. 운영 MySQL·실주문 어댑터·LLM 네트워크를 호출하지 않는다. 같은 입력·설정·정책의 `--resume`은 커서 뒤만 처리하며, 입력/정책 변경 시 기존 장부 복구를 거부한다. 원본 전략 처리 시각의 역전은 정렬로 숨기지 않고 거부한다. 과거 봉의 백필은 원래 봉 시각과 지금의 수신/처리 시각을 따로 보존한다.

재생·평가 식별자에는 `recorded_policy_v1` 계약, 임계값 전체·가설 버전·비용/위험 설정을 포함한다. 묶음 ID에는 파일 해시뿐 아니라 시작 조건과 합성 여부도 포함하므로 메타데이터만 바꿔 같은 묶음으로 복구할 수 없다. 운영 참고 위험 설정은 별도로 기록하고 실험 결과로 자동 변경하지 않는다.

## 로컬 실행 순서

먼저 PyCharm에서 GitHub 클론 프로젝트를 업데이트한다. 기존 실행은 종료한다. 기존 데이터베이스를 삭제하거나 버전 1 체결 장부를 변경하지 않는다.

아래 예시는 기존 `.env`의 로컬 MySQL·토스 키를 사용한다. **새 PAPER 네임스페이스**를 지정하므로 기존 모의 장부와 분리한다. 다음 터미널에서 다시 내보낼 때도 같은 네임스페이스를 지정해야 한다.

```powershell
$env:SQ_DB_NAMESPACE = 'stage05_recording_v1'
.\venv\Scripts\python.exe -m sessionquant doctor
.\venv\Scripts\python.exe -m sessionquant run --config config/sessionquant.recording.example.yaml
```

이 예시는 뉴스/LLM 호출을 끈다. 이 자료만으로 뉴스 기여 효과를 평가할 수는 없다. 기존에 승인·설정한 뉴스 분석까지 기록하려면 해당 기존 설정에서 `recording_enabled: true`를 사용하고 기존 예산을 유지한다. 06·07의 새 뉴스 수집/분류 기능을 먼저 구현하는 것은 아니다.

시작 버튼 전에도 시세를 수집하고 처리 시계를 기록한다. 모의매매를 평가하려면 대시보드의 시작 버튼을 누르고 필수 데이터 검사를 통과해야 한다. 실제 주문은 전송하지 않는다. 중지 기간은 재생에서도 신규 진입을 중지한다. UNKNOWN을 True로 바꾸거나 감시 제한을 우회하는 시험이 아니다.

장중 관측 후 수집을 `Ctrl+C`로 종료하고 같은 터미널에서 새 폴더로 내보낸다.

```powershell
.\venv\Scripts\python.exe -m sessionquant record-export --config config/sessionquant.recording.example.yaml --out data/recording-001
.\venv\Scripts\python.exe -m sessionquant record-quality --bundle data/recording-001

# 같은 기록의 기준선. 아래 DB는 새 파일이어야 한다.
.\venv\Scripts\python.exe -m sessionquant replay-recording --bundle data/recording-001 --db data/replay-recording-001.db --policy baseline

# 네 단독 전략과 정책/기여 제거 비교. 실제 기록이 준비된 뒤 실행한다.
.\venv\Scripts\python.exe -m sessionquant evaluate-recording --bundle data/recording-001 --report data/evaluation-001.json
```

예시 폴더·파일은 위 수집·내보내기를 직접 실행했을 때 생긴다. 이 개발 환경에서 사용자의 실제 기록을 생성한 것은 아니다. 기존 폴더/보고서는 덮어쓰지 않으므로 재실행 시 새 이름을 사용한다.

`record-export`는 작성자 잠금을 사용하므로 같은 엔진을 종료하고 실행한다. 묶음에는 `events.jsonl`, `universe.json`, `quality.json`, `manifest.json`이 들어간다. 이전 기록에 처리 시각이 없으면 별도 `legacy.jsonl`에 보존하고 재생 성과로 사용하지 않는다. 기존 원장만으로 소급해 완전한 당시 판단을 만들 수는 없다.

첫 기록이 이미 보유·미결 주문을 가진 중간 시점이면 재생을 거부한다. 새 빈 PAPER 실행 공간을 사용하거나, 이후 빈 계좌에서 시작한 `recording_start.run_id`를 확인해 `record-export --run-id ...`로 선택한다. 선택 실행에도 필요한 유니버스·가격·역사 입력이 있어야 한다.

Workbench에서 위 예시의 기록 종류와 시작 경계를 확인하려면 사용하는 MySQL DB에서 아래 SELECT를 실행한다. 네임스페이스를 다르게 지정했다면 같은 값으로 바꾼다.

```sql
SELECT JSON_UNQUOTE(JSON_EXTRACT(payload, '$.kind')) AS kind, COUNT(*) AS records
FROM pq_research_events
WHERE namespace = 'paper_local_stage05_recording_v1'
GROUP BY kind;

SELECT JSON_UNQUOTE(JSON_EXTRACT(payload, '$.run_id')) AS run_id,
       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.available_at')) AS available_at,
       JSON_EXTRACT(payload, '$.data.initial_flat') AS initial_flat
FROM pq_research_events
WHERE namespace = 'paper_local_stage05_recording_v1'
  AND JSON_UNQUOTE(JSON_EXTRACT(payload, '$.kind')) = 'recording_start'
ORDER BY seq;
```

## 정책 비교와 통계

| 정책 | 재생 동작 |
|---|---|
| `baseline` | 기존 SPY·QQQ 기반 전략·기업/거시 뉴스 제한 |
| `shadow` | ETF 수치 감시만 추가. 기준선과 금융 결과가 같아야 함 |
| `reference` | 검증된 위험 변화에서 신규 진입·추가매수 제한. 신규 UNKNOWN은 fail-open 실험이나 기존 위험 제한은 유지 |
| `fail_closed` | 위 제한 + 결측/지연/UNKNOWN에서 신규 진입 보류 |

운영 `macro_reference_filter`의 OFF·shadow 정책은 유지한다. ON 실험은 이 격리 재생 경로에만 허용한다. ETF 신호로 보유 종목을 강제 매도하지 않고 종목 손절·시간 청산·수동 종료를 계속한다.

기본 전체 비교는 네 단독 전략 × 네 정책 16개와 주 전략의 `numeric_only`, `neither_news_nor_reference`, `no_add`, `technical_only` 4개로 총 20개다. 뉴스만/수치만/둘 다/둘 다 제외를 비교한다. 한 전략만 선택하려면 `--strategy pullback`을 사용한다. 임계값을 자동 탐색하거나 최적값을 추천하지 않는다.

`NO_OBSERVATION`·`DELAYED`·`UNKNOWN`·`VERIFIED` 자료 상태별 판단 수와 진입 가능 수를 따로 센다. 이를 아무 신호도 없었던 구간으로 합치지 않는다. 차단된 신호 중 동일 기준선의 완료 거래에서 이익이 났던 비율은 조건부 false positive 기술 통계이며, 포지션 변화·미체결·대체 진입의 인과 효과를 뜻하지 않는다.

보고서에는 순손익·실현손익·거래 수·승률·Profit Factor·기대수익·MDD·평균 보유시간·비용·미청산·세션 청산 성공률·국면별 결과·차단 근거가 있다. 가격/호가 공백 뒤에 가상의 체결을 만들지 않는다. 기록이 끝났을 때 미청산이면 그대로 보고하며 미래 호가로 마지막 잔량을 청산해 성과를 높이지 않는다.

기본 분할은 학습 20·검증 5·최종 홀드아웃 5거래일, 최소 완료 거래 30개라는 **초기 연구 기준**이다. 날짜가 충분하면 학습 기간을 확장하는 순차 검증 fold를 만들고 마지막 홀드아웃은 모든 개발/검증에서 제외한다. 부족하면 `INSUFFICIENT_SESSIONS`/`insufficient_trades`다. 20일만으로 통계적 수익성을 입증하지 않는다. 재생은 평가 시작 전 기록으로 지표만 준비하고 해당 평가 구간에서만 신규 진입을 허용한다.

이 구현은 단일 기간 최고 성과를 운영 설정에 반영하지 않는다. 보고서를 보고 가설을 바꾸면 새 미래 홀드아웃을 확보해야 한다. JSON의 `performance`는 아직 `null`, 수치는 `diagnostic_simulation`이며 `actual_out_of_sample_validated=false`다. 실제 데이터·실비·체결 및 표본을 사람이 검토하기 전 실제 운용 효과로 발표하지 않는다.

## 금리·원유 대용 분석과 비용

공개/수신 시각과 기업행동 비교 가능성이 확인된 완료 일봉·FRED 일별 자료만 같은 기간으로 맞춰 비교한다. 상관·단위 없는 표준화 방향 오차·방향 괴리 사례를 기록한다. 원래 날짜에 실제 확인하지 못했던 값은 제외한다. TLT를 10년물 금리로 표시하거나 ETF 수익률을 bp·WTI 현물 변화로 환산하지 않는다. 실제 동일 시각 금리/현물 원장이 없는 장중 효과, USO 롤·배당락/분할의 확정 가짜 신호율은 `UNVERIFIABLE`/`null`로 남는다.

검증된 실제 토큰 사용량·단가가 있는 `llm_usage`만 알려진 LLM 비용으로 센다. API·인프라 실비는 확인 후 `--api-cost-usd`, `--infrastructure-cost-usd`로 별도 표시할 수 있다. 미확인 비용을 0으로 만들거나 서로 다른 평가 구간/정책에 임의 배분하지 않는다. 매매 진단 손익에는 같은 모의 수수료·스프레드·슬리피지 가정을 사용하고, 미래 청산·LLM·인프라 실비의 포함 여부를 명시한다. 실제 브로커 체결·수수료 실측은 후속 실거래 검증 영역이다.

## 호환성·보안·미완료

스키마 4는 `research_events`만 추가하며 MySQL은 기존 `pq_` 접두사·네임스페이스·기록 해시/시각 인덱스를 사용한다. 기존 주문·사건·해시는 재작성하지 않는다. SQL 원장 사본은 같은 트랜잭션에 기록하므로 실패 시 원본 변경도 롤백된다. 재생 장부와 수집 원장을 섞지 않는다. `.env`·계좌 보유·잔고·인증 헤더는 내보내지 않고 URL 인증/쿼리·비밀 키를 제거한다. 기존 모든 비밀 문자열을 자동 식별한다고 보장하는 기능은 아니다. 공유 전 뉴스 본문 등도 확인한다.

빈 청산 시계·원본 배치·분석 버전을 보존하므로 DB 사용량이 늘어난다. 새 원장은 자동 삭제 대상이 아니다. 저장량은 실제 `db-stats --db ...`의 같은 네임스페이스에서 측정하고 장기 보존 정책은 별도로 정한다. 내보내기 정렬은 임시 디스크를 사용하므로 원장 크기에 비례한 여유 공간이 필요하다.

실제 사용자 토스·MySQL·유료 LLM을 개발 환경에서 호출하지 않았다. 사용자 PC에서 실제 장중 자료를 충분히 모으고, 시총 공급·계좌 자격·20일 RVOL 비교 근거·ETF 실시간 권한을 검증해야 한다. 시총 목록이 UNKNOWN이면 현재 집중 후보도 없어 SPY·QQQ 등 우선 자료 위주로 기록될 수 있다. 주문/거래가 0건인 상태를 수익성 검증 완료로 해석하지 않는다. 이 선행 부족을 해소하기 위해 임의 100종목·계좌 자격·기업행동 정보를 생성하지 않았다.

판정은 **코드·합성/격리 DB 검증과 실제 수집 워크플로 구현, 실제 장기간 외표본 효과는 미검증**이다. 06은 별도 승인 전 보류한다.

## 검증 결과 — 2026-11-14

| 검사 | 실행 결과 |
|---|---|
| 변경 전 `python -m pytest -q` | 900 통과·선택 MySQL 13 제외·실패 0 |
| 신규 `tests/test_research_recording.py` | 최종 전체에 33개 사례 포함: 동일 금융 결과·두 번 재생·복구·메타데이터/파일 변조·과거 백필·미래/역전·뉴스 버전·뉴스 확정률·지연/정정·FRED 날짜·대용 상관·출처 혼합·조기 폐장·외표본·기존 보고서 보존 |
| 연구·참고 위험·SQL 바인딩 집중 검사 | 74 통과. 이후 추가한 뉴스 확정률 3개도 최종 전체에서 통과 |
| 실제 격리 MySQL 8 `PQ_TEST_MYSQL=1 python -m pytest tests/test_mysql_integration.py -q` | 14 통과·실패 0. 사용자 DB가 아닌 임의 테스트 네임스페이스 사용 |
| **최종 전체 `PQ_TEST_MYSQL=1 python -m pytest -q`** | **947 통과·실패/제외 0**, 114.22초 |
| `python -m ruff check sessionquant tests`, `git diff --check` | 통과 |
| wheel 빌드·소스 폴더 밖 `doctor`/신규 도움말·새 기록 설정 로드 | 통과 |

검사 집합은 서로 겹치므로 합산하지 않는다. 기존 pandas 시간대 배열 경고 8개가 남아 있다. SQL 삽입 방지 테스트는 추가 상태 조회 뒤의 마지막 문장이 아닌 검증 대상 원본 INSERT를 명시적으로 선택하도록 수정했고, 해시 길이·바인딩·원본 JSON 동등성 조건은 유지했다. 기존 주문/이벤트 ID·거래 자격·손절·취소·청산·일일 위험·대시보드 회귀도 전체 검사에 포함했다.

이번 검증에서 실제 주문·사용자 계좌 API·유료 LLM 호출은 0회다. 전용 MySQL 컨테이너의 자체 테스트 기록만 정리하고 종료한다. 실제 장중 기록과 수익성 보고서를 생성했다고 주장하지 않는다.

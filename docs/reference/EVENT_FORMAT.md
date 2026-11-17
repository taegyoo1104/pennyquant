# 시장 입력과 모델 평가 자료

`run`의 기본 소스 `toss`는 읽기 전용 관측이다. 미국 종목의 계좌별 거래 가능·소수점 지원 자료를 API에서 확인할 수 없으면 WATCH를 유지한다. 다음 입력은 확인한 자료 공급자가 만든 JSONL을 사용하는 경로다. 합성 값을 실제 관측 파일에 붙이지 않는다.

`config/sessionquant.yaml`을 별도로 복사하고 `source: file`, `input_path: ../data/verified-events.jsonl`을 설정한다. 경로는 설정 파일 기준이다. 모든 이벤트는 `{"kind":"...","data":{...}}` 한 줄이며 사용 가능 시각 오름차순으로 기록한다. 시각에는 UTC `+00:00` 또는 명시적 시간대가 있어야 한다.

| kind | data 필드 | 사용 가능 시각 |
|---|---|---|
| bar | symbol,timestamp,received_at,open,high,low,close,volume | received_at |
| quote | symbol,timestamp,received_at,bid,ask,bid_size,ask_size,tradable,halted,exchange | received_at |
| eligibility | symbol,checked_at,source,tradable,fractional,halted,corporate_action,exchange,volume_history_verified | checked_at |
| rvol | symbol,value,observed_at,verified | observed_at |
| news | id,symbols,title,body,source,url,published_at,received_at,verified,real_time | received_at |
| risk_context | scope(company/macro),symbol 또는 *,available_at,source,verified,block_entries,major_adverse,blackout,reasons | available_at |

`bar.timestamp`는 확정된 1분봉의 **종료 시각**이다. 임의의 5분봉을 1분봉으로 입력하지 않는다. 기업행동 비교 가능성을 확인한 과거 20거래일이 없으면 외부의 검증된 RVOL을 제공하거나 미확인 상태로 둔다. eligibility의 미확인 필드는 `null`이며 새로운 매수를 차단한다. SPY·QQQ의 봉과 최신 호가도 같은 파일에 필요하다. 뉴스 위험 이벤트는 본 시스템에서 검증·분석이 완료된 결과를 기록 재생할 때 사용하며 원문 기사의 수신 시각으로 완료 시각을 앞당기면 안 된다.

SessionEngine의 시작 예열에서 DB 분봉을 복원한 내부 이벤트에는 최상위 `restored_at`이 추가된다. 원본 `data.received_at`은 유지하고, 이번 실행에서 사용할 수 있게 된 복원 시각으로 이벤트를 정렬한다. 복원 시각은 원본 수신 이후·처리 시각 이하여야 하며 bar에만 허용한다. 연구 기록은 두 시각을 보존해 재생한다. 이 필드가 없는 기존 이벤트는 위 표의 시각을 그대로 사용한다.

수집·저장 처리 때문에 엔진의 마지막 처리 시각보다 늦게 전달된 `bar`와 `eligibility`에는 최상위 `delivered_at`을 기록한다. 원본 분봉 수신 시각과 상장 정보 확인 시각은 변경하지 않는다. 전달 시각은 원본 수신·복원 이후, 판단 시각 이하여야 한다. 호가에는 이 필드를 허용하지 않으며 오래된 호가를 새 호가처럼 사용하지 않는다. 연구 기록과 재생은 원본 시각 및 실제 전달 시각을 함께 유지한다.

처리한 파일의 접두부가 바뀌거나 잘리면 복구를 거부한다. 재생에서는 입력과 유니버스 파일 해시를 함께 확인한다. 기존 기록을 수정하려면 새 실험 DB를 사용한다.

## 경제 발표 일정

공식 발표를 확인한 일정은 JSON 배열로 준비한다. 각 항목은 `id,name,scheduled_at,known_at,source_url,importance,verified`를 갖는다. `importance`는 `HIGH`, `verified`는 실제로 확인한 경우에만 `true`다. 공식 HTTPS URL을 요구한다. 일정 공급을 자동으로 보장하지 않으며 등록되지 않은 일정은 미확인이다.

```powershell
.\venv\Scripts\python.exe -m sessionquant calendar-import --input config/economic-calendar.json
```

엔진을 종료한 뒤 가져오고 재시작한다. 확인된 중요 발표 전후 15분은 신규 진입을 제한한다.

## 모델 비교

기본 분석 모델은 기존 `gpt-4.1-mini`를 유지한다. 후보 모델과 한국어 프롬프트의 실제 정확도는 API 키가 없어 이번 개발에서 비교하지 않았다. 내부 영어 프롬프트를 유지하며, 모델 비교 도구는 기본적으로 유료 호출을 하지 않는다.

평가 JSONL은 각 줄에 `ticker`, `expected_sentiment`(POSITIVE/NEGATIVE/NEUTRAL/MIXED/UNKNOWN), `data`(위 news 형식)를 넣는다. 부정문·과거·루머·확정 공시 등을 균형 있게 라벨링한다. 평가는 과거 본문을 현재 평가 시각으로 바꿔 분류기만 비교하며 거래 엔진에 입력하지 않는다.

```powershell
# 호출 없는 입력 점검
.\venv\Scripts\python.exe -m sessionquant llm-evaluate --input data/evaluation.jsonl
# 실제 호출과 과금은 본인 PC에서 명시적으로 선택
.\venv\Scripts\python.exe -m sessionquant llm-evaluate --input data/evaluation.jsonl --execute
```

결과는 모델별 라벨 일치율·오류·실제 토큰·지연·설정 단가에 따른 추정 비용을 저장한다. 기본 도구는 최대 20개 사례이며 비교용 호출은 운영 월간 예산과 별개다. 접근 권한이 없는 모델은 오류로 기록된다. 보고서를 검토한 뒤 운영 YAML의 `llm.primary_model`을 `gpt-6-luna` 등으로 선택할 수 있다. 한국어 프롬프트는 별도 평가 자료로 검증하기 전 자동 전환하지 않는다.

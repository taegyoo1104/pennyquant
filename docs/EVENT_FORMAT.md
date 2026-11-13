# 정규화 이벤트 입력

UTF-8 JSONL이며 한 줄에 `{ "kind": "...", "data": {...} }`를 기록하고 개행을 끝에 붙입니다. 허용 kind는 `news`, `quote`, `snapshot`, `bar`입니다. 생산자는 파일에 단독으로 쓰고 수신 시각으로 정렬합니다. 거래 수량·호가 수량은 음수가 아닌 정수, 가격은 유한한 양수, 불리언은 실제 JSON true/false여야 합니다. NaN·시간대 없는 시각을 쓰지 마세요.

모든 시각은 UTC 오프셋이 있는 ISO 8601로 작성합니다. `timestamp`는 시장 이벤트 시각, `received_at`은 시스템이 실제로 수신한 시각입니다. **1분봉 timestamp는 봉 종료 시각**입니다. 발표 시각은 뉴스의 `published_at`에 별도로 기록합니다.

## 호가

```json
{"kind":"quote","data":{"symbol":"DEMO","timestamp":"2026-11-13T14:35:00+00:00","received_at":"2026-11-13T14:35:00.050+00:00","bid":2.178,"ask":2.182,"bid_size":100,"ask_size":100,"tradable":true,"halted":false,"exchange":"NASDAQ"}}
```

위 수치는 형식 설명용입니다. `tradable=true`, `halted=false`, `exchange`는 신뢰할 수 있는 최신 원천으로 확인해야 합니다. `bid`는 매도 가능한 가격, `ask`는 매수 가능한 가격입니다. 같은 symbol·시장 timestamp는 같은 호가 잔량 예산으로 취급해 재수신으로 체결 가능 수량을 늘리지 않습니다.

## 스캐너 snapshot

```json
{"kind":"snapshot","data":{"quote":{"symbol":"DEMO","timestamp":"2026-11-13T14:35:00+00:00","received_at":"2026-11-13T14:35:00.050+00:00","bid":2.178,"ask":2.182,"bid_size":100,"ask_size":100,"tradable":true,"halted":false,"exchange":"NASDAQ"},"prev_close":1.75,"cumulative_volume":1500000,"dollar_volume":3100000,"relative_volume":null,"day_high":2.25,"corporate_action":false,"dilution_risk":false}}
```

quote 필드는 호가와 동일합니다. `relative_volume=null`이면 진입을 차단합니다. 당일 거래대금을 현재가×누적 거래량으로 대체하거나 RVOL을 당일 거래량/일평균 거래량으로 대체하지 마세요. 과거 같은 세션 경과시각의 누적 거래량 평균이 기준입니다. 당일 고가·기업행동·중대한 희석 여부도 당시 확인한 사실을 기록합니다.

## 완료된 1분봉

```json
{"kind":"bar","data":{"symbol":"DEMO","timestamp":"2026-11-13T14:35:00+00:00","received_at":"2026-11-13T14:35:00.100+00:00","open":2.15,"high":2.19,"low":2.14,"close":2.18,"volume":2500}}
```

봉의 실제 종료와 수신 이후에만 전략이 사용합니다. 중복·수정 봉은 이미 관측한 봉을 덮어쓰지 않습니다. 1분 간격의 필요한 구간이 빠지면 신호를 만들지 않습니다. 주문은 신호 생성에 쓴 호가나 봉에서 체결되지 않고, 생성 이후의 실제 quote가 있어야 합니다.

## 뉴스

```json
{"kind":"news","data":{"id":"provider:article-id","symbols":["DEMO"],"title":"SYNTHETIC example: FDA approves treatment","published_at":"2026-11-13T14:29:00+00:00","received_at":"2026-11-13T14:29:01+00:00","source":"SYNTHETIC_FIXTURE","url":"https://example.invalid/article","body":"","verified":true,"real_time":true}}
```

기사 ID·티커·출처·시간을 실제 원천에서 기록합니다. `real_time=true`는 해당 권한과 실제 수신 경로를 확인한 경우에만 사용합니다. REST 과거 기사와 SEC/RSS 보조 수집은 false입니다. 양의 이벤트도 이후 시장 반응·유동성·전략·위험 조건이 모두 맞아야 주문 후보가 됩니다.

## 파일 메타데이터

`events.jsonl` 옆 `events.meta.json`에 `synthetic`, `source_label` 등을 기록할 수 있습니다. 이 메타데이터는 사람이 읽을 출처 표시이며 실시간 권한을 증명하거나 거래 조건을 완화하지 않습니다. 합성 테이프를 외부 실제 관측 파일로 재분류하지 마세요. 예시 전체 파일은 `examples/synthetic.jsonl`입니다.

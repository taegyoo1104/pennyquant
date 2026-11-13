# PennyQuant 합성 데이터 기능 검증 보고서

기간: 2026-10-26T13:29:30+00:00 ~ 2026-11-06T14:50:02+00:00
세션: 10 / 이벤트: 810

실제 시장 수익성 검증이나 실거래 승인 결과가 아닙니다.

| 전략 | 종료 자산 USD | 순손익 USD | 완료 거래 | 승률 | PF | MDD | 검증구간 손익 USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| momentum | 75.3882 | 5.3882 | 10 | 70.0% | 2.49 | 3.09% | 2.4901 |
| pullback | 61.8750 | -8.1250 | 10 | 0.0% | 0.00 | 12.18% | -3.1502 |
| gap_breakout | 61.1664 | -8.8336 | 10 | 0.0% | 0.00 | 12.88% | -3.5534 |
| vwap | 79.4712 | 9.4712 | 7 | 100.0% | 계산 불가 | 0.11% | 3.7885 |

각 전략은 같은 이벤트·자본·비용 조건을 사용합니다. 파라미터 최적화는 수행하지 않았습니다.
마지막 자산에는 보유 포지션의 매수호가 평가액이 포함되며, 완료 거래의 손익과 다를 수 있습니다.
거래가 적거나 손실 거래가 없는 경우 PF는 계산 불가로 표시합니다.

## 해석 제한

- Synthetic/demo data cannot establish profitability or live readiness.
- News is available only after both publication and recorded receipt. Historical receipt provenance is not inferred.
- Quotes must follow order creation; displayed size is consumed once per quote. Queue position and depth beyond level 1 are unknown.
- Stops are local synthetic limit exits. Gaps, halts and failed liquidity can leave positions open.
- Final equity includes open positions marked at bid; no fabricated end-of-period liquidation is inserted.
- Fees are illustrative symmetric basis-point costs; broker-specific minimums and regulatory/FX fees require configuration work.

실거래 승인: 보류. 4주 이상의 실시간 모의매매와 충분한 표본, 계정별 API 복구 검증이 필요합니다.

> **과거 기록 · 현재 실행 지침 아님** · 아래 명령·설정·완료 상태는 작성 당시의 기록입니다. 현재 절차는 [프로젝트 소개](../../../README.md)와 [문서 안내](../../README.md)를 따르세요. [보관 문서 안내](../README.md)

# 08 뉴스·업종 상대강도와 포트폴리오 최종 선정

기준: 2026-11-15, 사용자 첨부 `08_종목순위_뉴스_포트폴리오.txt`. 작업 시작 HEAD `329ea7a`, WIP 브랜치 `wip/stage06-handoff-20261115`. [프로젝트 방향](../PROJECT_DIRECTION.md)에 따라 08만 수행한다. 08A 전체 UI 개편·09/10 실거래 연결·GCP 배포·KIS는 이번 범위에 포함하지 않는다.

08 완료 커밋은 `8a328e7`이다. 사용자가 이후 승인한 시계·비용·CI 등 후속 보완은 [00~08 종합 검토](../REVIEW_00_08.md)를 따른다.

## 실제 연결 경로

`run → SessionConfig.candidate_selection → SessionEngine → WatchlistManager.candidate_symbols → 같은 now의 MarketEngine 평가 → PortfolioCandidateSelector → PortfolioManager의 기존 위험·비중 검사 → Paper 주문 의도 → 다음 유효 호가 체결` 순서다.

기존에는 새 호가가 들어온 종목끼리만 기본 뉴스 점수로 비교했다. 이제 새 후보 호가를 수신하면 **현재 집중 목록 전체와 이번 수신 종목**을 같은 평가 시각으로 검사한다. 집중 밖의 종목은 기존 감시 범위 검사에서 제외한다. 필수 자료가 부족한 후보도 제외 사유와 함께 기록한다. 빈 청산 tick에는 전체 후보 평가를 반복하지 않으며 기존 위험·청산 작업을 먼저 처리한다.

| 영역 | 주요 파일 |
|---|---|
| 기업 뉴스 참고 신호 | `sessionquant/news/ranking.py`, `macro/service.py` |
| 업종·지수 비교 | `portfolio/strength.py` |
| 후보 순위·시간 정렬·분산 제약 | `portfolio/selection.py` |
| 실제 진입 연결·복구 | `session_engine.py`, `portfolio/manager.py`, `watchlist/selection.py` |
| 기동·설정 | `session_config.py`, `session_runtime.py`, [설정 예시](../../../config/sessionquant.candidates.example.yaml) |
| 저장된 선정 화면 | `dashboard/selection_panel.py`, `dashboard/session_app.py` |
| 당시 입력 기록·비교 | `research/journal.py`, `research/replay.py`, `research/candidate_evaluation.py`, `cli.py` |
| 조회·검증 | [MySQL SELECT 진단](../../../sql/portfolio_candidate_diagnostics.sql), `tests/test_stage08_*.py`, `tests/test_mysql_integration.py` |

## 종목 점수와 위험 차단

기존 실험 가중치인 추세 25/RVOL 20/시장 20/업종 15/유동성 10/기업 뉴스 10을 유지한다. `candidate_score`와 `macro_reference_risk`는 별도 필드다. 점수는 후보 간 비교용이며 주문 허가나 수익 확률이 아니다.

기업 뉴스 가점은 07의 검증된 분석만 사용한다. 종목·공식 1차 출처·발표/수신/분석 시각·24시간 유효성, 오류 없음, confidence 0.75 이상, 누락 정보 없음, 근거 인용과 해시, 모델/프롬프트/채택 호출 ID를 확인한다. 긍정은 1, 검증된 중립은 0.5, 검증된 부정·희석은 0이다. 중대한 기업 악재·경제 발표·확인된 거시 위험의 차단은 기존 `NewsPipeline.risk_context`가 별도로 유지한다.

미확인·루머·예정·오래된 기사·LLM 실패·공식 확인 미충족은 `UNKNOWN/value=null`, 가점 기여 0이다. 같은 기사의 나중 실패 버전이 있으면 과거 긍정 가점을 되살리지 않는다. 같은 사실의 여러 기사를 합산해 가점을 부풀리지 않는다. 거시 LLM 설명과 일반 거시 호재를 모든 종목의 가점으로 변환하지 않는다. 뉴스가 없어도 기술적 조건으로 후보가 될 수 있다.

시장 참고 ETF `SPY/QQQ/SHY/IEF/TLT/USO`는 감시 100/집중 20/최종 후보에 섞지 않는다. `PortfolioManager.plan_entry`도 해당 ETF의 BUY를 독립 차단한다. 기존 보유 ETF의 매도·청산은 허용한다. USO 상승을 에너지주 수익으로 환산하지 않는다.

04의 시장 참고 위험 필터는 기존 기본 OFF/shadow를 유지한다. 합성/승인된 오프라인 재생에서 ON이면 검증된 위험 및 fail-closed UNKNOWN을 진입 게이트에 반영하고 이유를 기록한다. 운영 필터를 이번 단계에서 임의 활성화하지 않았다. 기업 악재·일일 손실·매매 시간·청산 규칙도 높은 점수로 우회하지 못한다.

## 업종 상대강도와 지수 상관

새 외부 ETF API나 임의 섹터 값을 추가하지 않았다. 첨부가 허용한 **비교 가능한 S&P 섹터 데이터** 경로를 구현한다.

업종은 당시 유효한 S&P 구성 스냅샷의 sector를 사용한다. 해당 업종 전체 구성 종목(최소 2개)의 정확히 같은 15분 구간 16개 확정 1분봉과 SPY를 대조한다. 업종 내 종목의 15분 수익률을 동일 가중 평균하고 SPY 수익률을 뺀다. 초기 정규화는 `clip(0.5 + 초과수익률 / 0.04, 0, 1)`이다. 시총 가중 공식 섹터 지수와 구분하며 검증 전 가중치를 최적화하지 않는다.

전체 구성원·기간·수신 시각·공급자·조정 방식과 기업행동 비교 근거가 필요하다. 완전한 업종 자료를 일부 집중 종목 평균으로 대체하지 않는다. 실제 운영 경로에서는 유효한 `volume_evidence`의 출처·분할/비교 정책·포괄 기간을 가격 원본과 대조한다. 데이터 누락·미래 수신·출처/조정 혼합이면 UNKNOWN이다. 합성 예외는 명시적 synthetic 실행과 SYNTHETIC 원본에 한정하며 실제 공급자 검증으로 표시하지 않는다.

따라서 현재 토스 수집의 실제 권한·기업행동 근거·업종 전체 커버리지가 부족한 환경에서는 **업종 점수가 UNKNOWN인 것이 정상**이다. 단일 섹터 ETF 수집·대체 추정은 이번 구현에 추가하지 않았다. 실제 비교 가능한 원본이 준비되어야 운영 효과를 평가할 수 있다.

선택적 지수 중복 제약은 같은 구간의 종목/SPY 1분 수익률로 Pearson 상관을 계산한다. 기본 30개 수익률, 설정 20~120개다. 입력 출처·기간·조정/비교 근거를 동일하게 검사하고 상수 시계열·미확보는 UNKNOWN으로 둔다. 높은 지수 상관은 기본 0.85 이상이라는 가설이다. 실제 장기간 상관의 안정성을 보증하지 않는다.

## 같은 시점의 후보 비교와 실제 승인

- 현재 세션의 가장 최근 완료 5분 경계와 각 종목의 `last_closed_at`이 같아야 한다.
- 후보 호가는 기존 PortfolioSettings의 신선도(기본 10초)를 통과하고, 가장 최근 유효 후보 호가 시각과 기본 2초 이내로 정렬되어야 한다. 늦게 받은 가격을 최신 시세로 바꾸지 않는다.
- 필수 적격성·RVOL·봉·시장·감시 범위·뉴스 위험을 먼저 검사한다. 점수는 해당 게이트를 해제하지 않는다.
- 같은 종목의 중복 전략 신호는 한 후보로 제한한다. 현재 운영은 기존처럼 전략 1종이며 네 전략의 단독 실험을 보존한다. 동시 다중 전략 기능을 활성화하지 않았다.
- 포트폴리오의 기존 보유·매수 예약·취소 대기·청산 의도를 확인한다. 분산 제약으로 제외된 상위 후보는 최종 후보 수를 소진하지 않는다.
- 기본 최대 5개를 최종 배분 검사 대상으로 검토하고, 기존 `PortfolioManager`에서 현금·예약·예상 비용·손절 위험·비중·일일 손실·거래 시간창을 다시 계산한다. 검사 실패 시 다음 비교 후보를 검토하며 실제 승인 수는 0일 수 있다.

| 설정 구분 | 기본값·의미 |
|---|---|
| 운영 권장 목표 | 보유 + 매수 예약 2종목 중심. 신규 주문으로 억지로 채우지 않음 |
| `allow_above_preferred` | 기본 false. true로 명시한 별도 실험에서는 권장 수를 넘겨도 기존 hard limit 검사 |
| 보유 hard limit | 기존 최대 5종목. 기존 초과 노출을 목표 2 때문에 강제로 매도하지 않음 |
| 비중·위험 | 최초20%/추가20%/종목40%/총80%/종목위험0.75%/일일2% 유지 |
| `max_same_sector` | 기본 null(제약 OFF). 1~5개로 설정하면 보유·매수 예약의 동일 업종 수 제한 |
| `max_high_index_correlation` | 기본 null(제약 OFF). 높은 SPY 상관 종목의 중복 노출 수 제한 |
| `fail_closed_diversification` | 기본 true. 제약을 켠 상태에서 필요한 업종/상관 근거가 없으면 신규 진입 보류 |

분산 제약 OFF에서도 같은 업종·높은 지수 상관 노출과 UNKNOWN을 기록한다. 추가매수는 새 종목 슬롯과 구분하고 기존 추세·지지·반등·최대 1회·위험 재계산을 그대로 거친다.

## 기록·화면·기존 호환

선정 프레임은 기존 `pipeline_status(kind=portfolio_selection)`와 `portfolio_selection_v1` control에 추가한다. DB 스키마는 6을 유지하며 신규 테이블이나 기존 ID 재작성은 없다. `candidates`의 기존 symbol ID를 유지하고 최신 selection을 덧붙인다. 불변 선정 프레임 ID와 기존 `signal_id`, `order_intents.selection_id/candidate_rank/candidate_score`로 실제 승인까지 조회한다. 차단·배분 거절·추가매수·승인·미체결을 구분한다.

기존 메인 5개 탭 안의 종목 현황에 감시 100 → 집중 최대 20 → 비교 가능 후보 → 최종 후보 → 신규/추가 승인과 사유·원본 시각을 표시한다. 순위는 호가·자격·RVOL·뉴스 등 확인한 유효기간에 맞춰 만료한다. 만료 시 현재 수치는 `—`이며 마지막 선정 이력임을 명시한다. 08A의 6개 탭 재배치와 매매 제어 개편은 수행하지 않았다.

`run`의 새 후보 선정은 기본 enabled다. 직접 호출하는 기존 SessionEngine/과거 단순 replay/demo는 명시하지 않으면 종전 경로를 유지한다. 07 이전 상태에 선정 정책 필드가 없으면 기존 주문·잔고를 보존하며 08 메타데이터를 추가한다. 08 정책을 저장한 뒤 옵션을 바꾸는 경우 새 DB/namespace를 요구해 다른 선정 정책의 장부를 섞지 않는다. [설정 예시](../../../config/sessionquant.candidates.example.yaml)는 새 실행 공간을 사용한다.

실제 주문 어댑터·권한·승인 플래그는 바꾸지 않았다. 신규 정책을 실제 계좌로 연결하지 않았으며 KIS·GCP를 추가하지 않았다.

## 같은 기록의 가점 비교

05 불변 기록에 당시 후보 범위·뉴스 점수 근거·선정 프레임·설정을 추가했다. 재생은 해당 시점의 분석만 읽으며 새 LLM 호출이나 현재 뉴스를 보충하지 않는다. 내보낸 유니버스의 최초 이용 가능 시각을 보존하므로 재생의 선정 ID는 원본과 다를 수 있다. `source_selection_id`로 원본을 연결하며 같은 동결 묶음을 두 번 재생하면 동일 주문·선정 결과를 얻는다.

```bash
# 기존 실행과 함께 쓰지 않는 새 PAPER 실행 공간에서 기록한다.
python -m sessionquant run --config config/sessionquant.candidates.example.yaml

# 수집 종료 뒤 기존 05 명령으로 새 묶음을 내보낸다.
python -m sessionquant record-export --config config/sessionquant.candidates.example.yaml --out data/candidate-recording-001
python -m sessionquant record-quality --bundle data/candidate-recording-001

# 같은 원본·비용·현금·전략·위험 게이트에 뉴스/업종 가점만 각각 적용한다.
python -m sessionquant evaluate-candidates --bundle data/candidate-recording-001 --report data/candidate-comparison-001.json
```

비교는 `technical_liquidity/company_news/sector_strength/combined` 네 조합이다. 각 조합의 실제 저장된 진단 주문·수익 지표·검증된 가점 표본 수를 보고하며 운영 설정을 자동 변경하지 않는다. 08 후보 입력을 기록하지 않은 과거 묶음은 거절한다. 누락된 과거 LLM 결과나 집중 목록을 추측하지 않는다. 보고서는 새 경로만 생성한다.

이번 개발 검증은 합성 자료·모의 HTTP·격리 DB다. **동일 실제 시장 기록을 사용한 성과 비교는 사용자 기록이 없어 미실행**이며, 명령 경로·결정론·가점 전후 선택 차이만 합성 자료로 검사했다. 모델·전략 효과나 수익성으로 해석하지 않는다.

## 검증과 남은 운영 확인

최종 횟수와 명령은 [HANDOFF 최신 08 절](../../../HANDOFF.md)에 기록한다. 주요 검사는 `python -m pytest -q`(전용 MySQL opt-in 포함), `python -m ruff check sessionquant tests`, `python -m sessionquant doctor`, `git diff --check`다. 8단계 집중 검사는 같은/다른 업종, 지수 상관 중복, 순위 아래 대체 후보, 여러 전략 중복, 시각 불일치, 출처·인용·루머·오래된 뉴스, LLM 실패, 보유·예약·권장/최대 제한, 일일 손실, OFF/ON/UNKNOWN 위험, 청산 독립, 복구/저장 실패, 재생 비교, UI 만료, MySQL namespace와 SQL 연결을 포함한다.

남은 실제 확인은 토스 계좌 적격성·RVOL/기업행동 비교 근거, 업종 전체 동시 구간 커버리지, 공급자 지연·성능, 실제 장기간 기록·수수료/체결·외표본 결과다. 08 구현 및 격리 검사와 실제 운영 검증을 구분한다. 유료 모델 평가·실주문·사용자 DB 접근은 0건이다. **다음 08A/09/10 및 클라우드 작업은 별도 승인 대기**다.

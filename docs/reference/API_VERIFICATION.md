# 외부 API 확인 기록

확인일: 2026-11-13 UTC. 이 문서는 공개 공식 문서를 읽고 공개 RSS를 조회한 결과다. 사용자 계좌, 유료 LLM API, 실제 주문은 호출하지 않았다. 따라서 문서상 지원과 사용자 계좌에서의 실제 이용 가능 여부를 구분한다.

## 토스증권 소수점 주문

확인한 공식 REST 명세는 `1.2.28`이다.

- [REST OpenAPI 원문](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)
- [공식 FAQ](https://openapi.tossinvest.com/openapi-docs/faq.md)
- [개발자 문서](https://developers.tossinvest.com/docs)

| 항목 | 공식 문서에서 확인한 내용 |
|---|---|
| 금액 매수 | `POST /api/v1/orders`, 미국 주식 `MARKET`, `orderAmount`에 달러 금액 문자열 사용 |
| 소수점 수량 매도 | 미국 주식 `MARKET` + `SELL`, `quantity` 문자열, 소수점 6자리까지 |
| 수량·금액 필드 | `quantity`, `orderAmount` 중 정확히 하나 사용 |
| 지정가 주문 | 소수점 수량을 지원하지 않음. Paper의 보호 가격을 실제 소수점 지정가 주문으로 해석하지 않음 |
| 접수 시간 | 금액 주문과 소수점 수량 주문 모두 정규장 시작부터 정규장 종료 1시간 전까지 |
| 멱등성 키 | `clientOrderId`, 최대 36자. **서버의 중복 방지 유효기간은 10분**이므로 앱의 영구 주문 장부가 별도로 필요 |
| 매수 가능 금액 | `GET /api/v1/buying-power?currency=USD`, `cashBuyingPower` |
| 매도 가능 수량 | `GET /api/v1/sellable-quantity?symbol=...`, `sellableQuantity` |
| 장 운영 시간 | `GET /api/v1/market-calendar/US?date=YYYY-MM-DD`, 미국 현지 날짜로 조회하며 응답 시각은 시간대가 포함된 KST |
| 현재 계좌 수수료 | `GET /api/v1/commissions` |

계좌별 최소 주문 금액, 종목별 소수점 지원, 실제 부분 체결·취소·잔량 처리는 Phase 5에서 계좌 응답과 제한된 주문 검증으로 확인해야 한다. 공개 명세에 최소 금액이 없다는 사실을 최소 금액이 없다는 뜻으로 해석하지 않는다.

`GET /api/v1/stocks`는 미국 종목의 상장 시장, 통화, 보통주 여부와 상장 상태 등을 제공한다. `status=ACTIVE`는 상장 상태이지 해당 계좌의 주문 허용 확인이 아니다. 미국 종목별 `fractionalSupported`, 거래정지, 기업행동 검사 완료 필드는 찾지 못했다. `krxTradingSuspended`, `nxtTradingSuspended`는 국내 시장 전용이다. `warnings`의 빈 목록도 미국 거래정지·기업행동 부재를 증명하지 않는다.

따라서 토스 시세만 수집하는 실행에서 부족한 거래 자격을 임의로 채우지 않는다. 검증된 자격 자료가 없는 종목은 관찰 대상으로 남긴다. 완전한 자격·시세를 담은 기록 입력과 명시적으로 표시된 합성 자료로 Paper 엔진을 검증할 수 있다.

미국 시세는 일부 거래소 기반이며 NBBO와 동일하지 않다. 1분봉에서 거래가 없는 분이 생략될 수 있고 뒤늦은 체결 보고로 과거 봉이 수정될 수 있다. 이 때문에 누락된 분봉을 임의 생성하거나 현재 받은 정정 봉을 과거에 이미 알고 있던 값으로 사용하지 않는다.

## OpenAI 모델·요금

공식 모델 페이지에서 두 모델의 이름, 구조화된 출력 지원과 아래 표준 단가를 확인했다. 이는 Codex에서 보이는 모델 목록으로 추정한 결과가 아니다.

| 모델 | 입력 100만 토큰 | 출력 100만 토큰 | 공식 페이지 |
|---|---:|---:|---|
| `gpt-6-luna` | $0.10 | $0.50 | [Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) |
| `gpt-6.1-terra` | $1.00 | $5.00 | [Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra) |

표는 짧은 컨텍스트·표준 처리의 텍스트 단가이며, 캐시·빠른 처리·지역 처리·긴 컨텍스트 요금과 별도 도구 비용은 다를 수 있다. 두 모델은 추론 토큰을 사용하므로 화면에 표시된 답변 길이만으로 실제 출력 비용을 계산하면 안 된다. [최신 가격표](https://platform.openai.com/pricing)를 함께 확인한다.

이 개발에서 실제 계정의 모델 접근 권한이나 구조화된 응답을 유료 요청으로 검증하지 않았다. 설정의 모델명을 바꾸는 것만으로 접근 권한이 생기지는 않는다. 실제 호출의 `usage`와 실패·누락 상태를 기록하며, 비용을 알 수 없는 실패 요청은 무비용이라고 단정하지 않는다. 한국어·영어 프롬프트의 실제 정확도 비교도 별도 평가 자료와 API 실행이 필요하다.

## 공식 경제기관 RSS

아래 URL은 공개 요청에서 HTTP 200과 XML 구조를 확인했다. 확인 시점의 연결 성공은 이후 지연이나 중단이 없다는 보장이 아니다.

| 기관 | 피드 | 형식 |
|---|---|---|
| Federal Reserve | [전체 발표](https://www.federalreserve.gov/feeds/press_all.xml), [통화 정책](https://www.federalreserve.gov/feeds/press_monetary.xml) | RSS |
| BLS | [CPI](https://www.bls.gov/feed/cpi.rss), [고용](https://www.bls.gov/feed/empsit.rss), [PPI](https://www.bls.gov/feed/ppi.rss) | Atom |
| BLS | [최신 소식](https://www.bls.gov/feed/bls_latest.rss) | RSS |
| BEA | [공식 발표](https://apps.bea.gov/rss/rss.xml) | RSS |
| EIA | [Today in Energy](https://www.eia.gov/rss/todayinenergy.xml), [보도자료](https://www.eia.gov/rss/press_rss.xml) | RSS |

공식 목록: [Fed](https://www.federalreserve.gov/feeds/feeds.htm), [BLS](https://www.bls.gov/feed/), [BEA](https://www.bea.gov/resources/for-developers), [EIA](https://www.eia.gov/tools/rssfeeds/).

재무부의 [공식 보도자료 페이지](https://home.treasury.gov/news/press-releases)는 확인했지만 현재 동작하는 전체 보도자료 RSS를 확인하지 못했다. 예전에 쓰였거나 추측한 RSS 경로들은 404였으므로 정상 피드로 등록하지 않는다. 재무부 자료는 공식 HTML 발표 수집 또는 별도로 확인한 피드로 처리한다. [TreasuryDirect RSS](https://www.treasurydirect.gov/rss/)에는 국채 경매 공고·결과 등이 있지만 재무부 전체 정책 발표와 범위가 다르다.

재무부는 과거 자료 이전으로 오래된 기사가 RSS에 재등장할 수 있다는 [공식 안내](https://home.treasury.gov/news/press-releases/we-will-be-doing-some-content-migration-of-older-press-releases-starting-thursday-night-june-17th-2021-which-will-cause-old-press-releases-to-appear-on-this-rss-feed-please-disregard-these-releases-read-more)를 낸 적이 있다. 수집 시각을 사건 발생·공개 시각으로 대체하지 않는다.

## GDELT·FRED

[GDELT DOC API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)는 기사 검색을 제공하며 키가 필요하지 않다. 최소 검색 구간은 15분이고 기사 목록의 `maxrecords` 상한은 250이다. 5~10분 수집 주기를 설정해도 기사 공개 직후 수신을 보장하지 않는다. 기사 수나 검색 결과만으로 사건의 사실 여부·독립 취재 수를 확정하지 않는다. `seendate`를 원문 기사 공개 시각으로 단정하지 않고, 공개 시각을 모르면 별도로 미확인 상태를 보존한다.

검증 중 DOC API의 공개 요청 한 번은 HTTP 503을 반환했다. 따라서 이 환경에서 실제 기사 수집 연결이 성공했다고 보고하지 않는다. 서비스 오류·429·JSON이 아닌 응답을 처리하고 다음 수집까지 대기하도록 구현한다. 공급자가 바꾸는 제한에 대응하며 여러 주제 요청을 동시에 몰아 보내지 않는다.

[FRED API v1 키 안내](https://fred.stlouisfed.org/docs/api/api_key.html)에서 발급받은 본인 키를 사용한다. [DGS2](https://fred.stlouisfed.org/series/DGS2)·[DGS10](https://fred.stlouisfed.org/series/DGS10)은 일별 국채 수익률(%)이고, [DCOILWTICO](https://fred.stlouisfed.org/series/DCOILWTICO)는 일별 WTI 현물 가격(달러/배럴)이다. 지연·수정될 수 있는 참고 자료이며 실시간 체결 신호가 아니다. TLT 가격을 국채 수익률로, USO 가격을 WTI 현물 가격으로 기록하지 않는다.

FRED의 [실시간 기간](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)과 [빈티지 날짜](https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html)는 수정 전 자료를 재현하는 데 필요하다. 관측 대상 날짜만 보고 당시 공개됐다고 가정하지 않는다. API 키가 없어 계정 인증이 필요한 FRED 조회는 실행하지 않았다.

## S&P 500 구성 이력

[S&P 공식 지수 페이지](https://www.spglobal.com/spdji/en/indices/equity/sp-500/)와 [방법론](https://www.spglobal.com/spdji/en/methodology/article/sp-us-indices-methodology/)을 기준으로 한다. 500개 기업과 티커 수는 복수 종류 주식 때문에 다를 수 있다. 목록을 반드시 500개 티커로 강제하지 않는다.

공개 현재 구성 목록은 현재 시점의 후보 목록을 만드는 데 쓸 수 있지만, 그 목록을 과거 전체에 적용하면 생존자 편향이 생긴다. 편입·제외 발표 시각과 실제 효력 시각을 분리해 보존한다. 현재 목록만 확보한 경우 최초 확인 이전의 구성은 `UNKNOWN`으로 두며, 과거 백테스트는 출처와 해당 시점 정보가 있는 별도 이력을 요구한다. 공개 현재 목록 수집이 공식 과거 구성 이력 전체의 확보를 의미하지 않는다.

## Phase 5 이전에 남은 확인

- 실제 계좌의 주문 권한, 종목별 소수점 지원, 최소·최대 금액과 정밀도.
- 계좌의 거래 가능·거래정지·기업행동 자료를 신뢰할 수 있는 방법으로 확보.
- 실제 소수점 체결·취소·잔량과 내부 Decimal 장부 대사.
- 타임아웃 주문 상태 재조회, 재시작 복구, 조기 폐장 청산 실패 처리.
- 해당 계정의 OpenAI 모델 접근과 구조화된 출력 연결 확인.

Phase 0~4는 Paper 개발·검증 범위다. 위 확인 없이 실제 주문을 활성화하지 않는다.

## Phase 5 구현 시 재확인

공식 OpenAPI 원문을 다시 내려받아 버전 `1.2.28`, 금액 매수·소수점 수량 매도·주문 상세의 `orderAmount`와 `OrderExecution` 누적 체결·수수료·세금·실제 체결 시각 필드를 재확인했다. 주문 API 권한은 계좌 읽기 성공만으로 검증됐다고 기록하지 않는다. 개발 서버에서는 공식 명세 GET과 모의 HTTP만 사용하며 실제 계좌 주문은 호출하지 않았다.

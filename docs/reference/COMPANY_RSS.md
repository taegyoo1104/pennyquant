# 기업 RSS 기본 목록과 조사 기록

어떤 회사의 공식 RSS를 기본 등록했는지와 제외 근거를 확인할 때 읽습니다. 목록에 있다고 매 주기 모든 피드를 요청하거나 모든 기사를 분석하는 것은 아닙니다. 수집 대상·본문·시각·중복·예산 검사를 통과한 기사에 LLM을 사용합니다. 추천 단순 전략에서 뉴스는 후보 가점이며, 별도의 전략 설명도 주문을 직접 실행하지 않습니다.

[문서 목록](../README.md) · [매매 흐름](../TRADING_PROCESS.md) · [용어 설명](../GLOSSARY.md)

확인일: 2026-11-14 KST · S&P 500 내 시가총액 **상위 100개 발행사** 대상 · **63개 발행사 / 64개 티커 등록**.

이 목록은 RSS 기본 등록 당시의 시총 100개 발행사 기준이다. 운영 감시·집중 정책은 이 목록과 별개다. 현재 추천 프로필은 확인 가능한 시총 기준 최대 50개에서 거래대금 중심 집중 10개를 선정한다. [단계 01](https://github.com/taegyoo1104/pennyquant/blob/03fc270546c72c15c7ffed9af9b60a7207f3ae14/docs/archive/stages/STAGE01_WATCHLIST.md)은 이전 구현 기록이다. 전체 편입 원장은 유지하고 운영 선정에는 검증된 시총 입력과 대표 티커 근거를 사용한다. 이 RSS 스냅숏을 운영 감시목록으로 자동 전환하지 않는다.

## 시가총액 선정 기준

[Stock Analysis의 S&P 500 목록](https://stockanalysis.com/list/sp-500-stocks/)에서 시가총액 열을 USD Decimal 값으로 변환해 내림차순 정렬했다. 페이지의 지수 비중 순서를 그대로 쓰지 않는다. 화면에 표시된 반올림 시총 수치 기준이며 정확한 실시간 시총 순위를 보증하지 않는다. 구성 여부는 [기존 유니버스와 같은 공개 CSV](https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv)와 대조했다. 지수 제공사의 공식 과거 이력 상품은 아니다.

같은 CIK의 주식 클래스는 한 발행사로 계산한다. 클래스별로 제공된 기업 시총은 합산하지 않고 최댓값을 대표값으로 사용한다. 선정 결과는 100개 발행사·101개 티커이며 Alphabet의 GOOG·GOOGL을 함께 포함한다. 순위는 확인 시점의 고정 스냅숏이다. 이 RSS 목록 자체는 운영 중 시총 순위나 거래 유니버스를 자동 변경하지 않는다. 실행기의 시총 갱신·집중 선정 기능과는 별개다.

순위·시총·복수 클래스·확인 시각은 `sessionquant/config/rss_top100.json`에 기록했다. 등록 피드·HTTP·파서·출처 증거는 `sessionquant/config/company_rss.json`, 전체 100개 발행사의 조사·제외 기록은 [COMPANY_RSS_AUDIT.json](COMPANY_RSS_AUDIT.json)에 보존했다.

## 등록 및 제외 기준

공식 회사 도메인의 IR·뉴스룸 RSS/Atom 응답과 현재 수집 파서에서 실제 뉴스 이벤트를 확인한 피드만 포함했다. 댓글, 다른 회사에 대한 일반 시장 해설, 기업 연결이 불명확하거나 오랫동안 갱신되지 않은 피드는 제외했다. 공식 피드가 있어도 접속 오류나 발행 시각을 확인할 수 없으면 이번 기본 목록에서 제외한다. 예를 들어 Apple 뉴스룸 Atom은 updated만 제공하여 실제 공개 시각으로 대체하지 않았다. 제외는 RSS가 없다는 확정 판정이 아니다.

`rss_verified: true`는 발행사와 공식 피드의 연결을 검토했다는 뜻이다. 뉴스 사건의 확정·호재·악재나 주문 허용을 보장하지 않는다. 회사 소식에는 제품·사업·사회공헌 기사도 포함되며, 현재 사건 여부·원문 근거·최신성·기존 LLM 예산·위험 규칙을 계속 적용한다.

## 적용 및 점검

업데이트하면 기본 목록을 읽는다. 실제로 선택한 YAML의 사용자 기업별 필드는 같은 기업 기본값보다 우선한다. 직접 등록한 NVDA 주소 등은 유지된다. 전체 기본 목록을 해제하려면 `company_rss_catalog: false`, 특정 기업의 RSS만 해제하려면 그 기업에 `rss_urls: []`를 지정한다. 등록되지 않은 종목의 차트·SEC·Alpaca 처리 경로는 기존 방식으로 동작한다.

```powershell
.\venv\Scripts\python.exe -m sessionquant check-news --config config/sessionquant.strategy-lab-websocket.example.yaml --connect --provider rss
```

등록된 전체 피드를 최대 4개 동시 요청으로 확인한다. 같은 URL을 사용하는 복수 주식 클래스는 한 번만 조회하고 각 티커에 결과를 표시한다. 공급자별 요청 간격을 유지하며 LLM 생성·주문을 하지 않는다. 운영 뉴스 수집은 기존 보유·후보 종목 우선순위와 호출 예산을 유지한다.

## 등록 목록

| 시총 순위 | 티커 | 기업 | 공식 RSS | 마지막 발행일(검증 당시) |
|---|---|---|---|---|
| 2 | NVDA | Nvidia | [RSS](https://nvidianews.nvidia.com/cats/press_release.xml) | 2026-11-12 |
| 4 | GOOGL | Alphabet Inc. (Class A) | [RSS](https://blog.google/rss/) | 2026-11-13 |
| 4 | GOOG | Alphabet Inc. (Class C) | [RSS](https://blog.google/rss/) | 2026-11-13 |
| 5 | AMZN | Amazon | [RSS](https://ir.aboutamazon.com/rss/pressrelease.aspx) | 2026-10-29 |
| 6 | AVGO | Broadcom | [RSS](https://investors.broadcom.com/rss/news-releases.xml) | 2026-11-09 |
| 7 | META | Meta Platforms | [RSS](https://www.meta.com/blog/rss/) | 2026-11-13 |
| 9 | LLY | Lilly (Eli) | [RSS](https://investor.lilly.com/rss/news-releases.xml) | 2026-11-12 |
| 13 | MU | Micron Technology | [RSS](https://investors.micron.com/rss/pressrelease.aspx) | 2026-11-12 |
| 14 | AMD | Advanced Micro Devices | [RSS](https://ir.amd.com/news-events/press-releases/rss) | 2026-11-10 |
| 15 | V | Visa Inc. | [RSS](https://investor.visa.com/rss/pressrelease.aspx) | 2026-11-05 |
| 17 | JNJ | Johnson & Johnson | [RSS](https://www.jnj.com/media-center/press-releases.rss) | 2026-11-10 |
| 18 | MA | Mastercard | [RSS](https://investor.mastercard.com/rss/pressrelease.aspx) | 2026-10-29 |
| 19 | ABBV | AbbVie | [RSS](https://news.abbvie.com/?pagetemplate=rss) | 2026-11-08 |
| 20 | COST | Costco | [RSS](https://investor.costco.com/rss/pressrelease.aspx) | 2026-11-10 |
| 22 | KO | Coca-Cola Company (The) | [RSS](https://investors.coca-colacompany.com/news-events/press-releases/rss) | 2026-11-11 |
| 25 | ORCL | Oracle Corporation | [RSS](https://www.oracle.com/corporate/press/rss/rss-pr.xml) | 2026-11-13 |
| 26 | CSCO | Cisco | [RSS](https://newsroom.cisco.com/c/services/i/servlets/newsroom/rssfeed.json?feed=press-releases) | 2026-11-12 |
| 31 | GE | GE Aerospace | [RSS](https://www.geaerospace.com/news/rss.xml) | 2025-04-29 |
| 32 | INTC | Intel | [RSS](https://www.intc.com/news-events/press-releases/rss) | 2026-11-12 |
| 35 | AMAT | Applied Materials | [RSS](https://ir.appliedmaterials.com/rss/news-releases.xml) | 2026-11-12 |
| 36 | LRCX | Lam Research | [RSS](https://newsroom.lamresearch.com/press-releases?pagetemplate=rss) | 2026-11-12 |
| 37 | TXN | Texas Instruments | [RSS](https://investor.ti.com/rss/news-releases.xml) | 2026-10-27 |
| 40 | RTX | RTX Corporation | [RSS](https://www.rtx.com/rss-feeds/news) | 2026-11-09 |
| 41 | PANW | Palo Alto Networks | [RSS](https://investors.paloaltonetworks.com/rss/news-releases.xml) | 2026-11-03 |
| 42 | TMO | Thermo Fisher Scientific | [RSS](https://ir.thermofisher.com/rss/pressrelease.aspx) | 2026-11-13 |
| 43 | WFC | Wells Fargo | [RSS](https://newsroom.wf.com/rss/pressrelease.aspx) | 2026-11-13 |
| 44 | NFLX | Netflix | [RSS](https://ir.netflix.net/rss/pressrelease.aspx) | 2026-09-14 |
| 46 | ANET | Arista Networks | [RSS](https://investors.arista.com/rss/pressrelease.aspx) | 2026-11-03 |
| 48 | IBM | IBM | [RSS](https://kr.newsroom.ibm.com/announcements?pagetemplate=rss) | 2026-11-04 |
| 49 | CRWD | CrowdStrike | [RSS](https://ir.crowdstrike.com/rss/news-releases.xml) | 2026-11-12 |
| 50 | DELL | Dell Technologies | [RSS](https://investors.delltechnologies.com/news-events/press-releases/rss) | 2026-11-11 |
| 51 | AXP | American Express | [RSS](https://ir.americanexpress.com/rss/pressrelease.aspx) | 2026-10-27 |
| 52 | GEV | GE Vernova | [RSS](https://www.gevernova.com/news/subscribe/all/rss.xml) | 2026-11-10 |
| 53 | GILD | Gilead Sciences | [RSS](https://investors.gilead.com/rss/pressrelease.aspx) | 2026-11-12 |
| 54 | DIS | Walt Disney Company (The) | [RSS](https://thewaltdisneycompany.com/feed/) | 2026-11-13 |
| 56 | KLAC | KLA Corporation | [RSS](https://ir.kla.com/news-events/press-releases/rss) | 2026-11-05 |
| 58 | BLK | BlackRock | [RSS](https://ir.blackrock.com/rss/pressrelease.aspx) | 2026-10-14 |
| 59 | APH | Amphenol | [RSS](https://investors.amphenol.com/rss/pressrelease.aspx) | 2026-10-28 |
| 60 | ADI | Analog Devices | [RSS](https://investor.analog.com/rss/news-releases.xml) | 2026-11-09 |
| 63 | ABT | Abbott Laboratories | [RSS](https://abbottinvestor.gcs-web.com/rss/news-releases.xml) | 2026-11-02 |
| 64 | QCOM | Qualcomm | [RSS](https://investor.qualcomm.com/rss/pressrelease.aspx) | 2026-11-04 |
| 69 | CRM | Salesforce | [RSS](https://investor.salesforce.com/rss/pressrelease.aspx) | 2026-11-10 |
| 70 | NEE | NextEra Energy | [RSS](https://newsroom.nexteraenergy.com/news-releases?pagetemplate=rss) | 2026-11-11 |
| 72 | PFE | Pfizer | [RSS](https://investors.pfizer.com/rss/pressrelease.aspx) | 2026-11-11 |
| 73 | DHR | Danaher Corporation | [RSS](https://investors.danaher.com/press-releases?pagetemplate=rss) | 2026-11-02 |
| 74 | SCHW | Charles Schwab Corporation | [RSS](https://pressroom.aboutschwab.com/rss/pressrelease.aspx) | 2026-11-13 |
| 75 | ISRG | Intuitive Surgical | [RSS](https://isrg.gcs-web.com/rss/news-releases.xml) | 2026-11-02 |
| 80 | CB | Chubb Limited | [RSS](https://news.chubb.com/news-releases?pagetemplate=rss) | 2026-10-20 |
| 81 | UBER | Uber | [RSS](https://investor.uber.com/rss/pressrelease.aspx) | 2026-11-12 |
| 82 | LMT | Lockheed Martin | [RSS](https://news.lockheedmartin.com/news-releases?pagetemplate=rss) | 2026-11-10 |
| 84 | SPGI | S&P Global | [RSS](https://investor.spglobal.com/rss/pressrelease.aspx) | 2026-10-27 |
| 86 | PGR | Progressive Corporation | [RSS](https://progressive.mediaroom.com/news-releases/?pagetemplate=rss) | 2026-11-10 |
| 87 | VRTX | Vertex Pharmaceuticals | [RSS](https://investors.vrtx.com/rss/news-releases.xml) | 2026-11-04 |
| 88 | BA | Boeing | [RSS](https://boeing.mediaroom.com/news-releases-statements?pagetemplate=rss) | 2026-11-04 |
| 89 | APP | AppLovin | [RSS](https://investors.applovin.com/rss/pressrelease.aspx) | 2026-11-04 |
| 90 | MO | Altria | [RSS](https://investor.altria.com/rss/pressrelease.aspx) | 2026-10-29 |
| 91 | BMY | Bristol Myers Squibb | [RSS](https://news.bms.com/rss/pressrelease.aspx) | 2026-11-12 |
| 93 | MCK | McKesson | [RSS](https://investor.mckesson.com/rss/pressrelease.aspx) | 2026-11-04 |
| 94 | SNDK | Sandisk | [RSS](https://investor.sandisk.com/rss/news-releases.xml) | 2026-10-29 |
| 95 | MDT | Medtronic | [RSS](https://news.medtronic.com/press-releases?pagetemplate=rss) | 2026-11-04 |
| 96 | PLD | Prologis | [RSS](https://ir.prologis.com/news-events/press-releases/rss) | 2026-10-28 |
| 97 | ADP | Automatic Data Processing | [RSS](https://mediacenter.adp.com/press-releases?pagetemplate=rss) | 2026-11-11 |
| 98 | SBUX | Starbucks | [RSS](https://about.starbucks.com/feed/) | 2026-11-09 |
| 99 | STX | Seagate Technology | [RSS](https://investors.seagate.com/rss/pressrelease.aspx) | 2026-11-10 |


## 2026-11-14 당시 검증 결과

전체 자동 테스트 631개가 통과했고, 선택 MySQL 검사 8개는 이번 실행에서 제외했다. Ruff·변경 형식 검사와 wheel 빌드를 통과했으며, 설치 패키지에 두 JSON 목록이 포함되는 것을 확인했다. 읽기 전용 실제 접속 검사에서는 등록된 64개 티커의 63개 고유 피드가 모두 조회됐다. LLM 텍스트 생성이나 실제 주문은 실행하지 않았다. 공급자의 이후 변경·장애가 있을 수 있으므로 사용자 PC에서도 같은 점검 명령으로 상태를 확인한다.

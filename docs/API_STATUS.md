# 외부 API 연동 상태

문서 확인일: 2026-11-13. 공식 문서의 경로·스키마와 HTTP 모의 응답을 확인했습니다. 계정 키가 없어 실제 인증·시세 권한·뉴스 구독·주문·OCO는 실계정에서 검증하지 않았습니다.

| 제공자 | 구현한 기능 | 현재 제한 |
|---|---|---|
| 토스증권 | OAuth 토큰, 현재가·호가·1분봉·종목·유의사항·시장 랭킹·계좌·보유·주문 이력·미국 캘린더 조회 | 실제 계정 연결 미검증. 실주문 함수는 예외로 차단 |
| 토스증권 WebSocket | 체결·호가·개인 주문 선언, ack 확인, PING, 오류 전달 | 재접속 전 호출자가 REST 주문·보유를 동기화해야 함 |
| Alpaca | REST 뉴스 페이지 수집, 중복 제거, 실시간 WS 인증·구독 확인 | REST 결과는 항상 보조 데이터. WS는 별도 실시간 권한 확인 필요 |
| SEC | CIK별 최근 공시 메타데이터, 양식 필터, acceptanceDateTime·수신 시각 분리 | 본문 의미 분석·과거 추가 파일 순회는 미구현. 양식만으로 호악재 판정하지 않음 |
| RSS/Atom | 제목·본문·URL·발표·수신 시각, 회사별 명시적 티커 매핑 | 광역 피드에서 티커를 추측하지 않음. 시각 없는 기사는 제외 |

## 토스증권

공식 [문서 인덱스](https://developers.tossinvest.com/llms.txt), [REST OpenAPI](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json), [WebSocket AsyncAPI](https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json), [개요](https://openapi.tossinvest.com/openapi-docs/overview.md)를 확인했습니다. 확인한 버전은 REST `1.2.28`, 실시간 `1.2.3`입니다. 배포 전 최신 문서와 차이를 확인하세요.

REST 주소는 `https://openapi.tossinvest.com`입니다. `POST /oauth2/token`에 form-urlencoded Client Credentials를 보내며, 일반 호출은 Bearer 토큰, 계좌 관련 호출은 `X-Tossinvest-Account: accountSeq`를 추가합니다. 환경변수는 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT_SEQ`입니다. `.env`를 자동으로 읽지 않으므로 셸/비밀 저장소에서 주입하세요. 키를 코드나 로그에 넣지 마세요. 토큰 발급 재시도는 없으며, 메모리에 캐시합니다. 클라이언트당 유효 토큰은 하나라 여러 프로세스의 재발급은 서로 기존 연결을 끊을 수 있습니다.

구현한 GET 경로는 `/api/v1/prices`, `/api/v1/orderbook`, `/api/v1/candles`, `/api/v1/stocks`, `/api/v1/stocks/{symbol}/warnings`, `/api/v1/rankings`, `/api/v1/accounts`, `/api/v1/holdings`, `/api/v1/orders`, `/api/v1/market-calendar/US`입니다. `candles()`는 USD 1분봉만 정규화하며 timestamp는 공식 정의대로 봉 종료 시각입니다. 아직 끝나지 않은 봉은 제외합니다. 원자료를 읽은 시각은 `received_at`에 남습니다. 과거 봉을 처음 내려받은 시각을 과거 실시간 수신 시각으로 바꾸지 마세요.

`quote()`와 `get_quote()`는 호가만으로 주문 가능 여부를 확정하지 않습니다. 기본 `tradable=False`, `exchange="UNKNOWN"`이며, 현재 거래정지·주문 가능 여부를 확인한 별도 입력이 있어야 거래용으로 설정할 수 있습니다. 종목 마스터 `status=ACTIVE`는 상장 상태이고 실시간 주문 가능성의 증거가 아닙니다. 미국 종목에는 실시간 정지·주문 가능 여부를 확정할 필드가 없고, 유의사항 목록이 비어 있어도 정지가 없다는 증거로 삼지 않습니다. 1일 시장 랭킹의 `price.basePrice`는 참조 기준가이며 공식 전일 종가로 단정하지 않습니다. `tradingVolume`·`tradingAmount`는 참고할 수 있지만 동일 시간 과거 거래량은 제공되지 않습니다. RVOL은 별도 비교 가능한 누적 이력이 없으면 `None`으로 유지하고 진입을 차단하세요.

공식 [FAQ](https://openapi.tossinvest.com/openapi-docs/faq.md)는 미국 현재가·체결·호가·봉·WebSocket이 일부 거래소 기반이고 NBBO가 아님을 명시합니다. 호가 timestamp는 마지막 호가 갱신 시각이므로 값이 변하지 않으면 오래될 수 있습니다. 미국 1분봉은 거래 없는 분을 생략하고 일부 거래소 거래량을 사용하지만 일봉 거래량은 통합 집계이므로 서로 합산·비교할 때 기준을 맞춰야 합니다. 지연 체결 보고로 과거 1분봉이 바뀔 수 있고 정정·취소가 완전히 반영되지는 않습니다. 기업행동 유형·분할 비율을 제공하지 않으며 `adjusted=false`만으로 과거 데이터 전체가 미조정임을 증명할 수 없습니다. 현재 토스 수집만으로 완전하게 검증된 미국 종목 스캐너 입력이 채워진다고 보지 않습니다.

실시간 주소는 `wss://openapi-ws.tossinvest.com/ws/v1`이며 헤더 인증 후 JSON 배열을 보냅니다. `[{"type":"trade:us","codes":["AAPL"]},{"type":"orderbook:us","codes":["AAPL"]}]`처럼 전체 구독을 한 번에 교체합니다. 계정당 연결 2개, 연결당 채널×종목 합계 100개를 관리해야 합니다. 어댑터 기본 후보 상한은 20개이며 두 시세 채널이면 40개 구독입니다. 60초마다 텍스트 `PING`을 보내고 부분 거절 ack·인증 오류·종료는 예외로 전달합니다. 체결 스트림은 유실 가능하므로 체결 메시지를 더해 당일 누적 거래량을 만들지 않습니다. 재접속 후 주문·조건주문 이벤트는 과거 누락분을 재전송하지 않으므로 REST 재확인이 필요합니다.

공식 API에는 실주문과 OCO가 있습니다. 이 MVP는 계정별 세션·수량·미체결·조건주문 동작을 실제로 검증하지 않았으므로 실주문 요청을 보내지 않습니다. 별도 구현에서 동일 `clientOrderId` 확인·불명 주문 보류·부분체결 수량 보호·재시작 복구를 실제 계정으로 검증한 뒤 계획서의 수동 승인 절차를 거쳐야 합니다.

## Alpaca 뉴스

[REST 뉴스](https://docs.alpaca.markets/us/reference/news-3)와 [실시간 뉴스](https://docs.alpaca.markets/us/docs/streaming-real-time-news)의 공식 경로와 필드를 사용합니다. `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`로 인증합니다. 문서는 권한에 따라 REST 최신 구간이 지연될 수 있음을 명시합니다. 이 구현은 REST를 모두 `real_time=False`로 저장합니다.

WebSocket은 `realtime_verified=True`를 명시하고 계정의 실시간 뉴스 권한을 별도로 확인한 경우에만 시작합니다. 서버의 authenticated와 요청 심볼 subscription ack까지 확인한 WS 기사만 `real_time=True`입니다. 그래도 기사 생성 시각 대비 수신 지연은 전략 필터에서 따로 검사합니다. 업데이트는 원래 기사 ID를 유지하며 중복 버전 수신을 제한합니다. API 접근 성공이 수익성이나 저지연 체결을 뜻하지는 않습니다.

Alpaca 기사는 요청 종목 외에도 여러 종목·자산 태그를 포함할 수 있습니다. 내부 주식 티커 형식에 맞는 문자열만 원문 그대로 유지하고 중복을 제거합니다. 지원하지 않는 태그와 null 등 개별 무효 값은 개수 경고를 남기고 제외하며 다른 기사 수집을 중단시키지 않습니다. 대소문자·공백·구두점을 바꾸거나 객체를 문자열로 바꾸어 별칭을 추측하지 않습니다. 유효한 태그가 없으면 종목 목록을 비워 저장하므로 어떤 종목의 진입 근거로도 사용할 수 없습니다. 요청한 `--symbols`를 기사에 임의로 붙이지 않으며, 다른 태그가 제외되어도 정확히 일치하는 유효 종목과 기존 출처 검증 상태는 유지합니다. 이 처리는 REST와 WebSocket에 공통 적용됩니다.

### Alpaca 수집 오류 확인

구버전의 `alpaca response could not be normalized (ValueError)`만으로는 원인을 확정할 수 없습니다. 키에 한글 등이 포함되어 HTTP 요청 전에 실패한 경우와 기사 필드 변환 실패가 같은 문구로 표시될 수 있습니다. 최신 코드는 키·시크릿의 입력 형식을 요청 전에 검사하고, 응답 오류는 고정된 필드명으로 구분합니다.

| 최신 오류에 표시되는 항목 | 확인할 내용 |
|---|---|
| `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`의 `invalid characters` | 안내 문구 대신 발급받은 값만 입력했는지 확인합니다. 공백·줄바꿈·한글 등 비ASCII 문자는 허용하지 않습니다. |
| `field: response` / `field: news` | 유효한 JSON 또는 뉴스 배열이 없는 응답입니다. 응답 원문 대신 정제된 오류 문구만 공유합니다. |
| `field: created_at` | 기사 발표 시각이 없거나 시간대를 포함한 ISO 형식이 아닙니다. 현재 시각으로 대체하지 않습니다. |
| `field: symbols` | 종목 필드가 배열이 아닌 응답입니다. 배열 안의 지원하지 않는 개별 태그는 경고 후 제외하고 수집을 계속합니다. |

REST 수집은 모든 페이지를 변환한 뒤 파일에 기록하므로, `0`건 저장 후 오류가 났다고 해서 API가 뉴스를 0건 반환했다는 뜻은 아닙니다. 한 기사나 후속 페이지의 오류로 저장 전 중단될 수 있습니다. 실패한 실행은 `collection did not complete`로 표시합니다. 발표 시각·배열 구조 등 필수 데이터의 오류를 임의의 값으로 대체하지 않습니다.

PyCharm에서 **Ctrl+T**로 갱신한 뒤 프로젝트 루트의 PowerShell에서 새로운 파일명으로 다시 실행합니다. `venv` 대신 `.venv`를 사용한다면 경로를 바꾸세요.

```powershell
$newsFile = "data/alpaca-news-$(Get-Date -Format yyyyMMdd-HHmmss).jsonl"
.\venv\Scripts\python.exe -m pennyquant collect alpaca --symbols AAPL,MSFT --output $newsFile
```

오류가 계속되면 `PennyQuant:`로 시작하는 최신 오류 문구만 확인합니다. API 키·시크릿·환경변수 값·인증 헤더·원본 응답은 채팅이나 GitHub에 올리지 마세요. `real_time=false` 안내는 REST 보조 뉴스의 정상 표시이며 변환 오류의 원인이 아닙니다.

## SEC·RSS

[SEC 데이터 API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)와 [개발자 Fair Access 지침](https://www.sec.gov/about/developer-resources)을 기준으로 `SEC_USER_AGENT="PennyQuant 본인연락처"`를 요구하고, 프로세스 내 모든 SEC 클라이언트와 재시도를 하나의 초당 5회 제한기로 묶습니다. SEC는 사용자 전체 합산 한도를 두므로 여러 프로세스·장비를 동시에 운용하면 별도 중앙 제한기가 필요합니다. acceptanceDateTime에 정확한 시간대가 없으면 `published_at=None`으로 보존하며 거래용 뉴스로 변환하지 않습니다. 공시 목록은 증자·계약의 내용을 판정하지 않습니다.

[GlobeNewswire 공식 RSS 목록](https://www.globenewswire.com/rss/list)에서 회사/주제 피드를 선택할 수 있습니다. RSS는 지연·신원·내용을 확인하기 전 보조 데이터이며, 기본적으로 `verified=False`, `real_time=False`입니다. 회사별로 검증한 피드에만 명시적 티커를 매핑하세요. RSS 문서 타입/엔티티 선언과 4MB 초과 피드는 거절합니다.

## 장애 처리

GET만 최대 3번 재시도하고 429·일부 5xx는 Retry-After를 따릅니다. 60초보다 긴 서버 지연 요청은 대기를 줄여 재시도하지 않고 오류로 반환합니다. 인증/권한 오류를 자동 키 재발급이나 다른 주소로 우회하지 않습니다. 수집기 예외가 나면 상위 운용 루프가 신규 진입을 중단하고 보유 포지션 상태를 확인해야 합니다. 실제 계정 권한·고정 IP 등록·무료 데이터 조건은 사용자 계정에서 별도로 확인할 항목입니다.

## 수집 CLI

`python -m pennyquant check-toss --symbol AAPL`은 인증과 한 종목의 현재가 조회만 확인합니다. Windows·PyCharm 키 설정과 단계별 실패 안내는 [토스 연결 확인](TOSS_CONNECTION.md)을 참고하세요. 이 검사는 계좌·주문을 호출하지 않으며 성공해도 실제 시세 기반 모의매매 전체의 준비가 끝났다는 뜻이 아닙니다.

프로젝트를 설치하고 환경변수를 셸에 주입한 다음 실행합니다. 출력은 `Event` JSONL이며 매 이벤트마다 flush하므로 별도 `paper --input ...` 소비자가 읽을 수 있습니다. 기존 기록과 혼합하지 않도록 비어 있지 않은 출력은 거절하고 파일 잠금으로 동시에 두 수집기가 쓰지 못하게 합니다. 새로운 파일명을 사용하세요. HTTP 오류는 상태 코드만 표시하며 인증 헤더·응답 원문·RSS URL의 쿼리 문자열은 출력하지 않습니다.

```bash
# 토스 REST 호가 1회. --poll 2는 2초 간격 반복 수집이며 Ctrl+C로 종료합니다.
pennyquant collect toss --symbols AAPL,MSFT --output data/toss-quotes.jsonl
pennyquant collect toss --symbols AAPL --output data/toss-poll.jsonl --poll 2

# 토스 WebSocket 호가. 체결 채널도 함께 구독하지만 JSONL에는 호가만 저장합니다.
pennyquant collect toss --symbols AAPL --output data/toss-stream.jsonl --stream

# Alpaca REST 보조 뉴스. --poll 60으로 반복 수집할 수 있습니다.
pennyquant collect alpaca --symbols AAPL --output data/alpaca-news.jsonl

# 실시간 권한을 실제 계정에서 확인한 경우에만 사용합니다.
pennyquant collect alpaca --symbols AAPL --output data/alpaca-stream.jsonl --stream --realtime-verified

# SEC는 acceptanceDateTime 미확인 공시까지 .filings.jsonl에 보존합니다.
pennyquant collect sec --cik 320193 --output data/sec-filings.jsonl

# 공식 목록에서 선택한 HTTPS 피드 URL을 넣습니다.
pennyquant collect rss --url 'https://example.com/company-feed.xml' --output data/rss-news.jsonl
```

`--poll 0`이 기본값이며 한 번 수집합니다. 반복 수집 간격은 1초 이상입니다. 스트림은 종료 시까지 계속합니다. 데이터가 없는 성공 응답은 빈 JSONL일 수 있으며 `.meta.json`에는 제공자와 수집 출처가 남습니다. SEC의 `.filings.jsonl`은 누락된 발표 시각을 `null`로 보존하는 메타데이터 기록으로, 거래 이벤트 입력이 아닙니다.

토스 수집 명령은 감시용 호가만 기록하고 기본 `tradable=false`를 유지합니다. `--symbols`를 입력해도 RSS 광역 피드의 모든 기사를 해당 종목에 연결하지 않습니다. 이 CLI의 제공자별 출력 하나만으로는 스캐너·RVOL·뉴스·닫힌 봉이 함께 있는 완전한 매매 입력이 만들어지지 않습니다. 동일 시간 거래량 이력, 검증된 종목 적격성 및 통합 수신 순서를 보존하는 별도 조립기가 필요하며, 그전까지 `paper`에서 진입이 없을 수 있습니다.

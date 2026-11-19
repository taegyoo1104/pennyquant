# 웹소켓 호가 연결과 운영 확인

현재 권장 실데이터 프로필은 **`config/sessionquant.strategy-lab-websocket.example.yaml`**입니다. 호가 수신기와 모의매매 엔진 연결은 구현되어 있습니다. 처음 실행은 [시작 안내](GETTING_STARTED.md), 매일 운영은 [Paper 운영](REALTIME_PAPER.md), 서버 배포는 [GCP 운영](GCP_PAPER.md)을 따르세요.

이 문서는 저장소 코드의 동작을 설명합니다. 코드 병합·자동 테스트 통과와 실제 GCP에 그 코드·프로필이 배포된 것은 별개입니다. 연결 속도와 장중 동작은 운영 기록으로 확인해야 합니다.

## 무엇이 웹소켓으로 바뀌었나요?

| 자료·작업 | 현재 경로 |
| --- | --- |
| 후보 최대 50개와 후보 밖 보유·미체결·청산 대기 종목의 호가 | 토스 웹소켓 구독 |
| 집중 최대 10개와 위험 관리 종목의 전략·체결 판단 | 수신기의 최신값을 약 2초마다 묶어 엔진에 전달 |
| 당일 분봉·거래대금·상장 정보 | 기존 REST 조회 |
| 뉴스 수집·LLM 분석 | 별도 주기와 비용 한도 |
| 대시보드 | 저장된 상태를 부분 갱신. 토스 웹소켓과 브라우저 화면은 별도 경로 |
| 주문 | `PaperBroker`의 모의 주문·체결 |

웹소켓 메시지마다 DB 저장이나 전략 계산을 실행하지 않습니다. 종목별 최신 호가를 메모리에 보관하고 필요한 묶음을 엔진에 전달해 기록합니다. DB·계산이 느리면 전달 주기도 늘어나므로 **2초 지연을 보장하는 서비스는 아닙니다.** 웹소켓으로 분봉·RVOL 거래량을 재구성하지도 않습니다.

단순 눌림목은 RVOL을 필수 진입 조건으로 쓰지 않고, 이 프로필은 시장 ETF 필터도 끕니다. 당일 확정봉·ATR 준비, 개장 75분 이후 거래 시간, 개별 호가 신선도, 스프레드·잔량·상장 상태·자금·위험 검사는 유지합니다. `quote_alignment_seconds: null`은 종목끼리 호가 시각이 2초 안에 모여야 한다는 비교만 해제합니다.

## 끊겼다가 연결되면 어떻게 하나요?

1. **끊김을 감지하면 이전 호가를 무효화합니다.** 새 매수를 막고 엔진의 다음 처리에서 남은 매수 주문도 취소합니다. 종료 작업이 오래 걸려도 무효화부터 수행합니다.
2. **새 구독 수락과 새 자료를 기다립니다.** 이전 연결의 늦은 응답·종료 콜백은 새 연결 상태를 바꾸지 못합니다. 수신기만 새 값을 가지고 엔진이 아직 받지 못한 동안에도 이전 연결 호가로 진입하지 않습니다.
3. **중복·역순·충돌을 검사합니다.** 같은 시각의 같은 호가는 새 체결 유동성으로 쓰지 않습니다. 같은 시각에 값이 충돌하면 해당 종목 값을 무효화하고 더 새로운 자료를 기다립니다. 엔진 재시작 후에도 저장 호가보다 오래된 자료로 진입을 다시 허용하지 않습니다.
4. **보유 위험 관리는 계속합니다.** 보유·미체결·청산 대기 종목의 호가가 없으면 기존 REST 요청 예산에서 대체 조회합니다. REST도 실패하거나 오래되면 가상의 최신 체결을 만들지 않습니다.

`SUBSCRIBED`는 구독이 수락됐다는 뜻입니다. 모든 종목의 호가가 최신이거나 매수 조건을 통과했다는 뜻은 아닙니다. 연결 유지 신호인 `pong`도 시세 신선도를 대신하지 않습니다.

## 로컬 실행

기존 엔진을 종료하고 다른 장부의 보유·미체결을 먼저 정리·백업합니다. 웹소켓 프로필과 실행 공간을 함께 선택합니다.

PowerShell:

```powershell
$env:SQ_DB_NAMESPACE="sessionquant-strategy-lab-ws-v1"
python -m sessionquant run --config config/sessionquant.strategy-lab-websocket.example.yaml
```

Bash:

```bash
export SQ_DB_NAMESPACE=sessionquant-strategy-lab-ws-v1
python -m sessionquant run --config config/sessionquant.strategy-lab-websocket.example.yaml
```

이후 상태 조회도 같은 터미널 환경 또는 같은 환경변수로 실행합니다. MySQL의 `SQ_DB_NAMESPACE`/`PQ_DB_NAMESPACE`는 YAML의 `db_path`보다 우선합니다. 프로필을 바꿔 기존 보유가 보이지 않는 것은 청산이 아닙니다. 이미 운영한 장부를 혼합하거나 삭제하지 않습니다.

## GCP에서 전환

먼저 [장전 업데이트](GCP_PAPER.md#장전-업데이트)로 검토한 코드와 이미지를 설치합니다. **기존 REST 장부에서 처음 전환할 때만**, 장전이며 기존 보유·미체결이 모두 정리된 상태에서 운영 VM 안의 `tmux`에서 실행합니다.

```bash
sudo bash /opt/sessionquant/deploy/gcp/activate_websocket.sh
```

이 명령은 다음을 차례로 수행합니다.

- 업데이트와 같은 잠금으로 중복 작업을 막고, 현재 환경·장전 시간·종료 작업 여부를 확인합니다.
- 기존 앱을 멈춘 뒤 **기존 설정과 장부 그대로** 종료·보유·미체결을 확인합니다. 대상 웹소켓 장부도 읽어 호환 상태와 미처리 제어 요청을 확인합니다.
- 새 Cloud Storage 백업이 `UPLOADED`로 완료됐는지 확인합니다.
- 원본 환경 파일을 비공개로 보관하고 아래 두 설정을 함께 적용합니다.
- 같은 서비스를 시작하고 장 마감·저장 공간·비용 방지 타이머를 확인합니다.

```dotenv
SQ_SESSION_CONFIG=config/sessionquant.strategy-lab-websocket.example.yaml
SQ_DB_NAMESPACE=sessionquant-strategy-lab-ws-v1
```

이미 두 설정이 선택돼 있으면 바꾸지 않고 현재 컨테이너 상태를 보여 줍니다. 이것만으로 건강 상태 검증이나 미완료 업데이트 재시작을 대신하지는 않습니다. 설정 변경 전 실패하면 기존 설정을 유지하고 서비스를 다시 시작하려고 시도합니다. **설정 변경 후 시작 실패는 새 장부를 숨기지 않도록 자동 복귀하지 않습니다.** 오류와 현재 설정을 확인합니다.

미지정 시 GCP Compose는 이전 `realtime-paper`를 사용합니다. `stop.sh`는 VM까지 종료하므로 프로필 전환 대신 실행하지 않습니다. 이전 프로필로 돌아갈 때도 새 장부의 보유·미체결 정리와 새 백업을 먼저 확인하고 두 설정을 함께 복원해야 합니다.

## 실행 중 점검

로컬은 다음 읽기 전용 명령을 사용합니다.

```powershell
python -m sessionquant paper-status --config config/sessionquant.strategy-lab-websocket.example.yaml
```

GCP는 **운영 VM `@sessionquant-paper` 터미널**에서 실행합니다. `exec`는 이미 실행 중인 컨테이너 안에서 저장 상태만 읽으며, 별도 토스 토큰을 발급하거나 엔진을 재시작하지 않습니다.

```bash
sudo docker compose -f /opt/sessionquant/deploy/gcp/compose.yaml exec -T app \
  python -m sessionquant paper-status --config config/sessionquant.strategy-lab-websocket.example.yaml
```

| 항목 | 확인할 내용 |
| --- | --- |
| `quote_transport` | `websocket`인지 |
| `websocket.state`, `acknowledged` | `SUBSCRIBED`, `true`인지 |
| `engine.heartbeat_fresh`, `websocket.observed_at` | 현재 엔진·관측 기록인지 |
| `fresh_count` | 전체 구독 중 신선한 호가가 있는 종목 수 |
| `selected_fresh_count` | 집중·위험 관리 종목 중 신선한 호가가 있는 수 |
| `delivery_batches` | 전달 묶음이 시간이 지나며 증가하는지 |
| `latest_candidates` | 각 평가 시각의 전략·주문 차단 이유 |

연결 요약은 `websocket_quotes_v1`에 약 10초마다 저장하므로 조회 시각과 차이가 있을 수 있습니다. 명령 성공만으로 수신 성공을 판단하지 않습니다. 운영에서는 수신량, CPU·메모리, 엔진 처리 시간과 DB 크기, 모의 주문·체결·매도를 함께 확인합니다.

## 고급: 수신기만 따로 점검

**같은 토스 인증을 사용하는 엔진·수집기를 중지한 뒤** 실행합니다. 새 토큰 발급이 기존 연결에 영향을 줄 수 있고 계정당 웹소켓 연결 한도는 2개입니다.

```bash
python -m sessionquant.ws_probe --symbols AAPL MSFT --seconds 60
```

실제 토스 연결을 열고 5초마다 상태를 출력합니다. 주문·LLM·DB 연결은 없으며 정해진 시간이 끝나면 연결과 초기 REST 작업을 정리합니다. 종료 코드만 보지 말고 `acknowledged`, `fresh_count`, 오류 유형을 확인하세요.

수신기 구현의 범위는 다음과 같습니다.

- 구독은 후보 최대 50개와 후보 밖 위험 종목을 중복 제거해 합칩니다. 총 100개 초과 시 오류를 내며 보유 종목을 조용히 잘라내지 않습니다.
- 구독 수락 후 초기 REST 호가 조회를 별도 비동기 작업에서 수행합니다. 보유·미체결 우선, 같은 인증 클라이언트와 요청 한도를 공유합니다. 이미 최신 웹소켓 값이 있으면 초기 조회를 생략하고 늦은 REST 결과가 새 값을 덮어쓰지 못하게 합니다.
- 구독 집합이 바뀌면 기존 연결을 닫은 뒤 새 전체 목록으로 연결합니다. 같은 집합에는 재연결하지 않습니다. 연결을 겹치지 않지만 잠깐 수신 공백은 생깁니다.
- 재시도 간격은 기본 1·2·4·8·16·30초이며 최대 30초입니다. 60초 유지 후 초기화합니다. 구독 수락 대기는 15초, 전체 무수신은 90초 제한이며 일부 구독 거절도 전체 연결 사용 불가로 표시합니다.
- 독립 점검의 호가 유효시간 기본값은 30초, 엔진 연결은 엔진·체결 모형 중 더 짧은 제한(현재 10초)을 사용합니다. 원본 시각·수신 시각·엔진 전달 시각을 구분합니다.
- 체결 스트림, 실계좌 주문 이벤트, 끊긴 동안의 모든 호가 복구는 이 구현의 범위가 아닙니다. 원시 인증 오류·토큰·응답 전문은 상태에 출력하지 않습니다.

자동 검사는 가짜 HTTP·웹소켓과 로컬 루프백 연결로 끊김·재연결·중복·역순·신선도·엔진 전달을 검증합니다. 대시보드 부분 갱신과 집중 카드도 구현되어 있지만 실제 토스/GCP의 장중 처리량·모바일 터널 안정성은 별도 운영 검증 대상입니다.

공급자 문서: [토스 AsyncAPI](https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json), [연동 가이드](https://openapi.tossinvest.com/openapi-docs/overview.md), [FAQ](https://openapi.tossinvest.com/openapi-docs/faq.md).

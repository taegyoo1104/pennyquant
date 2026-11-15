# 로컬 MySQL 8 연결과 데이터 관리

SessionQuant의 실제 시세 모의 운영은 로컬 MySQL을 기본으로 사용합니다. 설치된 MySQL 서버를 재설치하거나 기존 DB를 삭제할 필요가 없습니다. 합성 예제와 과거 SQLite 재생은 SQLite에서도 실행할 수 있습니다. MySQL 연결에 실패하면 SQLite로 자동 전환하지 않고 실행을 중지합니다.

이 저장소를 개발하는 서버에서는 사용자 PC의 `127.0.0.1`에 접근할 수 없습니다. 아래 설정과 `db-check`는 본인 PC에서 실행하세요. 비밀번호와 API 키를 채팅이나 Git에 올리지 마세요.

## 1. DB와 전용 계정 만들기

MySQL Workbench에서 설치된 로컬 서버에 관리자 계정으로 연결합니다. [scripts/setup_mysql.sql](../../scripts/setup_mysql.sql)을 열고 비밀번호 자리만 본인 PC에서 수정하여 실행하세요. 새 데이터베이스와 프로젝트 전용 계정을 생성하며 기존 데이터베이스를 삭제하지 않습니다. 이미 같은 계정이 있으면 기존 비밀번호가 유지됩니다.

기본 접속 주소는 `127.0.0.1:3306`, DB와 사용자 이름은 `sessionquant`입니다. 서버가 이름 확인을 사용하지 않아 `'sessionquant'@'localhost'` 연결을 거부한다면 Workbench에서 정확한 로컬 접속 호스트용 계정 권한을 확인하세요. 외부 호스트 `%`에 접근 권한을 줄 필요는 없습니다.

## 2. PyCharm 터미널 설정

GitHub 변경사항을 받은 뒤 프로젝트의 PowerShell 터미널에서 실행합니다.

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.lock
.\venv\Scripts\python.exe -m pip install --no-deps -e .

$env:SQ_DB_BACKEND = "mysql"
$env:SQ_ENVIRONMENT = "PAPER_LOCAL"
$env:SQ_MYSQL_HOST = "127.0.0.1"
$env:SQ_MYSQL_PORT = "3306"
$env:SQ_MYSQL_DATABASE = "sessionquant"
$env:SQ_MYSQL_USER = "sessionquant"
$secret = Read-Host "MySQL 전용 계정 비밀번호" -AsSecureString
$env:SQ_MYSQL_PASSWORD = [System.Net.NetworkCredential]::new("", $secret).Password
.\venv\Scripts\python.exe -m sessionquant db-check --db data/sessionquant-paper-local.db
```

비밀번호 입력란에는 1번에서 설정한 **MySQL 전용 계정 비밀번호**를 입력합니다. 토스 API 클라이언트 시크릿이 아닙니다. 입력을 한 번 마쳤다면 같은 터미널에서 아래 명령으로 저장하세요. 이후 PyCharm·터미널을 다시 열어도 실제 `.env`를 자동으로 읽습니다. `.env.example` 자체는 읽지 않습니다. 기존 프로세스 환경변수가 파일보다 우선합니다.

```powershell
.\venv\Scripts\python.exe -m sessionquant setup-env --from-current
```

값이 누락됐다면 `setup-env`를 실행해 한 번 입력하세요. 파일은 Git/Docker에서 제외되며 실주문 활성화는 저장하지 않습니다.

`db-check`는 접속과 테이블 구성을 확인하고 행 수·용량을 출력합니다. 정상 결과의 `ready`는 `true`입니다. 비밀번호를 출력하거나 실제 주문을 전송하지 않습니다. 새 DB의 테이블은 첫 연결 시 자동 생성됩니다.

새 설정의 `SQ_`가 우선하고 예전 `PQ_` 환경변수도 호환됩니다. `PQ_MYSQL_`만 사용하면 기존 기본 DB·사용자 이름 `pennyquant`를 유지합니다. `SQ_ENVIRONMENT`가 설정된 경우 네임스페이스는 예를 들어 `paper_local_sessionquant-paper-local`이 됩니다. 환경이 없던 이전 원장의 네임스페이스는 이름만 바뀌었다는 이유로 수정하지 않습니다.

## 3. 어떤 데이터를 저장하나요?

| 영역 | 저장 내용 |
|---|---|
| 시세·기술적 분석 | 수신한 1분봉, 호가, 거래 후보, 스캐너 점수, 시장 국면 |
| 뉴스 | 기업 공시·RSS·Alpaca 자료, 거시·세계 뉴스, 원문 URL, 공개·수신 시각 |
| LLM | 사건 분류·근거·불확실성, 모델·프롬프트 버전, 실제 토큰 사용량과 계산 비용 |
| 거래 장부 | 신호, 주문 의도, 모의 주문, 실제 모의 체결, 소수점 포지션, 수수료·손익 |
| 계좌·위험 | 현금·평가금액, 일일 손실 제한, 청산 상태, 알림, 대사 기록 |
| 재시작 | 처리 커서, 주문 멱등성 기록, 시장 문맥, 위험 상태, 실행 환경 |

기존 `pq_` 테이블은 과거 기록 호환성을 위해 유지합니다. 이름은 영어로 유지하며 내용은 한국어 설명을 포함할 수 있습니다. JSON 원장은 금액·수량을 정확한 소수 문자열로 저장하고, `sq_financial_ledger`는 관련 수치를 MySQL `DECIMAL(65,30)`으로 함께 저장합니다. 금융 수치 투영과 원장은 같은 트랜잭션으로 기록됩니다. `sq_schema_versions`에서 스키마 적용 이력을 확인할 수 있습니다.

모의·실거래 환경은 `PAPER_LOCAL`, `LIVE_LOCAL`, `LIVE_GCP`별 네임스페이스를 사용하고 실제 운영에서는 서로 다른 DB 계정·DB도 사용하세요. 현재 Phase 0~4 실행기는 `PAPER_LOCAL`만 허용합니다. 동일 MySQL 네임스페이스의 엔진 중복 실행은 서버 잠금으로 차단합니다. 서로 다른 서버·DB의 동일 실계좌 중복 실행 방지는 Phase 5~6에서 계좌 단위 활성 엔진 관리와 함께 검증해야 합니다.

## 4. 예상 용량

용량은 수집 종목 수, 호가 수집 주기, 기사 본문 길이, 로그 양에 따라 달라집니다. 하루 390분·월 22거래일, 20개 관심 종목 중 매 주기 7개 호가, 10초 간격의 예에서는 대략 **일 30~100MB, 월 0.5~3GB**를 초기 계획 범위로 잡을 수 있습니다. 이는 상한이나 실제 측정 결과가 아닙니다. S&P 500 전체를 고빈도로 저장하면 이보다 크게 늘어납니다. 1초 수집은 동일 조건에서 시장 데이터만 10초 수집보다 약 9배 많아질 수 있습니다.

MySQL 바이너리 로그, 백업, JSONL 수집 파일은 별도 용량입니다. 초기 여유 디스크 20GB 이상을 확보하고 첫 1~2주 실제 측정치로 보관 기간을 정하세요. 상세 계산과 주의사항은 [DATABASE_STORAGE.md](DATABASE_STORAGE.md)를 참고하세요.

```powershell
.\venv\Scripts\python.exe -m sessionquant db-stats --db data/sessionquant-paper-local.db
```

`namespace_payload_bytes`는 해당 실행 공간의 JSON 원장 크기, `physical_table_bytes`는 같은 DB 안의 **전체 SessionQuant 네임스페이스**를 포함한 테이블·인덱스 크기입니다. 서버 로그·백업을 포함한 디스크 총 사용량과 다릅니다.

## 5. 보관 기간 정리

자동 삭제는 기본으로 하지 않습니다. 처음에는 예상 삭제 건수만 확인하세요.

```powershell
.\venv\Scripts\python.exe -m sessionquant db-prune --db data/sessionquant-paper-local.db --days 7 --kinds price_samples,quote_snapshots
```

실제로 정리하려면 엔진을 종료한 뒤 같은 명령에 `--apply`를 추가합니다. 주문·체결·포지션·뉴스 분석·위험 상태는 이 기능의 삭제 대상이 아닙니다. 1분봉 `market_bars`를 삭제하면 이후 백테스트에 필요한 자료가 사라질 수 있으므로 별도 백업 후 선택하세요. MySQL에서 행을 지워도 파일 크기가 즉시 줄지는 않을 수 있습니다.

## 6. 기존 SQLite 보존과 이관

새 S&P 500 엔진은 새로운 DB 공간으로 시작하는 것이 권장됩니다. 기존 PennyQuant 주문 상태는 전략·스키마가 달라 새 SessionQuant 모의 상태로 자동 변환하지 않습니다. 과거 자료를 MySQL에 보존하려면 해당 엔진을 종료한 뒤 별도 빈 네임스페이스로 이관하세요.

```powershell
$env:SQ_DB_NAMESPACE = "legacy-archive"
.\venv\Scripts\python.exe -m sessionquant db-migrate --source data/system.db --db data/legacy-archive.db
Remove-Item Env:SQ_DB_NAMESPACE
```

원본 SQLite는 읽기 전용으로 열며 기록 ID, 이벤트 ID, 주문 ID, 제어 값과 장부 상태를 유지합니다. 대상에 기존 자료가 있으면 덮어쓰지 않고 거부합니다. 이관 후 과거 자료 조회·비교에 사용하고 새 엔진 실행 DB와 혼용하지 마세요.

## 7. 백업·복구

작은 로컬 DB는 엔진을 종료하고 프로젝트의 논리 백업 도구로 전체 원장을 저장할 수 있습니다. 복구는 빈 저장소에만 가능하고 수동 거래 중단 상태로 시작합니다.

```powershell
.\venv\Scripts\python.exe -c "from sessionquant.database.operations import backup_database; print(backup_database('data/sessionquant-paper-local.db', 'backups/sessionquant-snapshot.json'))"
$env:SQ_DB_NAMESPACE = "restore-check"
.\venv\Scripts\python.exe -c "from sessionquant.database.operations import restore_database; print(restore_database('backups/sessionquant-snapshot.json', 'data/restore-check.db'))"
Remove-Item Env:SQ_DB_NAMESPACE
```

논리 백업은 메모리에 전체 원장을 읽으므로 대용량에는 MySQL Workbench의 Data Export/Import 또는 `mysqldump --single-transaction`을 사용하세요. 비밀번호는 명령행에 쓰지 말고 `-p` 프롬프트에서 입력합니다. 백업에는 계좌·거래 내용이 포함되므로 Git에 넣지 말고 접근을 제한하세요. 테스트 환경에서는 정확한 소수값, 주문 ID, 상태, 제어 값의 백업·복구와 재시작 후 중복 체결 방지를 확인합니다. 사용자 PC의 실제 복구 검증은 별도 테스트 DB에서 실행하세요.

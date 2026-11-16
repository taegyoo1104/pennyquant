# 저비용 GCP 모의매매

프로그램과 MySQL 8.4를 **GCE 서버 한 대**에 설치한다. Cloud SQL은 사용하지 않는다. 서버 켜기와 대시보드의 매매 시작은 수동이며, 거래소 폐장 5분 후 프로그램 종료 → 백업 → 서버 종료를 자동 처리한다. 실제 주문은 계속 비활성이다.

현재 저장소에는 설치·종료·백업 파일이 준비되어 있다. GCP 프로젝트 생성과 실제 배포·토스 연결·백업 업로드는 사용자 계정에서 별도로 확인해야 한다. 아래 자원 생성 명령은 과금 대상 자원을 만든다. 실행 전에 프로젝트에 무료 크레딧이 있는 결제 계정을 연결하고 생성 화면의 예상 비용을 확인한다.

## 구성과 비용

| 항목 | 초기 구성 |
| --- | --- |
| 서버 | `e2-small` · 메모리 2GB · Debian 12 · `us-central1-a` |
| DB | 같은 서버의 MySQL, 외부 접속 차단 |
| 데이터 | 20GB 표준 영구 디스크. 서버 중지 후에도 보존 |
| 화면 | SSH/IAP 터널을 통한 `localhost:8501` 접속 |
| 백업 | 종료 시 SQL과 앱 자료를 묶음. 서버 안 최근 3개, 비공개 Cloud Storage 7일 보관 |
| 시작 | GCP에서 서버를 수동 시작 → 예열 → 화면에서 매매 시작 |
| 종료 | 뉴욕 거래소 달력의 폐장 + 5분. 저장·백업 소요 시간만큼 실제 VM 종료는 늦어질 수 있음 |

서버를 사용한 시간의 CPU·메모리·외부 IPv4, 영구 디스크, 백업 저장/요청·전송량이 비용 항목이다. 고정 IP·Cloud SQL·Cloud NAT·로드밸런서·Cloud Scheduler는 만들지 않는다. 서버를 중지하면 계산 자원 요금은 멈추지만 디스크와 백업 비용은 남는다. 임시 외부 IP는 중지 후 반납되며 다시 켜면 달라질 수 있다.

`e2-small`은 무료 사용량 대상 `e2-micro`와 다르다. 1GB 서버에 DB·수집·화면을 함께 넣으면 메모리 부족 위험이 있어 초기값은 2GB로 잡았다. 무료 크레딧의 남은 금액·만료일·사용 가능 서비스는 결제 화면에서 확인한다. 이는 확정 월 견적이나 비용 상한이 아니다. 비용은 실행 시간과 사용량으로 확인한다.

OpenAI API 비용은 GCP 크레딧과 별개다. 기존 AI 월 한도 설정을 유지한다.

참고: [VM 중지와 시작](https://cloud.google.com/compute/docs/instances/stop-start-instance), [무료 크레딧·무료 사용량](https://cloud.google.com/free/docs/free-cloud-features).

## 1. 프로젝트와 서버 만들기

GCP 콘솔 상단의 프로젝트 선택 → **새 프로젝트**에서 이름을 `sessionquant`로 정하고 생성된 **프로젝트 ID**를 확인한다. 결제 메뉴에서 무료 크레딧이 있는 계정을 연결한다. ID는 비밀값이 아니지만 API 키·비밀번호는 채팅이나 Git에 올리지 않는다.

이후 Cloud Shell에서 아래 명령을 사용한다. `YOUR_PROJECT_ID`를 실제 ID로 바꾼다. 이미 같은 이름의 자원이 있으면 삭제하거나 덮어쓰지 말고 기존 설정을 확인한다.

```bash
export SQ_PROJECT_ID='YOUR_PROJECT_ID'
export SQ_BACKUP_BUCKET="${SQ_PROJECT_ID}-sq-backups"
export SQ_VM_SA="sessionquant-vm@${SQ_PROJECT_ID}.iam.gserviceaccount.com"
gcloud config set project "$SQ_PROJECT_ID"
gcloud services enable compute.googleapis.com iap.googleapis.com storage.googleapis.com iam.googleapis.com oslogin.googleapis.com
gcloud iam service-accounts create sessionquant-vm --display-name='SessionQuant backup uploader'
gcloud storage buckets create "gs://$SQ_BACKUP_BUCKET" --location=us-central1 --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets add-iam-policy-binding "gs://$SQ_BACKUP_BUCKET" --member="serviceAccount:$SQ_VM_SA" --role=roles/storage.objectCreator
git clone --branch wip/stage06-handoff-20261115 https://github.com/taegyoo1104/pennyquant.git
cd pennyquant
gcloud storage buckets update "gs://$SQ_BACKUP_BUCKET" --lifecycle-file=deploy/gcp/backup-lifecycle.json
gcloud compute networks create sessionquant-net --subnet-mode=custom
gcloud compute networks subnets create sessionquant-subnet --network=sessionquant-net --region=us-central1 --range=10.42.0.0/24
gcloud compute firewall-rules create sessionquant-iap-ssh --network=sessionquant-net --allow=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=sessionquant-iap
gcloud compute instances create sessionquant-paper --zone=us-central1-a --machine-type=e2-small --image-family=debian-12 --image-project=debian-cloud --boot-disk-size=20GB --boot-disk-type=pd-standard --no-boot-disk-auto-delete --subnet=sessionquant-subnet --tags=sessionquant-iap --service-account="$SQ_VM_SA" --scopes=cloud-platform --metadata=enable-oslogin=TRUE --no-restart-on-failure
gcloud compute ssh sessionquant-paper --zone=us-central1-a --tunnel-through-iap
```

VM 서비스 계정은 해당 백업 버킷에 새 파일을 쓰는 권한만 가진다. JSON 서비스 계정 키를 발급하지 않는다. 별도 사용자가 SSH를 이용할 때에는 IAP 터널·OS Login·서비스 계정 사용 권한을 추가로 확인해야 한다.

## 2. 서버 안에서 설치

SSH로 들어간 **서버 안에서** 실행한다. 마지막 인자는 1단계에서 만든 버킷 이름이다.

```bash
sudo git clone --branch wip/stage06-handoff-20261115 https://github.com/taegyoo1104/pennyquant.git /opt/sessionquant
cd /opt/sessionquant
sudo bash deploy/gcp/install.sh YOUR_PROJECT_ID-sq-backups
sudo nano /etc/sessionquant/app.env
```

설치가 생성한 `SQ_MYSQL_*` 값은 유지하고, PC에서 사용하던 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT_SEQ`, `OPENAI_API_KEY`, `SEC_USER_AGENT` 및 사용하는 뉴스 키를 입력한다. 값은 한 줄로 입력한다. 이 파일은 관리자만 읽을 수 있다. PC의 `.env`를 통째로 덮어써서 서버 DB 비밀번호를 바꾸지 않는다.

설치 스크립트는 기존 비밀번호·DB를 초기화하지 않는다. 이 단계에서는 자동 실행·종료를 아직 활성화하지 않으므로 키 입력·자료 이전을 마칠 수 있다. 서버가 켜진 동안에는 사용 시간이 누적된다.

## 3. 기존 DB 이전

PC에서 실행 중인 프로그램을 먼저 종료한다. 이후 PC와 클라우드에서 같은 모의 프로그램을 동시에 운영하지 않는다. 클라우드 DB에는 새로 생성된 비밀번호를 사용하며 기존 PC DB 비밀번호를 복사하지 않는다.

PC 터미널에서 MySQL 명령 도구를 사용한다. DB 이름·사용자가 다른 경우 현재 값을 지정한다. `-p`는 비밀번호를 별도로 입력받으며 명령행에 비밀번호를 쓰지 않는다. PowerShell의 출력 인코딩을 피하려고 `--result-file`을 사용한다.

```powershell
mysqldump -h 127.0.0.1 -u sessionquant -p --single-transaction --quick --no-tablespaces --set-gtid-purged=OFF --result-file=cloud-migration.sql sessionquant
gcloud compute scp cloud-migration.sql sessionquant-paper:~/cloud-migration.sql --project=YOUR_PROJECT_ID --zone=us-central1-a --tunnel-through-iap
gcloud compute scp data/sp500-universe.json sessionquant-paper:~/sp500-universe.json --project=YOUR_PROJECT_ID --zone=us-central1-a --tunnel-through-iap
```

Windows에 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)가 필요하다. 처음 한 번 `gcloud auth login`으로 로그인한다. SQL 덤프는 전체 저장 자료를 담으므로 공개 저장소에 넣지 않는다.

서버 안에서 **앱이 한 번도 실행되지 않은 빈 DB**에 가져온다. 이미 클라우드 거래 기록이 있다면 이 명령으로 덮어쓰지 않는다.

```bash
cd /opt/sessionquant
sudo docker compose -f deploy/gcp/compose.yaml up -d --wait db
sudo bash deploy/gcp/import_database.sh "$HOME/cloud-migration.sql"
sudo install -o 1000 -g 1000 -m 600 "$HOME/sp500-universe.json" /var/lib/sessionquant/app/sp500-universe.json
sudo docker compose -f deploy/gcp/compose.yaml run --rm --no-deps app python -m sessionquant history-status --config config/sessionquant.realtime-paper.example.yaml
sudo docker compose -f deploy/gcp/compose.yaml run --rm --no-deps app python -m sessionquant check-connections --config config/sessionquant.realtime-paper.example.yaml
sudo bash deploy/gcp/backup.sh
```

`history-status` 결과가 PC의 준비 종목과 맞는지, 백업이 버킷에 실제로 생겼는지 확인한다. 기본 실행 공간은 `paper_local_sessionquant-realtime-paper-v1`이다. PC에서 다른 `SQ_DB_NAMESPACE`를 사용했다면 서버에도 동일한 실행 공간을 지정해야 한다. `prepare-paper`를 다시 실행하지 않는다. 분봉 재수집 대신 기존 SQL의 분봉·시총·모의 장부·뉴스 원장을 그대로 이전한다.

`check-connections`는 GCP에서 실제 API의 읽기 연결을 확인하지만 주문·유료 AI 생성은 하지 않는다. 토스·DB와 사용하는 뉴스 공급자 연결을 확인한 뒤 진행한다. 이전과 백업 확인 후 평문 SQL 전송 파일은 PC와 서버에서 삭제하고 비공개 백업만 보관한다. PC의 원본 DB는 이전 검증이 끝날 때까지 보존한다.

## 4. 수동 시작·자동 종료 활성화

**실제 거래일에 사용할 준비가 끝난 뒤** 서버 안에서 실행한다.

```bash
sudo systemctl enable sessionquant.service sessionquant-cost-guard.timer
sudo systemctl start sessionquant.service
```

이후에는 GCP 콘솔 → Compute Engine → VM → `sessionquant-paper` → **시작**만 누르면 DB와 프로그램이 실행된다. 모의매매는 예열 후 대시보드에서 **매매 시작**을 눌러야 한다.

PC의 Google Cloud CLI에서 아래 터널을 연 뒤 `http://localhost:8501`에 접속한다.

```powershell
gcloud compute ssh sessionquant-paper --project=YOUR_PROJECT_ID --zone=us-central1-a --tunnel-through-iap -- -N -L 8501:127.0.0.1:8501
```

시작을 누른 뒤 PC나 터널을 꺼도 서버의 프로그램은 계속 실행된다. 정규장 종료 5분 뒤 자동으로 프로그램을 정리하고 백업 후 VM을 중지한다. 일반 거래일 한국시간으로 겨울에는 다음 날 06:05, 서머타임에는 다음 날 05:05부터 종료 처리를 시작한다. 조기 폐장은 거래소 달력을 따른다.

- 휴장일 또는 그날 종료 시각 이후에 켜면 수집을 시작하지 않고 1분 뒤 꺼진다. 다음 거래일까지 켜 두지 않는다.
- 달력을 읽지 못하면 거래를 시작하지 않고 15분 뒤 종료한다. 별도 비용 방지 타이머는 부팅 20시간 후 서버를 종료한다. 월 비용 상한 기능은 아니다.
- 자동 종료는 체결 보장이 아니다. 미청산 모의 수량은 저장해 다음 실행에 복구하며 청산 완료로 바꾸지 않는다.
- 프로그램 오류가 나도 마감 타이머는 남는다. 자동 재매매 시작은 하지 않는다.
- 백업 업로드 실패 시 로컬 백업과 원본 DB 디스크를 보존하고 VM을 종료한다. 다음 실행 때 로그를 확인한다.

## 5. 확인·업데이트·복구

서버 안에서 상태를 확인한다. `paper-status`, `news-status`는 외부 API를 호출하지 않는다.

```bash
sudo systemctl list-timers 'sessionquant-*'
sudo journalctl -u sessionquant.service -u sessionquant-market-close.service -u sessionquant-cost-guard.service --since today
cd /opt/sessionquant
sudo docker compose -f deploy/gcp/compose.yaml logs --tail=100 app
sudo docker compose -f deploy/gcp/compose.yaml run --rm --no-deps app python -m sessionquant paper-status --config config/sessionquant.realtime-paper.example.yaml
```

점검·주말 뉴스 확인 때문에 자동 종료를 잠시 끄려면 실행 중 VM에서 다음을 적용한다. **서버가 계속 켜져 있으므로 점검 후 직접 중지하거나 자동 종료 설정을 다시 활성화해야 한다.**

```bash
sudo systemctl disable --now sessionquant.service sessionquant-cost-guard.timer
sudo systemctl stop sessionquant-market-close.timer
```

코드 업데이트도 위와 같이 엔진과 타이머를 먼저 중지하고 `/opt/sessionquant`에서 `sudo git pull --ff-only origin wip/stage06-handoff-20261115`, `sudo docker build -t sessionquant:local .`을 실행한다. 변경된 unit 파일이 있다면 설치 스크립트를 다시 실행한다. 마지막에는 4단계로 자동 실행·종료를 다시 활성화한다. DB 디렉터리를 삭제하거나 `down -v`를 실행하지 않는다.

백업은 `database.sql`과 `app/`을 담은 `tar.gz`다. 복구는 자동 실행을 끈 **별도 빈 서버**에서만 한다. 관리자 계정으로 버킷에서 백업을 내려받아 임시 폴더에 풀고, 3단계와 같은 방식으로 SQL을 빈 DB에 가져온 뒤 `app/`을 `/var/lib/sessionquant/app`에 복사하고 소유자를 `1000:1000`으로 맞춘다. 최신 백업의 장부·분봉·뉴스 기록을 확인한 뒤 실행한다. 기존 운영 DB 위에 덮어쓰지 않는다. 이 설치의 버킷과 디스크는 VM을 삭제해도 별도로 남으므로 프로젝트를 종료할 때 잔여 저장 비용도 확인한다.

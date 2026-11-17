# 저비용 GCP 모의매매

프로그램과 MySQL 8.4를 **GCE 서버 한 대**에 설치한다. Cloud SQL은 사용하지 않는다. 서버 켜기와 대시보드의 매매 시작은 수동이며, 거래소 폐장 5분 후 프로그램 종료 → 백업 → 서버 종료를 자동 처리한다. 실제 주문은 계속 비활성이다.

현재 저장소에는 설치·종료·백업 파일이 준비되어 있다. GCP 프로젝트 생성과 실제 배포·토스 연결·백업 업로드는 사용자 계정에서 별도로 확인해야 한다. 아래 자원 생성 명령은 과금 대상 자원을 만든다. 실행 전에 프로젝트에 무료 크레딧이 있는 결제 계정을 연결하고 생성 화면의 예상 비용을 확인한다.

## 구성과 비용

| 항목 | 현재 운영 구성 |
| --- | --- |
| 서버 | `e2-standard-2` · 2 vCPU·메모리 8GB · Ubuntu 24.04 · `us-central1-a` |
| DB | 같은 서버의 MySQL, 외부 접속 차단 |
| 데이터 | 40GB 표준 영구 디스크. 서버 중지 후에도 보존 |
| 화면 | SSH/IAP 터널을 통한 `localhost:8501` 접속 |
| 백업 | 종료 시 SQL과 앱 자료를 묶음. 서버 안 최근 3개, 비공개 Cloud Storage 7일 보관 |
| 시작 | GCP에서 서버를 수동 시작 → 예열 → 화면에서 매매 시작 |
| 종료 | 뉴욕 거래소 달력의 폐장 + 5분. 저장·백업 소요 시간만큼 실제 VM 종료는 늦어질 수 있음 |

서버를 사용한 시간의 CPU·메모리·외부 IPv4, 영구 디스크, 백업 저장/요청·전송량이 비용 항목이다. 고정 IP·Cloud SQL·Cloud NAT·로드밸런서·Cloud Scheduler는 만들지 않는다. 서버를 중지하면 계산 자원 요금은 멈추지만 디스크와 백업 비용은 남는다. 임시 외부 IP는 중지 후 반납되며 다시 켜면 달라질 수 있다.

초기 2GB·20GB 구성에서 장중 CPU 포화와 저장 공간 부족을 확인해 위 사양으로 변경했다. 무료 사용량 대상 `e2-micro`와는 다르다. 추가 증설은 처리 시간 기록을 확인한 뒤 판단한다. 무료 크레딧의 남은 금액·만료일·사용 가능 서비스는 결제 화면에서 확인한다. 이는 확정 월 견적이나 비용 상한이 아니다. 비용은 실행 시간과 사용량으로 확인한다.

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
git clone --branch main https://github.com/taegyoo1104/pennyquant.git
cd pennyquant
gcloud storage buckets update "gs://$SQ_BACKUP_BUCKET" --lifecycle-file=deploy/gcp/backup-lifecycle.json
gcloud compute networks create sessionquant-net --subnet-mode=custom
gcloud compute networks subnets create sessionquant-subnet --network=sessionquant-net --region=us-central1 --range=10.42.0.0/24
gcloud compute firewall-rules create sessionquant-iap-ssh --network=sessionquant-net --allow=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=sessionquant-iap
gcloud compute instances create sessionquant-paper --zone=us-central1-a --machine-type=e2-standard-2 --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud --boot-disk-size=40GB --boot-disk-type=pd-standard --no-boot-disk-auto-delete --subnet=sessionquant-subnet --tags=sessionquant-iap --service-account="$SQ_VM_SA" --scopes=cloud-platform --metadata=enable-oslogin=TRUE --no-restart-on-failure
gcloud compute ssh sessionquant-paper --zone=us-central1-a --tunnel-through-iap
```

VM 서비스 계정은 해당 백업 버킷에 새 파일을 쓰는 권한만 가진다. JSON 서비스 계정 키를 발급하지 않는다. 별도 사용자가 SSH를 이용할 때에는 IAP 터널·OS Login·서비스 계정 사용 권한을 추가로 확인해야 한다.

## 2. 서버 안에서 설치

SSH로 들어간 **서버 안에서** 실행한다. 새 Ubuntu 서버에는 Git이 없을 수 있으므로 먼저 설치한다. 프로젝트 ID는 GCE 메타데이터에서 읽고, 1단계의 기본 이름으로 만든 백업 버킷을 사용한다. 버킷 이름을 직접 다르게 정했다면 마지막 인자를 해당 이름으로 바꾼다.

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
sudo git clone --branch main https://github.com/taegyoo1104/pennyquant.git /opt/sessionquant
cd /opt/sessionquant
SQ_PROJECT_ID="$(curl -fsS -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/project/project-id)"
test -n "$SQ_PROJECT_ID" && sudo bash deploy/gcp/install.sh "${SQ_PROJECT_ID}-sq-backups"
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
sudo systemctl start sessionquant-cost-guard.timer
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

기존 운영 서버 업데이트는 아래 장전 절차를 사용한다. 서버에 수동 복구 수정이 남아 있을 수 있으므로 `reset --hard`나 강제 덮어쓰기를 하지 않는다. DB 디렉터리를 삭제하거나 `down -v`를 실행하지 않는다.

새 백업은 압축한 `database.sql.gz`와 `app/`을 담은 `tar.gz`다. 이전 백업은 `database.sql`일 수 있으며 두 형식을 가져오기 도구가 지원한다. 복구는 자동 실행을 끈 **별도 빈 서버**에서만 한다. 관리자 계정으로 버킷에서 백업을 내려받아 임시 폴더에 풀고, 3단계와 같은 방식으로 SQL을 빈 DB에 가져온 뒤 `app/`을 `/var/lib/sessionquant/app`에 복사하고 소유자를 `1000:1000`으로 맞춘다. 최신 백업의 장부·분봉·뉴스 기록을 확인한 뒤 실행한다. 기존 운영 DB 위에 덮어쓰지 않는다. 이 설치의 버킷과 디스크는 VM을 삭제해도 별도로 남으므로 프로젝트를 종료할 때 잔여 저장 비용도 확인한다.

## 장전 업데이트와 확인

앱의 최초 HTTP 응답은 DB·장부 복원 뒤 늦게 열릴 수 있다. 시작 후 3분은 상태 검사 실패를 유예하고, 정상 응답이 오면 즉시 준비된 것으로 판정한다. DB 시작을 포함한 Compose 준비 확인은 최대 8분, 바깥 systemd 시작 제한은 10분이다. 매번 그 시간을 기다리는 것은 아니며 예열 완료·매매 허가와도 별개다. 제한을 넘기거나 앱이 종료되면 실패를 표시하고 자동 종료 예약은 유지한다. 과거 버전에서 서비스가 실패로 남았어도 앱 컨테이너가 나중에 healthy가 될 수 있으므로 상태·로그를 함께 확인한다.

2026-11-17 장은 한국시간 **23:30 개장**, 현재 전략의 신규 매수 판단은 다음 날 **00:45부터**다. 22:00~22:30에 VM을 켜서 업데이트·예열을 시작하는 것을 권장한다. 실제 소요 시간은 API 응답에 따라 달라지며 자료 준비 완료를 보장하는 시각은 아니다. 휴장일·폐장 이후 부팅은 자동 종료하므로 장전 작업 시간에 실행한다.

1. **Cloud Shell**에서 운영 VM을 시작하고 외부 IP를 확인한다. IP가 달라졌다면 토스 API 허용 IP를 수정한다. 복구 VM은 켜지 않는다.

```bash
gcloud compute instances start sessionquant-paper --project=sessionquant --zone=us-central1-a
gcloud compute instances describe sessionquant-paper --project=sessionquant --zone=us-central1-a --format='value(networkInterfaces[0].accessConfigs[0].natIP)'
gcloud compute ssh sessionquant-paper --project=sessionquant --zone=us-central1-a --tunnel-through-iap
```

2. **운영 VM**에서 검사가 통과하고 main에 병합된 커밋의 `deploy/gcp/update_preopen.sh`를 별도 임시 파일로 내려받아 실행한다. 실행 인자는 해당 전체 커밋 SHA다. 릴리스 안내에 제공한 고정 주소와 SHA256 검증 명령을 사용한다. 실행 중 SSH 창은 열어 둔다. 이 스크립트는 미국 정규장 시작 전에만 동작한다.

업데이트는 알려진 복구 파일 변경만 허용하며 원본을 `/var/lib/sessionquant/update-backups/`에 보관한다. 다른 수정·준비된 Git 변경이 있으면 덮지 않고 중단한다. `.env`·서버 환경 파일·DB·앱 자료는 보존한다. 배포된 Git 체크아웃은 검토한 커밋으로 고정되며 기존 브랜치 이력은 남는다. 이후 업데이트도 같은 도구를 사용한다.

새 이미지 빌드 후 앱을 멈추고 새 Cloud Storage 백업을 만든다. DB 변경 로그 OFF, 전송 크기 64MiB 이상, 테이블 준비, 백업 성공, 토스 인증·AAPL 조회를 확인한 뒤 앱을 다시 실행한다. 실패하면 오류를 해결하고 재실행한다. 자동으로 매매 시작을 누르거나 초기 모의 장부를 만들지 않는다. 저장 공간 장애를 처음 복구하는 서버라면 [저장 공간 복구 안내](GCP_STORAGE_RECOVERY.md)가 먼저 필요하다.

3. **완료 출력**에서 `sessionquant-market-close.timer`와 `sessionquant-storage-guard.timer`가 활성 상태인지 확인한다. 일반 거래일 겨울 기준 다음 날 06:05부터 종료·백업하고 VM도 꺼진다. 실제 전원 종료까지는 몇 분 더 걸릴 수 있다.

4. 대시보드에서 예열 완료와 운영 종목 수를 확인하고 **매매 시작**을 직접 누른다. 장전에는 장중 시세·집중 종목·전략 판단이 대기일 수 있다. 개장 뒤 시장·종목에서 종목/SPY 비교 가격과 거래대금이 모이는지, 집중 제외 이유와 처리 속도 기록이 갱신되는지 본다. **ETF 관측 가격 표시만으로 분석 검증 완료를 판단하지 않는다.**

서버 안에서 연결 점검만 다시 할 때는 `sudo bash /opt/sessionquant/deploy/gcp/preopen_check.sh`를 사용한다. 이 점검은 DB를 읽고 토스 인증·현재가만 조회한다. 주문·유료 LLM 생성은 하지 않으며 뉴스 분석의 의미적 정확성이나 전략 체결을 보증하지 않는다. 실행 중인 엔진은 별도로 설정된 뉴스 수집·분석을 계속할 수 있다.

Cloud Shell에서 화면 터널은 별도 터미널에 연다.

```bash
gcloud compute ssh sessionquant-paper --project=sessionquant --zone=us-central1-a --tunnel-through-iap -- -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8501:127.0.0.1:8501
```

웹 미리보기 포트는 8501이다. 이미 사용 중이면 기존 터널을 이용한다. 미리보기 호스트가 바뀌었을 때만 `STREAMLIT_BROWSER_SERVER_ADDRESS`를 새 호스트와 맞춘다. 터널을 닫아도 VM의 엔진은 계속 실행된다.
# 장중 수집 개선 버전 업데이트

2026-11-18 개선 버전은 반복 시총 조회, 호가마다 전체 분봉 재저장, 참고 ETF 뒤에서 밀리는 분봉 순환을 줄인다. 모든 종목의 실시간성이나 매수 성립을 보장하지 않는다. 운영 결과는 시장·종목의 처리 속도 표와 종목별 계산 기준 시각으로 확인한다.

업데이트할 커밋이 main에 일반 병합된 뒤 해당 커밋의 `deploy/gcp/update_preopen.sh`를 내려받아 실행한다. 기본 실행은 장전만 허용한다. 장중은 대시보드에서 매매 종료가 완료되고 보유·미체결이 없는 경우에만 `--paused-paper`를 명시한다. 도구는 종료 상태를 빌드 전후와 프로그램 종료 후 다시 검증한다. 폐장까지 30분 미만이면 중단하며 자동 종료 예약을 해제하지 않는다.

```bash
sudo bash /tmp/sessionquant-update.sh <검토한_전체_커밋_SHA> --paused-paper
```

프로그램 중단 전에 이미지를 만들고, DB와 앱 자료의 Cloud Storage 백업 성공 후 코드·이미지를 변경한다. 완료 후 자동 매매는 대기 상태로 시작하므로 자료 준비와 기존 상태를 확인하고 매매 시작을 직접 누른다. 오류가 발생하면 DB·환경 파일은 보존되지만 프로그램이 중지된 상태일 수 있으므로 완료 문구 없이 재개됐다고 판단하지 않는다.

**복구 형식 v3 안내:** 새 버전은 v2 상태를 읽고 첫 저장에서 분봉/시세 메타데이터 분리 형식 v3로 전환한다. 구버전은 이를 거부한다. 코드만 구버전으로 바꾸지 말고, 롤백이 필요하면 업데이트 전 DB 백업과 해당 코드 버전을 함께 복원한다. 업데이트 뒤 생성된 모의거래 기록을 잃지 않도록 먼저 별도 백업한다.

모바일 앱을 전환하면 SSH가 끊길 수 있다. `tmux new-session -A -s sq-update` 안에서 실행하고 재접속 시 `tmux attach -t sq-update`로 기존 작업을 확인한다. 같은 업데이트를 중복 실행하지 않는다.

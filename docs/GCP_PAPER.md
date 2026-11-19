# GCP에서 켜고, 확인하고, 종료하기

GCP VM 한 대에서 앱과 MySQL을 실행합니다. **VM 시작, 프로그램 시작, 코드 업데이트, 대시보드 접속, 매매 시작은 서로 다른 동작**입니다. 이 안내의 거래는 모두 Paper 모의매매입니다.

현재 권장 설정은 `strategy-lab-websocket`입니다. 저장소에 코드가 있어도 운영 서버에 자동으로 배포되지 않습니다. 기존 서버의 설정을 확인하고 필요할 때 [장전 업데이트](#장전-업데이트)와 [웹소켓 전환](WEBSOCKET_ROLLOUT.md#gcp에서-전환)을 적용합니다. 처음 서버를 만드는 경우는 아래 [최초 설치](#고급-최초-설치)부터 진행하세요.

## 명령을 어디에서 실행하나요?

| 화면의 프롬프트 | 위치 | 할 일 |
| --- | --- | --- |
| `이름@cloudshell:~` | GCP Cloud Shell | VM 시작·상태 확인, SSH 접속, 브라우저용 터널 |
| `이름@sessionquant-paper:~` | 운영 VM 내부 | 프로그램 상태·로그 확인, 업데이트, 종료 |
| 로컬 PC의 PowerShell | 본인 PC | 로컬 실행 또는 PC용 SSH 터널 |

Cloud Shell과 운영 VM은 다른 컴퓨터입니다. `gcloud compute ssh ... -L ...`로 **Cloud Shell의 웹 미리보기**를 열 때는 `@cloudshell`에서 실행합니다. VM 안에서 자기 자신에게 같은 터널을 열지 않습니다. `tmux`는 터미널 작업을 유지하는 도구이며 프로그램의 실행 상태를 뜻하지 않습니다.

## 매일 켜고 접속하기

### 1. Cloud Shell에서 VM 시작

아래는 프로젝트 `sessionquant`, VM `sessionquant-paper`, 지역 `us-central1-a`를 쓰는 기존 구성입니다. 다른 프로젝트는 값을 바꿉니다.

```bash
gcloud compute instances start sessionquant-paper --project=sessionquant --zone=us-central1-a
gcloud compute instances describe sessionquant-paper --project=sessionquant --zone=us-central1-a --format='yaml(status,networkInterfaces[0].accessConfigs[0].natIP)'
```

외부 IP가 토스 API 허용 목록과 일치하는지 확인합니다. 임시 IP는 다시 켤 때 달라질 수 있습니다. 기존 IP를 예약한 고정 IP라면 유지되지만 별도 IP 요금 조건을 확인합니다.

VM 상태가 `RUNNING`이어도 SSH와 프로그램 준비는 조금 늦을 수 있습니다. 시작 직후 포트 22 연결 실패가 나면 잠시 뒤 SSH만 다시 시도하고, 계속 실패하면 상태·부팅 로그를 점검합니다. 반복 시작·강제 재설정부터 하지 않습니다.

### 2. VM 안에서 프로그램 확인

Cloud Shell에서 접속합니다.

```bash
gcloud compute ssh sessionquant-paper --project=sessionquant --zone=us-central1-a --tunnel-through-iap
```

프롬프트가 `@sessionquant-paper`로 바뀌면 아래를 실행합니다. 자동 시작을 설치해 둔 서버는 VM 부팅 때 앱과 DB를 시작하므로 매일 업데이트할 필요는 없습니다.

```bash
sudo systemctl show sessionquant.service -p ActiveState -p SubState -p Result
sudo docker compose -f /opt/sessionquant/deploy/gcp/compose.yaml ps -a
sudo systemctl list-timers --all 'sessionquant-*' --no-pager
```

`app`, `db`가 `healthy`인지, 장 마감·저장 공간·비용 방지 타이머가 있는지 봅니다. 이 systemd 서비스는 컨테이너를 시작한 뒤 끝나는 형식이므로 **`active (exited)`는 정상일 수 있습니다.** 앱의 `healthy`는 화면 서버 응답이며 자료 준비·매수 허가와는 별개입니다.

시작에 실패했다면 다음 읽기 전용 출력으로 원인을 확인합니다. 백업·디스크 차단을 지우거나 긴 업데이트 전체를 바로 반복하지 않습니다.

```bash
sudo journalctl -b -u sessionquant.service --no-pager -n 80
sudo docker compose -f /opt/sessionquant/deploy/gcp/compose.yaml logs --tail=100 app
```

### 3. 별도 Cloud Shell 터미널에서 대시보드 열기

새 Cloud Shell 탭의 프롬프트가 **`@cloudshell`**인지 확인한 뒤 실행합니다.

```bash
gcloud compute ssh sessionquant-paper --project=sessionquant --zone=us-central1-a --tunnel-through-iap -- -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8501:127.0.0.1:8501
```

출력 없이 기다리는 상태가 정상입니다. Cloud Shell 상단 **웹 미리보기 → 포트 변경 → 8501**로 엽니다. PC에 Google Cloud CLI를 설치해 같은 명령을 실행했다면 PC 브라우저에서 `http://localhost:8501`을 사용합니다.

휴대폰은 같은 Google 계정으로 인증된 미리보기 링크를 이용할 수 있지만, Cloud Shell 세션·터널이 끝나면 링크도 끊깁니다. 인터넷 공개 대시보드가 아닙니다. 휴대폰 앱 전환이나 PC 종료로 터널이 끊겨도 **VM의 systemd/Docker 앱은 별도로 계속 실행**됩니다. 서버가 실제로 종료됐는지는 VM 상태로 확인합니다.

### 4. 화면에서 매매 시작

메인에서 `모의매매`, 시세 시각, 집중 종목·자료 상태를 확인합니다. **매매 시작**을 열고 확인란을 선택해야 신규 진입 평가를 허용합니다. 자동으로 켜지는 VM/앱과 자동매매 허용은 별개입니다. 자세한 확인 순서는 [Paper 운영](REALTIME_PAPER.md)을 따릅니다.

## 대시보드가 안 열릴 때

| 표시 | 의미와 다음 확인 |
| --- | --- |
| `Unable to forward ... port 8501` | Cloud Shell에서 화면 서버까지 연결되지 않음. VM 앱 상태와 터널을 각각 확인 |
| `channel ... Connection refused` | 터널의 목적지 VM에서 8501에 응답하는 서버가 아직 없거나 종료됨. 앱 로그 확인 |
| `Address already in use` | Cloud Shell의 8501을 기존 터널/프로세스가 사용 중. 기존 터널 상태를 확인하며 앱을 재설치하지 않음 |
| `active account` 없음 | Cloud Shell 인증 문제. 콘솔 로그인·Cloud Shell 인증을 복구한 뒤 접속 |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | 접속 대상의 키가 바뀜. 복구·교체 이력과 GCP 측 지문을 확인한 후 해당 항목만 갱신 |
| `Broken pipe` | SSH 연결 종료. VM 종료 알림 직후라면 예상 결과이며, 그 외에는 서버 상태를 확인 |

앱은 DB·장부 복원 뒤 HTTP 포트를 열 수 있습니다. 최초 3분의 실패 유예, DB 포함 최대 8분의 Compose 준비 확인, 바깥 systemd 10분 제한이 있습니다. 매번 이 시간을 모두 기다리는 것은 아닙니다. 앱이 종료되거나 제한을 넘기면 로그를 확인하고, 포트 오류만으로 서버가 느리다고 단정하지 않습니다.

화면 내부에 DB 오류가 뜨면 터널은 연결된 상태일 수 있습니다. `MySQL 2013` 등 실제 오류와 `dashboard_read_failed`의 조회 항목·시간을 확인합니다. [오류별 진단](REALTIME_PAPER.md#문제별로-먼저-볼-것)을 참고하세요.

## 장전 업데이트

**CI는 구현되어 있고 CD 자동 배포는 구현되지 않았습니다.** GitHub의 `SessionQuant 회귀 검사`가 push·PR에서 정적 검사와 오프라인 테스트를 실행합니다. 선택적 MySQL 검사는 수동 실행입니다. 병합만 해서는 GCP 코드·이미지가 바뀌지 않습니다.

업데이트는 검사가 통과하고 `main`에 포함된 **검토한 전체 커밋 SHA**로 고정합니다. 정규장 시작 전, 매매 시작을 누르기 전에 하며 백업·빌드 시간을 넉넉히 둡니다. 백업이 커지면 20분 이상 걸릴 수도 있습니다. 현재 커밋·설정은 운영 VM에서 확인합니다.

```bash
sudo git -C /opt/sessionquant log -1 --oneline
sudo grep -E '^(SQ_SESSION_CONFIG|SQ_DB_NAMESPACE)=' /etc/sessionquant/app.env
```

휴대폰 연결이 끊겨도 작업이 남도록 **운영 VM 안에서** `tmux`를 사용합니다. 설치돼 있지 않으면 `sudo apt-get install -y tmux`로 준비합니다.

```bash
tmux new-session -A -s sq-update
```

이미 tmux 안이면 다시 만들지 않습니다. 재접속 후 `tmux attach -t sq-update`로 진행 결과를 확인합니다. 화면에 `(END)`가 나오면 `q`로 조회 화면만 닫습니다. 다른 터미널에서 같은 업데이트를 중복 실행하지 않습니다.

검토한 버전의 `update_preopen.sh`와 그 파일의 SHA256을 준비한 뒤 다음 템플릿의 두 값을 바꿉니다. 예시 문자열 그대로면 실행 전에 중단합니다. `main`의 최신 파일을 확인 없이 받아 바로 실행하지 않습니다.

현재 실험 프로필의 보유 한도는 3종목입니다. 이전 2종목 설정의 장부는 업데이트 과정에서 프로그램 종료와 DB 백업을 마친 뒤 전환합니다. 보유·미체결·청산·처리 중인 화면 요청이 있으면 전환하지 않습니다. 잔액·체결·신호 기록을 보존하고 보유 수 설정만 변경하며, 다른 위험·비용 설정까지 달라졌으면 중단합니다. 오늘의 거래가 정리된 뒤 다음 장전에 업데이트하면 됩니다. 이 전환 후 이전 2종목 설정으로 단순 재시작하면 설정 불일치로 중단되므로, 롤백할 때는 함께 저장된 기존 장부 백업도 검토해야 합니다.

```bash
bash <<'BASH'
set -Eeuo pipefail
SQ_REV='검토한_전체_커밋_SHA_40자리'
SQ_SCRIPT_SHA256='해당_스크립트의_SHA256_64자리'
[[ "$SQ_REV" =~ ^[0-9a-f]{40}$ && "$SQ_SCRIPT_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
  echo '검토한 커밋과 스크립트 해시를 먼저 지정하세요.' >&2
  exit 1
}
SQ_SCRIPT=$(mktemp /tmp/sq-update.XXXXXXXX)
trap 'rm -f -- "$SQ_SCRIPT"' EXIT
curl -fsSL "https://raw.githubusercontent.com/taegyoo1104/pennyquant/$SQ_REV/deploy/gcp/update_preopen.sh" -o "$SQ_SCRIPT"
printf '%s  %s\n' "$SQ_SCRIPT_SHA256" "$SQ_SCRIPT" | sha256sum --check
sudo bash "$SQ_SCRIPT" "$SQ_REV" < /dev/null
BASH
```

스크립트는 변경 파일·저장 공간·시간·종료 작업 여부를 확인하고 새 이미지를 먼저 만듭니다. 앱을 멈춘 뒤 새 Cloud Storage 백업에 성공해야 코드·이미지를 적용합니다. 알려진 복구 수정은 `/var/lib/sessionquant/update-backups/`에 보존하고, 다른 로컬 변경은 덮지 않고 중단합니다. DB·앱 자료·환경 파일을 초기화하지 않습니다.

DB 변경 로그 OFF, 전송 크기 64MiB 이상, 테이블·백업 준비, 토스 인증·AAPL 조회를 확인하고 서비스를 다시 시작합니다. 완료 출력의 컨테이너·타이머와 대시보드를 확인한 뒤 **매매 시작을 직접** 누릅니다. 중간 실패 시 DB·환경 파일은 남지만 앱은 중지돼 있을 수 있습니다. 마지막 성공 단계와 오류를 먼저 보고 재개하며, 이미 성공한 긴 백업을 무조건 다시 실행하지 않습니다.

이 도구는 현재 선택한 프로필을 유지합니다. 이전 REST 프로필에서 웹소켓으로 처음 바꾼다면 업데이트 완료 후 [전환 스크립트](WEBSOCKET_ROLLOUT.md#gcp에서-전환)를 별도로 실행합니다.

### 고급: 장중 업데이트와 롤백

장중은 대시보드의 종료 요청이 완료되고 보유·미체결·제어 요청이 없는 경우에만, 검증한 동일 스크립트에 `--paused-paper`를 명시합니다. 빌드 전후와 앱 종료 후 장부를 다시 확인하며 폐장까지 30분 미만이면 거부합니다. 자동 종료 타이머는 유지합니다.

```bash
sudo bash /tmp/sessionquant-update.sh "$SQ_REV" --paused-paper < /dev/null
```

이 예시는 앞서 다운로드·해시 검증한 파일을 `/tmp/sessionquant-update.sh`에 보관하고, 현재 셸의 `SQ_REV`에 검토한 전체 커밋 SHA를 지정한 경우입니다. 파일과 커밋이 일치해야 합니다. 실행 중인 엔진에 별도 연결 검사로 새 토스 토큰을 발급하지 않습니다. `preopen_check.sh`는 업데이트처럼 앱을 멈춘 점검 절차에서 사용합니다.

복구 형식 v3는 v2를 읽고 첫 저장에서 분봉/시세 메타데이터를 분리합니다. 구버전은 v3를 거부합니다. 코드만 되돌리지 말고 업데이트 전 DB 백업과 대응 코드 버전을 함께 **별도 복구 환경에서** 확인합니다. 업데이트 뒤 생긴 기록도 먼저 별도 백업합니다. `reset --hard`, DB 디렉터리 삭제, `docker compose down -v`로 문제를 덮지 않습니다.

## 종료와 다음 날 백업 확인

일반 거래일에는 거래소 폐장 5분 후부터 **앱 저장·중지 → 백업 → 오래된 자료 정리 → DB·VM 종료**를 실행합니다. 한국시간 겨울에는 다음 날 06:05, 서머타임에는 05:05부터이며 조기 폐장은 달력을 따릅니다. 백업 시간만큼 실제 VM 종료는 늦어집니다.

- 휴장일 또는 뉴욕 현지 날짜의 종료 시각 이후 켜면 새 수집 없이 1분 후 종료합니다. 한국시간 오후라도 뉴욕 날짜가 아직 전날이면 이 조건에 해당할 수 있습니다.
- 달력 확인 실패는 새 거래를 시작하지 않고 15분 후 종료합니다. 비용 방지 타이머는 부팅 20시간 후 종료하며 월 비용 상한은 아닙니다.
- 앱 오류 후에도 장 마감 타이머는 남습니다. 자동 종료는 전량 체결 보장이 아니며 잔량을 저장합니다.
- 종료 백업은 최대 30분, 바깥 종료 서비스는 40분을 허용합니다. 실패하면 원본 DB·완료 백업을 보존하고 종료하며, 다음 앱 시작은 백업 성공 확인 전까지 차단합니다.

장중에 수동으로 종료하려면 대시보드에서 청산과 잔량을 확인한 뒤 **운영 VM에서** 실행합니다. 이 명령은 프로그램만 끄는 것이 아니라 백업 후 VM 전원도 끕니다.

```bash
sudo bash /opt/sessionquant/deploy/gcp/stop.sh
```

종료 알림 뒤 SSH가 `Broken pipe`로 끝나는 것은 예상할 수 있습니다. **Cloud Shell**로 돌아와 다음을 확인합니다.

```bash
gcloud compute instances describe sessionquant-paper --project=sessionquant --zone=us-central1-a --format='yaml(status,lastStartTimestamp,lastStopTimestamp)'
gcloud storage ls -l 'gs://sessionquant-sq-backups/daily/' --project=sessionquant
```

`TERMINATED`는 VM 중지입니다. 새 백업 파일이 해당 종료 시각에 생성됐는지는 따로 봅니다. 날짜·파일명의 `Z`는 UTC이며 한국시간은 9시간을 더합니다. 종료 전 VM 또는 다음 정상 점검에서 `/etc/sessionquant/backup-status`의 `UPLOADED` 시각·파일명이 버킷과 일치하는지도 확인합니다. VM이 꺼졌다는 것만으로 백업 성공을 단정하지 않습니다.

## 구성과 비용

| 항목 | 이 안내의 구성 |
| --- | --- |
| VM | `e2-standard-2` · 2 vCPU·8GB · Ubuntu 24.04 · `us-central1-a` |
| DB | 같은 VM의 MySQL 8.4, 외부 포트 차단. Cloud SQL 사용 안 함 |
| 디스크 | 40GB 표준 영구 디스크. 중지 후에도 보존 |
| 화면 | IAP/SSH 터널의 8501. 인터넷 공개 포트 없음 |
| 백업 | SQL+앱 자료, 로컬 최근 3개·비공개 Cloud Storage 7일 |

초기 2GB·20GB 구성에서 CPU 포화와 저장 공간 부족을 겪어 위 기준으로 확장했습니다. 더 큰 VM이 모든 API·DB 지연을 해결하지는 않으므로 처리 시간을 보고 판단합니다. 중지하면 VM 계산 요금은 멈추지만 디스크·백업·예약 IP 등 남는 자원의 비용은 확인해야 합니다. 무료 크레딧의 잔액·만료일은 결제 화면에서 확인하며 OpenAI 요금은 GCP 크레딧과 별개입니다.

새 설치 명령은 Cloud SQL·Cloud NAT·로드밸런서·Scheduler를 만들지 않습니다. 외부 IP를 고정할지는 별도 운영 선택입니다. [VM 중지/시작](https://cloud.google.com/compute/docs/instances/stop-start-instance), [무료 사용량](https://cloud.google.com/free/docs/free-cloud-features)을 참고하세요.

## 고급: 최초 설치

다음은 새 자원을 만드는 절차입니다. 이미 운영 서버가 있다면 반복하지 않습니다. 프로젝트 결제 계정과 예상 비용을 확인한 뒤 진행합니다.

### Cloud Shell: 프로젝트와 VM 생성

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

### 운영 VM: 설치와 비밀값 입력

SSH로 들어간 **서버 안에서** 실행한다. 새 Ubuntu 서버에는 Git이 없을 수 있으므로 먼저 설치한다. 프로젝트 ID는 GCE 메타데이터에서 읽고, 생성 단계의 기본 이름으로 만든 백업 버킷을 사용한다. 버킷 이름을 직접 다르게 정했다면 마지막 인자를 해당 이름으로 바꾼다.

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates tmux
sudo git clone --branch main https://github.com/taegyoo1104/pennyquant.git /opt/sessionquant
cd /opt/sessionquant
SQ_PROJECT_ID="$(curl -fsS -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/project/project-id)"
test -n "$SQ_PROJECT_ID" && sudo bash deploy/gcp/install.sh "${SQ_PROJECT_ID}-sq-backups"
sudo nano /etc/sessionquant/app.env
```

설치가 생성한 `SQ_MYSQL_*` 값은 유지하고, PC에서 사용하던 `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`, `TOSS_ACCOUNT_SEQ`, `OPENAI_API_KEY`, `SEC_USER_AGENT` 및 사용하는 뉴스 키를 입력한다. 값은 한 줄로 입력한다. 이 파일은 관리자만 읽을 수 있다. PC의 `.env`를 통째로 덮어써서 서버 DB 비밀번호를 바꾸지 않는다.

설치 스크립트는 기존 비밀번호·DB를 초기화하지 않는다. 이 단계에서는 자동 실행·종료를 아직 활성화하지 않으므로 키 입력·자료 이전을 마칠 수 있다. 서버가 켜진 동안에는 사용 시간이 누적된다.

**기존 DB를 가져오지 않는 완전히 새 설치만** 첫 앱 실행 전에 `sudo nano /etc/sessionquant/app.env`에서 생성된 `SQ_DB_NAMESPACE` 한 줄을 바꾸고 `SQ_SESSION_CONFIG`를 추가합니다. 같은 키를 중복으로 추가하지 않습니다.

```dotenv
SQ_SESSION_CONFIG=config/sessionquant.strategy-lab-websocket.example.yaml
SQ_DB_NAMESPACE=sessionquant-strategy-lab-ws-v1
```

기존 DB를 이전한다면 먼저 아래 이전 절차에서 **원래 프로필·namespace를 유지**해 검증합니다. 장부가 확인되기 전에 새 이름으로 바꾸지 않습니다. 설치 스크립트 자체의 기본값은 아직 이전 `realtime-paper`입니다.

앱 자료 디렉터리에 S&P 500 목록이 없다면 앱을 시작하기 전에 운영 VM에서 준비합니다. 이미 있는 목록은 이 절차로 덮어쓰지 않습니다.

```bash
if ! sudo test -e /var/lib/sessionquant/app/sp500-universe.json; then
  sudo docker compose -f /opt/sessionquant/deploy/gcp/compose.yaml run --rm --no-deps -T app \
    python -m sessionquant universe-refresh --output data/sp500-universe.json
fi
```

설치·설정·필요한 자료 이전을 마친 **거래일 장전**에 운영 VM에서 자동 시작과 종료 보호를 활성화합니다.

```bash
sudo systemctl enable sessionquant.service sessionquant-cost-guard.timer
sudo systemctl start sessionquant-cost-guard.timer
sudo systemctl start sessionquant.service
```

이후 매일은 위의 VM 시작 절차를 사용합니다. 기존 서버의 프로필 전환은 이 최초 설정 편집을 반복하지 말고 전용 전환 스크립트를 사용합니다.

## 고급: 기존 DB 이전

아래는 이전 `realtime-paper` 원장의 이전 예시입니다. 웹소켓 원장을 이전할 때는 조회 명령의 `--config`와 서버의 두 프로필 설정을 원본과 같게 맞춥니다.

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

`history-status` 결과가 PC의 준비 종목과 맞는지, 백업이 버킷에 실제로 생겼는지 확인한다. 이 이전 예시의 실행 공간은 `paper_local_sessionquant-realtime-paper-v1`이다. 웹소켓 원장은 `paper_local_sessionquant-strategy-lab-ws-v1`이다. PC에서 다른 `SQ_DB_NAMESPACE`를 사용했다면 서버에도 동일한 실행 공간을 지정해야 한다. `prepare-paper`를 다시 실행하지 않는다. 분봉 재수집 대신 기존 SQL의 분봉·시총·모의 장부·뉴스 원장을 그대로 이전한다.

`check-connections`는 GCP에서 실제 API의 읽기 연결을 확인하지만 주문·유료 AI 생성은 하지 않는다. 토스·DB와 사용하는 뉴스 공급자 연결을 확인한 뒤 진행한다. 이전과 백업 확인 후 평문 SQL 전송 파일은 PC와 서버에서 삭제하고 비공개 백업만 보관한다. PC의 원본 DB는 이전 검증이 끝날 때까지 보존한다.

## 고급: 오래된 자료 정리

권장 웹소켓 및 두 REST Paper 예제 설정은 `retention.enabled: true`다. **프로그램 저장·중지 → 새 Cloud Storage 백업 업로드 성공 → 오래된 자료 정리 → DB·VM 종료** 순서로 실행한다. 업데이트용 백업이나 로컬 프로그램 종료에는 자동 정리를 붙이지 않는다. 이전 백업의 성공 기록만 남아 있거나 이번 백업이 실패하면 정리하지 않는다. 적용은 새 코드·이미지를 배포한 뒤 시작된다.

| 자료 | 기본 보관 기준 |
| --- | --- |
| 뉴스 수신·본문 추출 기록, 시세 관측, 시장 상태, 반복 집중·후보 선정 표, 시스템 로그 | 7일 |
| 시황 계산 스냅샷, LLM에 보내기 전 건너뛴 시황 분석 | 7일 |
| 운영용 기업·거시 뉴스와 분석 결과 | 30일. 매매 신호가 참조한 기사·분석은 보존 |
| 주문·체결·보유·손익·매매 신호·주문 근거인 후보 선정 표 | 자동 삭제하지 않음 |
| RVOL용 과거 분봉과 원본 분봉, 전략 재생용 입력·판단·뉴스 근거 | 자동 삭제하지 않음 |
| AI 호출·비용, 성공한 시황 LLM 분석, 재분석 방지 캐시, 현재 상태·복구·달력 | 자동 삭제하지 않음 |

기간은 달력일 기준이다. 수신 시각뿐 아니라 DB 생성·수정 시각도 기간을 지나야 하므로, 오래된 날짜의 자료를 오늘 보충했다고 바로 삭제하지 않는다. 시각이 불명확하거나 아직 유효한 자료도 보존한다. 연구 원장에 중복 저장된 수신·추출·시황 계산 스냅샷만 같은 7일 기준으로 정리하며, 재생에 쓰이는 뉴스 원문·분석 사본과 엔진 판단 기록은 남긴다. 예전 전략을 다시 검증할 근거까지 지우는 정책은 아니다.

한 번에 최대 50,000행을 검사하고 계산 예산은 90초다. 각 묶음은 최대 200행이며, 진행 위치를 저장해 다음 종료 때 이어서 확인한다. `PARTIAL`은 예산 안에서 일부만 검사했다는 뜻이다. 컨테이너 실행 전체는 120초와 강제 종료 유예 15초로 제한하며, 바깥 종료 서비스의 40분 안에 포함된다. 정리 오류는 백업 성공과 별도로 기록하고 VM 종료를 계속한다. 정리 도중 터널이 끊겨도 서버의 종료 작업은 계속된다.

서버 안에서 삭제 없는 미리보기를 실행할 수 있다. 검사 범위와 시간 제한이 적용되므로 `eligible`은 **이번에 검사한 범위의 대상 수**이며 DB 전체 합계가 아니다. 외부 API·LLM·주문 호출은 없다.

```bash
cd /opt/sessionquant
sudo docker compose -f deploy/gcp/compose.yaml run --rm --no-deps -T app \
  sh -c 'exec python -m sessionquant data-retention --config "${SQ_SESSION_CONFIG:-config/sessionquant.realtime-paper.example.yaml}"'
```

다음 기동 때 최근 자동 정리 결과를 확인한다. 명령은 **운영 VM 안에서** 실행한다.

```bash
sudo cat /etc/sessionquant/retention-status
sudo cat /var/lib/sessionquant/app/retention-last.json
sudo journalctl -u sessionquant-market-close.service --no-pager -n 60
```

`COMPLETE`는 이번 검사 완료, `PARTIAL`은 이어서 검사할 자료 있음, `DISABLED`는 설정에서 해제, `FAILED`는 정리 오류다. 실패 시 JSON 파일은 이전 정상 결과일 수 있으므로 상태 파일의 시각도 함께 본다. 백업 자체 실패로 정리를 호출하지 않은 날에는 정리 상태 파일도 이전 값이다. 실제 삭제는 엔진 잠금과 최근 백업 성공 기록을 확인하며 운영 중인 엔진과 동시에 실행할 수 없다.

기본 보관 기간을 지나는 자료부터 적용하므로 새로 쌓인 자료가 당일 없어지지는 않는다. MySQL의 `DELETE`는 내부 공간을 재사용하게 하며 `.ibd` 파일을 즉시 축소하지 않을 수 있다. 자동 `OPTIMIZE TABLE`은 수행하지 않는다. 전략 재생·분봉·비용 근거는 계속 쌓일 수 있으므로 저장 공간 경보를 함께 유지한다. 정리 전 백업에도 이전 자료가 남고, 별도 정책에 따라 서버 최근 3개·Cloud Storage 7일 보관된다.

## 고급: 백업 실패와 복구

`/etc/sessionquant/backup-status`의 `UPLOADED` 시각·파일명과 버킷의 객체가 일치해야 업로드 완료입니다. 실패하면 앱 시작을 차단하고 원본 디스크를 유지합니다. 로그의 `phase`와 단계별 소요 시간을 확인하세요. 실패 표시를 지워 앱만 다시 켜지 않습니다. 원인을 해결한 뒤 DB가 실행 중인 점검 상태에서 `backup.sh`를 성공시켜야 합니다.

새 백업은 압축 SQL `database.sql.gz`와 `app/`을 담은 `tar.gz`입니다. 옛 백업의 `database.sql`도 가져오기 도구가 지원합니다. 바깥 `tar.gz`는 압축률 0으로 묶어 이미 압축한 SQL을 다시 압축하는 시간을 줄입니다. 30분 제한은 모든 크기의 백업 완료를 보장하지 않습니다.

복구는 자동 실행을 끈 **별도의 빈 서버·DB**에서 합니다. 관리자 계정으로 비공개 버킷에서 내려받아 새 임시 폴더에 풀고, 위 DB 이전 절차로 SQL을 가져옵니다. `app/`은 `/var/lib/sessionquant/app`에 복원하고 소유자를 `1000:1000`으로 맞춥니다. 장부·분봉·뉴스·프로필과 대응 코드 버전을 확인한 뒤 운영 전환을 판단합니다. 운영 DB 위에 덮어쓰거나 기존 디스크를 삭제하지 않습니다.

백업 실패 단계에 따라 새 로컬 완료 파일이 없을 수 있습니다. 묶기까지 끝났다면 업로드에 실패해도 로컬 파일은 남지만, 그 이전에 실패했다면 완료 백업이 생성되지 않습니다. 기존 완료 백업과 원본 DB는 별도로 보존합니다.

VM을 삭제해도 보존한 디스크·스냅샷·버킷·예약 IP는 남을 수 있습니다. 복구 자원을 정리할 때는 현재 연결 관계와 필요한 백업을 먼저 확인합니다. 과거 장애와 복구 근거는 [저장 공간 복구 기록](GCP_STORAGE_RECOVERY.md), 특히 [백업 시간 초과 기록](GCP_STORAGE_RECOVERY.md#2026-11-18-백업-시간-초과)을 참고하세요.

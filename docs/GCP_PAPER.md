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

`app`, `db`가 `healthy`인지, 장 마감·이번 프로그램 실행 최대 20시간·저장 공간 타이머가 있는지 봅니다. 이 systemd 서비스는 컨테이너를 시작한 뒤 끝나는 형식이므로 **`active (exited)`는 정상일 수 있습니다.** 앱의 `healthy`는 화면 서버 응답이며 자료 준비·매수 허가와는 별개입니다.

시작에 실패했다면 다음 읽기 전용 출력으로 원인을 확인합니다. 백업·디스크 차단을 지우거나 긴 업데이트 전체를 바로 반복하지 않습니다.

```bash
sudo journalctl -b -u sessionquant.service --no-pager -n 80
sudo docker compose -f /opt/sessionquant/deploy/gcp/compose.yaml logs --tail=100 app
```

### 3. 별도 Cloud Shell 터미널에서 대시보드 열기

Cloud Shell에 검토한 접속 도우미를 한 번 준비한 뒤에는 아래 명령 하나로 접속합니다. 프롬프트는 **`@cloudshell`**이어야 합니다. VM 안에서 실행하면 올바른 위치를 안내하고 중단합니다.

```bash
python3 "$HOME/sessionquant-dashboard.py"
```

도우미는 활성 로그인 계정, 고정된 `sessionquant / us-central1-a / sessionquant-paper`의 실행 상태, VM의 화면 HTTP 응답을 확인합니다. 부팅 직후 SSH·화면 서버가 준비 중이면 제한 시간 안에서 기다립니다. 화면 준비 대기는 기본 8분이며 이후 SSH 터널 준비도 별도 제한 시간이 있습니다. VM·프로그램·매매를 시작하거나 재시작하는 명령은 보내지 않습니다.

8501이 이미 사용 중이면 **Cloud Shell에서만** 8502~8510 중 비어 있는 포트를 사용합니다. VM의 화면 포트는 항상 8501입니다. 기존 포트를 쓰는 프로세스를 종료하거나 임의의 기존 터널을 재사용하지 않습니다. 자신의 SSH 연결과 포트 전달 성공, 실제 HTTP 응답을 모두 확인한 뒤 `대시보드 접속 준비 완료`와 정확한 미리보기 포트를 표시합니다. **Cloud Shell 상단 웹 미리보기 → 포트 변경 → 출력된 번호**로 여세요. 필요하면 `--port 8503`처럼 포트를 직접 지정할 수 있습니다.

접속 도우미를 실행한 터미널은 유지합니다. **Ctrl+C는 이 도우미가 연 접속만 닫으며 VM의 프로그램·매매는 계속 실행됩니다.** 같은 도우미를 여러 터미널에서 동시에 실행하면 각각 다른 접속이 생길 수 있으므로 정상적으로 열린 미리보기는 그대로 사용하세요. 접속이 끊기면 도우미를 다시 실행합니다.

<details>
<summary>최초 한 번: 검토한 버전의 접속 도우미 준비</summary>

검토한 커밋의 전체 SHA와 그 커밋에 있는 `deploy/gcp/dashboard_access.py`의 SHA256을 준비합니다. 아래 두 예시 문자열을 실제 값으로 바꾼 뒤 **Cloud Shell**에서 실행합니다. 최신 `main` 파일을 검증 없이 바로 실행하지 않습니다.

```bash
bash <<'BASH'
set -Eeuo pipefail
umask 077
SQ_REV=검토한_40자리_커밋_SHA
SQ_ACCESS_SHA256=확인한_64자리_파일_SHA256
[[ "$SQ_REV" =~ ^[0-9a-f]{40}$ && "$SQ_ACCESS_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
  echo '검토한 커밋 SHA와 접속 도우미 SHA256을 입력하세요.' >&2
  exit 1
}
SQ_SCRIPT=$(mktemp)
trap 'rm -f -- "$SQ_SCRIPT"' EXIT
curl -fsSL "https://raw.githubusercontent.com/taegyoo1104/pennyquant/$SQ_REV/deploy/gcp/dashboard_access.py" -o "$SQ_SCRIPT"
printf '%s  %s\n' "$SQ_ACCESS_SHA256" "$SQ_SCRIPT" | sha256sum --check
install -m 700 "$SQ_SCRIPT" "$HOME/sessionquant-dashboard.py"
echo '준비 완료. python3 "$HOME/sessionquant-dashboard.py" 로 접속하세요.'
BASH
```

Cloud Shell 홈에 저장하므로 일반적인 터미널 재접속 때 다시 다운로드할 필요가 없습니다. 도우미를 업데이트할 때는 새로 검토한 SHA·해시로 같은 절차를 실행합니다.

</details>

로컬 PC에서 접속하려면 기존 수동 SSH 방식으로 자신의 PC에 터널을 열고 `http://localhost:8501`로 접속합니다. Cloud Shell용 도우미는 로컬 PC에서 실행하지 않습니다.

```bash
gcloud compute ssh sessionquant-paper --project=sessionquant --zone=us-central1-a --tunnel-through-iap -- -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8501:127.0.0.1:8501
```

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

### MySQL 1038로 시작이 중단된 경우

2026-11-19에는 코드·이미지 교체와 Cloud Storage 백업이 끝난 뒤 앱이 6초 만에 `MySQL 1038 · SELECT`로 종료됐다. 1038은 조회 결과의 정렬 작업 메모리 부족이다. 어떤 테이블·실행 계획이 실패했는지는 해당 로그만으로 확정할 수 없다. 상태 검사 대기 시간을 늘려 해결할 오류가 아니다.

공통 기록 조회는 `(namespace, seq)` 인덱스로 순번대로 읽도록 지정해 큰 JSON 내용을 별도로 정렬하는 경로를 피한다. 실제 MySQL에서 큰 JSON·작은 정렬 버퍼로 1038을 재현하고 인덱스 조회의 결과·순서·실행 계획을 검사한다. DB 서버의 다른 정렬 작업까지 모두 해결한다는 의미는 아니다.

기존 이미지의 복구가 먼저 필요하면, **8GiB 운영 VM·최대 DB 연결 30개 설정**에서 아래 명령으로 정렬 버퍼가 8MiB 미만일 때만 8MiB로 올릴 수 있다. 운영 VM에서 실행한다. 새 DB 연결부터 적용되며 `SET PERSIST`가 DB 데이터 디렉터리에 저장하므로 DB 재시작 뒤에도 유지된다. 이미 8MiB 이상인데 실패하면 추가 증설 대신 실패한 조회·실행 계획을 확인한다. 비밀번호·SQL 응답 원문을 공유하지 않는다.

```bash
sudo docker compose -f /opt/sessionquant/deploy/gcp/compose.yaml exec -T db sh <<'SH'
set -eu
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
SQ_SORT_BYTES=$(mysql -u root -N -B -e 'SELECT @@GLOBAL.sort_buffer_size')
case "$SQ_SORT_BYTES" in
  ''|*[!0-9]*) echo '정렬 버퍼 설정을 읽지 못했습니다.' >&2; exit 1 ;;
esac
if [ "$SQ_SORT_BYTES" -lt 8388608 ]; then
  mysql -u root -e 'SET PERSIST sort_buffer_size = 8388608'
fi
mysql -u root -e 'SELECT @@GLOBAL.sort_buffer_size / 1024 / 1024 AS sort_buffer_MiB'
SH
```

전체 업데이트를 반복하면 백업도 다시 실행된다. 코드 교체가 완료됐고 웹소켓 전환만 남았다면 위 설정이 성공한 뒤 [웹소켓 전환 절차](WEBSOCKET_ROLLOUT.md)의 `activate_websocket.sh`부터 이어갈 수 있다. 전환 스크립트는 기존 장부의 보유·미체결 없음과 새 백업 성공을 별도로 확인한다. 앞선 백업을 자동으로 재사용하지 않으며, 종료·시작 실패를 설정 파일 덮어쓰기로 우회하지 않는다.

## 장전 업데이트

**CI는 구현되어 있고 CD 자동 배포는 구현되지 않았습니다.** GitHub의 `SessionQuant 회귀 검사`가 push·PR에서 정적 검사·오프라인 테스트 후 폐기 가능한 MySQL 통합 검사까지 자동 실행합니다. 수동 실행에서는 MySQL 검사 여부를 선택할 수 있습니다. 병합만 해서는 GCP 코드·이미지가 바뀌지 않습니다.

업데이트는 검사가 통과하고 `main`에 포함된 **검토한 전체 커밋 SHA**로 고정합니다. 정규장 시작 전, 매매 시작을 누르기 전에 하며 백업·빌드 시간을 넉넉히 둡니다. 백업이 커지면 20분 이상 걸릴 수도 있습니다. 현재 커밋·설정은 운영 VM에서 확인합니다.

업데이트 뒤에는 대시보드 **시스템·설정 → 실행 중인 프로그램**에서 `실행 코드`를 확인합니다. 이 값은 화면 서버가 실행 중인 이미지에 빌드할 때 넣은 커밋이며, VM 작업 폴더의 현재 Git 커밋을 읽은 값이 아닙니다. 정상 장전 업데이트는 검토한 커밋의 압축 소스로 이미지를 만들어 그 커밋을 표시합니다. `이미지 생성 시각`은 한국시간으로 표시하며 배포 완료 시각은 아닙니다.

초기 설치·복구는 수정된 작업 폴더로 빌드할 수 있으므로 `작업 폴더로 만든 이미지 · 수정 포함 가능`과 참고용 `빌드 기준 커밋`을 표시합니다. 이 경우 기준 커밋과 실행 파일이 완전히 같다고 판단하지 않습니다. 기존 이미지·일반 Docker 빌드·잘못된 버전 파일은 `미확인`으로 표시하며, GitHub 최신 여부를 자동으로 판정하거나 DB의 과거 버전 기록을 현재 버전으로 대신 표시하지 않습니다.

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

장중은 대시보드의 종료 요청이 완료되고 보유·미체결·제어 요청이 없는 경우에만, 검증한 동일 스크립트에 `--paused-paper`를 명시합니다. 빌드 전후와 앱 종료 후 장부를 다시 확인하며 폐장까지 30분 미만이면 거부합니다. 프로그램 종료 예약은 유지하며 VM은 자동으로 끄지 않습니다.

```bash
sudo bash /tmp/sessionquant-update.sh "$SQ_REV" --paused-paper < /dev/null
```

이 예시는 앞서 다운로드·해시 검증한 파일을 `/tmp/sessionquant-update.sh`에 보관하고, 현재 셸의 `SQ_REV`에 검토한 전체 커밋 SHA를 지정한 경우입니다. 파일과 커밋이 일치해야 합니다. 실행 중인 엔진에 별도 연결 검사로 새 토스 토큰을 발급하지 않습니다. `preopen_check.sh`는 업데이트처럼 앱을 멈춘 점검 절차에서 사용합니다.

복구 형식 v3는 v2를 읽고 첫 저장에서 분봉/시세 메타데이터를 분리합니다. 구버전은 v3를 거부합니다. 코드만 되돌리지 말고 업데이트 전 DB 백업과 대응 코드 버전을 함께 **별도 복구 환경에서** 확인합니다. 업데이트 뒤 생긴 기록도 먼저 별도 백업합니다. `reset --hard`, DB 디렉터리 삭제, `docker compose down -v`로 문제를 덮지 않습니다.

## 휴장·폐장 후에도 같은 대시보드 열기

**이 기능이 포함된 버전을 운영 VM에 배포한 뒤 적용됩니다.** 휴장일·정규장 종료 후 VM을 켜면 매매 엔진 대신 저장 기록을 읽는 대시보드와 DB를 시작합니다. 평소와 같은 주소·메뉴를 사용합니다. ‘매매 시작’을 눌러도 주문 요청을 저장하지 않고 **“오늘은 휴장일입니다”** 또는 **“오늘 정규장이 종료되었습니다”**를 표시합니다. 시세·뉴스·AI는 새로 수집하지 않으며 화면 상단에 저장 기록 조회 중임을 안내합니다. 거래일과 종료 시각은 뉴욕 거래소 달력 기준입니다.

Tailscale Serve가 이미 설치되어 있다면 같은 계정의 Tailscale에 연결한 PC·휴대폰에서 기존 HTTPS 주소로 접속합니다. Cloud Shell이나 PC의 SSH 터널은 필요 없습니다. VM이 꺼져 있으면 화면도 열리지 않습니다.

휴장·마감 후 업데이트는 검토한 커밋의 `update_preopen.sh`를 내려받아 SHA256을 확인한 다음 운영 VM의 tmux 안에서 실행합니다. 아래 파일·변수는 앞의 장전 절차에서 준비한 것과 같습니다.

```bash
sudo bash /tmp/sessionquant-update.sh "$SQ_REV" --off-hours < /dev/null
```

이 모드는 휴장일 또는 정규장 마감 후에만 허용합니다. 매매 엔진이 없어야 하고, 모의 장부가 종료 상태이며 보유·미체결·청산·현재 실행의 제어 요청이 없어야 합니다. 다른 엔진의 DB 잠금도 확인합니다. **새 백업 → 새 코드 설치 → 제한된 자료 정리 → DB·백업 검사 → 조회용 화면** 순서이며 토스·AI 호출이나 자동 매매 시작은 하지 않습니다. 긴 업데이트가 다음 거래 시간으로 넘어가도 조회 모드를 유지합니다. 백업·앱 교체 중에는 대시보드 접속이 잠시 중단됩니다.

이미 꺼진 구버전 VM은 먼저 [최초 한 번의 점검 부팅 준비](GCP_OFFHOURS_MIGRATION.md)를 해야 합니다. 새 스크립트를 GitHub에 병합한 것만으로 기존 VM의 1분 종료 예약이 바뀌지는 않습니다. 예전 저장 공간 복구용 `maintenance` 표시·서비스 마스크가 남은 상태는 별도 장애 복구 절차이므로 임의로 지우지 않습니다.

## 장 마감 처리와 다음 날 시작

일반 거래일에는 거래소 폐장 5분 후부터 **매매 엔진 저장·중지 → 백업 → 오래된 자료 정리 → 조회용 대시보드·DB 시작**을 실행합니다. 한국시간 겨울에는 다음 날 06:05, 서머타임에는 05:05부터이며 조기 폐장은 달력을 따릅니다. 백업·정리 중 화면은 잠시 열리지 않습니다. **VM 전원은 자동으로 끄지 않습니다.** 켜 둔 동안 VM 비용은 계속 발생합니다.

- 매매 프로그램을 시작할 때마다 그날 마감과 시작 시점부터 최대 20시간의 프로그램 종료를 예약합니다. 옛 ‘VM 부팅 후 20시간’ 타이머는 해제합니다. 휴장·폐장 후 조회 전용 시작에는 이 두 예약이 없습니다.
- 거래소 달력 조회에 실패하면 매매 엔진을 켜지 않고 조회 화면만 시도하며, 시작 서비스에는 실패를 남깁니다.
- 앱 오류 후에도 마감 예약은 남습니다. 프로그램 종료는 전량 체결 보장이 아니며 잔량은 저장합니다. 잔량이 있으면 조회는 가능하지만 `--off-hours` 업데이트는 거부합니다.
- 종료 백업은 최대 45분, 이후 정리는 최대 5회·전체 10분, 화면 재시작을 포함한 종료 서비스는 70분을 허용합니다. 백업 실패 시 자료를 삭제하지 않고 실패 표시를 보존합니다. 조회 화면이 열려도 백업 성공을 뜻하지 않으며 다음 매매 엔진 시작은 백업 성공 확인 전까지 차단합니다.

수동으로 같은 마감 처리를 실행하려면 대시보드에서 청산·잔량을 확인한 뒤 **운영 VM의 tmux 안에서** 실행합니다.

```bash
sudo bash /opt/sessionquant/deploy/gcp/stop.sh
sudo cat /etc/sessionquant/backup-status
sudo cat /etc/sessionquant/retention-status
```

`backup-status`의 새 `UPLOADED` 시각·파일명이 Cloud Storage와 일치하는지 확인합니다. 날짜의 `Z`는 UTC이며 한국시간은 9시간을 더합니다. 정리의 `PARTIAL`과 백업 실패는 서로 다른 상태입니다.

화면을 다 보고 VM까지 끄고 싶으면 **마감 처리 완료를 확인한 뒤** `sudo systemctl poweroff`를 직접 실행합니다. 이때 SSH 연결이 끊기는 것은 정상입니다.

VM을 계속 켜 둔 경우 다음 거래일 장전에 조회 모드에서 매매 프로그램으로 전환합니다. 마감 백업·업데이트가 실행 중이지 않을 때 운영 VM에서 실행합니다.

```bash
sudo systemctl restart sessionquant.service
```

새로 켠 프로그램은 매매 시작 버튼을 기다립니다. 자료 준비와 새 종료 예약을 확인한 뒤 버튼을 누릅니다. 조회 화면을 켜 둔다고 다음 거래일에 자동으로 수집·매매가 시작되지는 않습니다.

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

현재 전략 실험 프로필은 앱 시작 뒤 같은 토스 연결로 미국 거래소 목록을 수집해 `data/toss-us-universe.json`에 저장합니다. 세 거래소와 시총이 준비되면 그날 후보 최대 50개를 고정합니다. 새 목록 수집은 기존 모의 잔액·체결 기록을 초기화하지 않습니다.

아래 S&P 500 준비 명령은 **이전 `realtime-paper` 프로필에만 해당**합니다. 그 프로필의 앱 자료 디렉터리에 S&P 500 목록이 없다면 앱을 시작하기 전에 운영 VM에서 준비합니다. 이미 있는 목록은 덮어쓰지 않습니다.

```bash
if ! sudo test -e /var/lib/sessionquant/app/sp500-universe.json; then
  sudo docker compose -f /opt/sessionquant/deploy/gcp/compose.yaml run --rm --no-deps -T app \
    python -m sessionquant universe-refresh --output data/sp500-universe.json
fi
```

설치·설정·필요한 자료 이전을 마친 **거래일 장전**에 운영 VM에서 자동 시작과 종료 보호를 활성화합니다.

```bash
sudo systemctl enable sessionquant.service
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

권장 웹소켓 및 두 REST Paper 예제 설정은 `retention.enabled: true`다. **프로그램 저장·중지 → 새 Cloud Storage 백업 업로드 성공 → 오래된 자료 정리 → 조회용 대시보드·DB 시작** 순서로 실행한다. 장전 업데이트도 새 백업과 새 코드 적용 뒤, 앱을 시작하기 전에 같은 정리를 실행한다. 평소 부팅에 백업·삭제를 추가하지는 않는다. 이전 백업의 성공 기록만 남아 있거나 이번 백업이 실패하면 정리하지 않는다. 적용은 새 코드·이미지를 배포한 뒤 시작된다.

웹소켓 전략 실험 설정은 `retention.intraday_auxiliary: true`로 **당일 자료 보관**을 사용한다. 장전·장중에는 뉴욕 날짜의 자정 이전 임시 시세·미사용 후보 선정 기록·원시 매매 재현 기록을 정리한다. 폐장이 확인된 뒤에는 종료된 당일 자료도 정리한다. 서머타임과 조기 폐장을 반영하며, 달력이 미확인이면 당일 자료를 보존한다. 현재 실행 중으로 표시된 재현 실행, 날짜·종류가 불명확한 자료, 최근에 보충한 자료도 남긴다. 오류 확인용 시스템 로그와 시장 상태는 기존 7일 기준을 유지한다. 다른 실행 공간(namespace)은 자동 삭제하지 않는다.

체결·매도 이유·잔액·수익률·미체결·매매 신호·재시작 상태·전략용 과거 분봉은 이 정책으로 삭제하지 않는다. 과거 실행을 틱 단위로 다시 재현하려면 정리 전 백업을 별도 환경에 복원해야 한다. 정리된 실행 ID와 최초 정리 직전 백업 이름을 DB에 남기고, 해당 실행을 완전한 재현 묶음처럼 내보내는 것은 차단한다. 새로운 실행은 그 실행 ID를 지정해 내보낼 수 있다. 주문·손익 기록 조회에는 이 제한이 없다.

| 자료 | 당일 자료 보관을 켜지 않았을 때의 기본 기준 |
| --- | --- |
| 뉴스 수신·본문 추출 기록, 시세 관측, 시장 상태, 반복 집중·후보 선정 표, 시스템 로그 | 7일 |
| 시황 계산 스냅샷, LLM에 보내기 전 건너뛴 시황 분석 | 7일 |
| 운영용 기업·거시 뉴스와 분석 결과 | 30일. 매매 신호가 참조한 기사·분석은 보존 |
| 주문·체결·보유·손익·매매 신호·주문 근거인 후보 선정 표 | 자동 삭제하지 않음 |
| RVOL용 과거 분봉과 원본 분봉, 전략 재생용 입력·판단·뉴스 근거 | 자동 삭제하지 않음 |
| AI 호출·비용, 성공한 시황 LLM 분석, 재분석 방지 캐시, 현재 상태·복구·달력 | 자동 삭제하지 않음 |

기본 기간은 달력일 기준이다. 수신 시각뿐 아니라 DB 생성·수정 시각도 기준을 지나야 하므로, 오래된 날짜의 자료를 오늘 보충했다고 장중에 바로 삭제하지 않는다. 시각이 불명확하거나 아직 유효한 자료도 보존한다. 당일 자료 보관을 끈 설정은 연구 원장의 수신·추출·시황 계산 사본만 7일 기준으로 정리하며, 재현 입력과 판단은 남긴다. 당일 자료 보관을 켜도 운영용 뉴스·분석의 30일 기준과 매매에 참조된 근거 보호는 유지한다.

한 번에 최대 50,000행을 검사하고 계산 예산은 90초다. 각 묶음은 최대 200행이며, 진행 위치를 저장해 다음 정리 때 이어서 확인한다. `PARTIAL`은 예산 안에서 일부만 검사했다는 뜻이다. 각 컨테이너 실행은 120초와 강제 종료 유예 15초로 제한한다. 장 종료와 장전 업데이트 모두 누적 자료를 따라잡도록 최대 5번 이어서 실행한다. 장 종료의 정리 전체에는 600초와 강제 종료 유예 15초 제한을 추가하며, 백업·앱 종료·DB 종료와 함께 바깥 서비스의 70분 안에 포함된다. 5회 후 남은 자료가 있어도 전부 지웠다고 표시하지 않는다. 상태 파일과 `retention-last.json`은 마지막 회차의 결과이며 5회 합계가 아니다. 각 회차는 종료 서비스 로그에서 확인한다. 정리 오류는 백업 성공과 별도로 기록한다. 종료 작업은 실패를 기록하고 조회 화면으로 전환을 시도하며, 업데이트 작업은 화면을 다시 켜기 전에 중단한다. 정리 도중 터널이 끊겨도 서버의 종료 작업은 계속된다.

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

`COMPLETE`는 저장된 진행 위치부터 이번 순회를 마쳤다는 뜻이며, 삭제할 자료가 모두 없어졌다는 뜻은 아니다. `PARTIAL`은 행·시간 예산에 도달한 상태, `DISABLED`는 설정에서 해제, `FAILED`는 정리 오류다. 실패 시 JSON 파일은 이전 정상 결과일 수 있으므로 상태 파일의 시각도 함께 본다. 백업 자체 실패로 정리를 호출하지 않은 날에는 정리 상태 파일도 이전 값이다. 실제 삭제는 엔진 잠금과 최근 백업 성공 기록을 확인하며 운영 중인 엔진과 동시에 실행할 수 없다.

대시보드 시스템 설정과 JSON의 `observability`에서 남은 자료를 별도로 확인한다. 정리를 마친 뒤 **남은 행·시간 예산 안에서만**, 최대 2,000행의 작은 관측을 추가한다. 정리 검사 `scanned`와 관측 `observation_scanned`의 합계 `inspected`는 설정의 50,000행을 넘지 않는다. `PRESENT`는 보호 규칙까지 확인한 오래된 삭제 대상이 남았음, `CLEAR`는 모든 종류의 관측 범위를 끝까지 확인해 그런 대상이 없음, `UNKNOWN`은 예산 소진·표본 제한·조회 실패로 확인하지 못했음을 뜻한다. 미확인은 0건이 아니다. 수치가 표본이면 **최소 건수**이며 가장 오래된 대상의 나이도 관측한 범위 기준이다.

종류별 `tables.*.observation.generation`은 이전 관측 이후 **해당 실행 공간에 실제 새로 삽입된 행**을 센다. 다른 실행 공간이나 자동 증가 번호의 빈칸을 생성량으로 계산하지 않는다. 첫 관측과 관측을 건너뛴 뒤에는 기준만 만들며 생성량은 미확인이다. `exact: false`는 제한된 표본의 최소 건수이고 `interval_seconds`는 실제 비교 기간이다. 서로 다른 기간의 수치나 최소 건수를 정확한 하루 증가율로 비교하지 않는다. 기존 행의 수정은 새 삽입 수에 포함하지 않으며, 보관 기간 안의 새 자료가 많다는 것만으로 정리 지연이라고 판단하지 않는다.

예산에 연속으로 도달하거나, 오래된 대상이 남은 종류에서 관측한 새 삽입 최소 건수가 이번 삭제 건수를 넘으면 `CAPACITY_REVIEW`를 표시한다. 이는 용량 점검 신호이며 정확한 증가율이나 손실을 뜻하지 않는다. 반복 횟수, 실제 관측 기간, 삭제 건수, 오래된 대상의 나이를 함께 확인한다. 50,000행 제한이나 보관 기간은 자동으로 늘리지 않는다. 재생 근거처럼 삭제 대상이 아닌 자료는 별도의 디스크 사용량 점검이 필요하다.

MySQL의 `DELETE`는 내부 공간을 재사용하게 하며 `.ibd` 파일을 즉시 축소하지 않을 수 있다. 자동 `OPTIMIZE TABLE`이나 `TRUNCATE`는 수행하지 않는다. 이 기능이 이미 커진 19GB 파일을 즉시 줄이거나 과거 namespace까지 정리한다는 의미는 아니다. 최초 복구는 작업 공간 확보·새 백업 성공·논리 정리 확인 뒤 별도의 파일 공간 회수 절차를 검토한다. 분봉·장부·비용 근거는 계속 보존하므로 저장 공간 경보도 유지한다. 정리 전 자료는 백업의 보관 기간 동안만 복원할 수 있으며, 서버 최근 3개·Cloud Storage 7일 정책이 적용된다.

## 고급: 백업 실패와 복구

`/etc/sessionquant/backup-status`의 `UPLOADED` 시각·파일명과 버킷의 객체가 일치해야 업로드 완료입니다. 실패하면 앱 시작을 차단하고 원본 디스크를 유지합니다. 로그의 `phase`와 단계별 소요 시간을 확인하세요. 실패 표시를 지워 앱만 다시 켜지 않습니다. 원인을 해결한 뒤 DB가 실행 중인 점검 상태에서 `backup.sh`를 성공시켜야 합니다.

새 백업은 압축 SQL `database.sql.gz`와 `app/`을 담은 `tar.gz`입니다. 옛 백업의 `database.sql`도 가져오기 도구가 지원합니다. 바깥 `tar.gz`는 압축률 0으로 묶어 이미 압축한 SQL을 다시 압축하는 시간을 줄입니다. 45분 제한도 모든 크기의 백업 완료를 보장하지 않습니다. 실제 소요 시간과 압축 크기를 계속 확인합니다. 백업에 성공한 다음 자료를 정리하므로, 삭제로 덤프할 행이 줄어드는 효과는 **그다음 백업부터** 나타납니다. `.ibd` 할당 크기와 삭제 가능한 실제 행의 크기는 다르므로 파일 크기만으로 시간 단축 비율을 계산하지 않습니다.

복구는 자동 실행을 끈 **별도의 빈 서버·DB**에서 합니다. 관리자 계정으로 비공개 버킷에서 내려받아 새 임시 폴더에 풀고, 위 DB 이전 절차로 SQL을 가져옵니다. `app/`은 `/var/lib/sessionquant/app`에 복원하고 소유자를 `1000:1000`으로 맞춥니다. 장부·분봉·뉴스·프로필과 대응 코드 버전을 확인한 뒤 운영 전환을 판단합니다. 운영 DB 위에 덮어쓰거나 기존 디스크를 삭제하지 않습니다.

백업 실패 단계에 따라 새 로컬 완료 파일이 없을 수 있습니다. 묶기까지 끝났다면 업로드에 실패해도 로컬 파일은 남지만, 그 이전에 실패했다면 완료 백업이 생성되지 않습니다. 기존 완료 백업과 원본 DB는 별도로 보존합니다.

VM을 삭제해도 보존한 디스크·스냅샷·버킷·예약 IP는 남을 수 있습니다. 복구 자원을 정리할 때는 현재 연결 관계와 필요한 백업을 먼저 확인합니다. 과거 장애와 복구 근거는 [저장 공간 복구 기록](GCP_STORAGE_RECOVERY.md), 특히 [백업 시간 초과 기록](GCP_STORAGE_RECOVERY.md#2026-11-18-백업-시간-초과)을 참고하세요.


### 실행 중인 VM에서 이번 종료 설정만 적용

**아래는 2026-11-20 당시 구버전 전용 절차입니다. 휴장·폐장 조회 기능 배포 후에는 실행하지 않습니다.** `apply_shutdown_budget.py`는 당시 백업 시간 제한 수정만 적용하는 도구입니다. 이미 자료 정리 조회 오류 수정판(PR #35)을 배포했고 앱이 정상 실행 중이라면, 같은 DB 백업과 이미지 재빌드를 다시 하지 않고 오늘 종료 설정만 변경할 수 있습니다. 일반적인 다음 버전 업데이트를 대신하는 도구는 아닙니다.

검토한 커밋에서 [적용 도구](../deploy/gcp/apply_shutdown_budget.py)를 내려받고 안내된 SHA-256을 확인한 뒤, **운영 VM의 tmux 안에서** 실행합니다. `SQ_REV`에는 해당 수정이 포함된 검토된 전체 커밋 SHA를 지정합니다.

```bash
sudo python3 /tmp/sessionquant-shutdown-budget.py "$SQ_REV"
```

도구는 내용 해시가 일치하는 `start.sh`, `stop.sh`, 비용 제한 서비스, 정리 반복 스크립트만 설치합니다. 지원하지 않는 버전이나 직접 수정한 파일, 진행 중인 업데이트·백업·종료 작업이 있으면 중단합니다. 장 마감과 최대 실행 시간 타이머 모두 종료까지 10분 넘게 남아 있어야 합니다. 실제 DB·환경 파일·앱 이미지·Git HEAD는 바꾸지 않습니다.

장 마감 서비스에는 `/run/systemd/system/`, 현재 실행의 타이머에는 `/run/systemd/transient/` 아래 임시 설정을 추가합니다. 타이머 설정은 해당 임시 타이머를 교체할 때 함께 사라져, 다음 기동의 휴장·달력 오류 종료 예약에 이전 시각을 강요하지 않습니다. 타이머는 **이번 부팅에서의 고정된 종료 시각**(`OnBootSec`)을 사용합니다. 기존의 ‘활성화 후 일정 시간’(`OnActiveSec`) 예약은 `daemon-reload` 때 기준점이 옮겨질 수 있었으므로, 서비스 파일만 복원해도 예약 시각까지 복원되는 것은 아닙니다.

이전 도구에서 ‘종료 예약 시각이 바뀌었습니다’로 실패한 경우도 새 도구로 복구할 수 있습니다. 최초 타이머 등록 시각과 설정된 간격으로 원래 시각을 계산합니다. 이전 적용 기록의 시각이 이 기준과 1초 이내로 일치할 때만 그 기록의 정확한 값을 사용합니다. 이미 늦춰진 기록이나 다른 실행의 기록을 그대로 적용하지 않습니다. GCP VM의 부팅 시계를 전제로 하며 컨테이너, 임의 시간·무작위 지연·절전 해제 타이머는 거부합니다.

`daemon-reload` 뒤 두 서비스의 시간 제한과 실제 타이머 시각을 다시 확인합니다. 적용에 실패하면 백업·종료 설정 파일은 복원하고, 원래 예약을 유지하는 타이머 고정 설정은 남겨 다시 늦춰지는 것을 방지합니다(`ROLLED_BACK_TIMER_PINNED`). 이 상태는 백업 시간 제한 업데이트 성공을 뜻하지 않습니다. 복구 검증에도 실패하면 `ROLLBACK_FAILED`로 남겨 확인을 요청합니다. 이전 작은 설정 파일과 적용 상태는 `/var/lib/sessionquant/update-backups/shutdown-budget-*/`에 남습니다.

타이머·앱을 다시 시작하지 않으며, 다음 부팅에는 갱신된 `start.sh`가 같은 방식으로 새 거래일의 종료 예약과 70분 서비스 제한을 설정합니다. 적용 성공 출력에서 장 마감과 최대 실행 시간 예약을 한국시간으로 확인할 수 있습니다. 대시보드 접속 도우미는 이 설정 적용과 별도로 실행할 수 있습니다.

다음 일반 업데이트에서는 이 네 파일이 대상 커밋과 일치하는 변경으로 확인되어 정상 반영됩니다. 이 도구만 실행한 직후에는 Git의 마지막 커밋과 앱 이미지 버전이 그대로인 것이 정상입니다. 다음 부팅 후 `backup-status`의 새 `UPLOADED` 시각과 종료 로그의 단계별 시간을 확인해야 실제 장 마감 백업 성공을 알 수 있습니다.

### 백업 성공 후 자료 정리 조회가 시간 초과한 경우

MySQL 오류 3024는 SELECT가 설정된 실행 시간 제한을 넘었다는 뜻입니다. 권한 실패를 뜻하지 않습니다.
자료 정리는 시간 인덱스로 최대 한 페이지의 순번만 찾고, JSON 내용은 기본 키로 10행씩 확인합니다. 조회가 3초 제한에 걸리면 같은 대상을 더 작은 묶음으로 나누어 재시도하며, 제한 시간 자체는 늘리지 않습니다. 후보 번호 조회도 페이지 크기를 줄여 재시도합니다. 전체 정리 시간 한도를 지키고, 한 행도 읽지 못하는 경우에는 실패를 유지합니다.
한 페이지를 다 확인하지 못하면 그 페이지는 삭제하거나 다음 위치로 넘기지 않습니다.
정리 결과에는 해당 테이블·오류 번호와 `QUERY_TIMEOUT`, 실패 단계(`KEYS` 또는 `PAYLOAD`), 상세 조회의 순번 범위와 묶음 크기를 남기며 업데이트는 앱을 시작하지 않고 중단합니다.

새 백업 업로드와 코드 교체까지 끝난 뒤 정리에서 실패했다면, 수정 버전의 `update_preopen.sh`에
장전에는 `--resume-retention`, 휴장일·정규장 마감 후에는 `--resume-off-hours`를 지정해 중단된 업데이트를 이어갈 수 있습니다. `--resume-off-hours`는 완료 후 저장 기록 조회용 대시보드만 켭니다. 다음 조건을 모두 확인합니다.

- 정리 실패 기록이 있으며 앱 컨테이너가 중지돼 있을 것.
- 현재 코드와 실행 이미지의 커밋이 같고, 새 대상이 검토된 main의 후속 커밋일 것.
- 성공한 백업이 최근 1시간 이내이며 준비 중 백업 성공 기록이 바뀌지 않았을 것.
- 모의매매 종료 상태가 OFFLINE이고 매매가 해제돼 있으며, 백업 이후 실행 상태 기록이 갱신되지 않았을 것.
- 선택한 모드의 거래 시간, 엔진 잠금, 저장 공간 등 기존 업데이트 검사도 통과할 것.

이 모드는 백업 성공 시각을 다시 쓰지 않으며, 오래된 백업을 새 백업으로 간주하지 않습니다.
조건을 충족하지 못하면 일반 업데이트로 새 백업을 생성해야 합니다.
정리 완료 후에도 `.ibd` 파일은 즉시 작아지지 않을 수 있습니다.

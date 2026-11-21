# 구버전 VM의 1분 종료를 피하는 최초 점검 부팅

휴장·폐장 후 1분 뒤 꺼지는 **기존 버전의 VM이 이미 중지된 경우**에만 필요한 첫 전환 절차입니다. GitHub 병합만으로 VM의 코드가 바뀌지는 않습니다. 이 작업 뒤 새 버전을 배포하면 다음부터는 주말에도 같은 대시보드에 접속합니다.

1분 안에 SSH로 들어가려 하지 않습니다. 중지된 운영 디스크를 복구용 VM에 잠시 연결하고 **프로그램·옛 비용 타이머의 자동 시작만 해제**합니다. DB·환경 파일·앱 코드는 수정하지 않습니다. 새 DB 백업·코드 교체는 운영 VM을 다시 켠 뒤 정상 업데이트에서 진행합니다.

## 1. Windows Google Cloud SDK Shell에서 상태 확인

Cloud Shell은 필요 없습니다. PuTTY의 운영 VM 터미널이 아니라 Windows의 Google Cloud SDK Shell에서 실행합니다. 아래 조회는 자원을 바꾸지 않습니다.

```text
gcloud compute instances describe sessionquant-paper --project=sessionquant --zone=us-central1-a --format="yaml(status,disks)"
gcloud compute instances describe sessionquant-recovery --project=sessionquant --zone=us-central1-a --format="yaml(status,disks)"
```

운영 VM은 `TERMINATED`여야 합니다. `boot: true`인 디스크 이름과 `deviceName`, `autoDelete`를 기록합니다. 이 배포의 기존 이름은 `sessionquant-ubuntu-boot`였지만 출력으로 확인합니다. 복구 VM에 이전 점검 디스크가 연결돼 있다면 먼저 마운트·사용 여부를 확인합니다. **복제 부팅 디스크가 붙은 채 복구 VM을 재부팅하지 않습니다.** 동일한 파티션 식별자 때문에 잘못된 루트를 선택할 수 있습니다. 복구 VM이 없으면 기존 운영 디스크와 별개의 Ubuntu VM이 필요합니다.

운영 상태·복구 디스크가 예상과 다르면 여기서 멈춥니다. 아래 단계는 두 디스크를 이름으로 구분할 수 있을 때만 진행합니다. 복구 VM·보존 스냅샷에는 비용이 발생합니다.

## 2. GCP 콘솔에서 운영 디스크 보존·임시 연결

1. 운영 VM은 중지 상태로 둡니다. **디스크 → 운영 부팅 디스크 → 스냅샷 만들기**에서 새 스냅샷을 만들고 완료(`READY`)를 확인합니다. 기존 백업이 있다는 이유로 생략하지 않습니다.
2. 복구 VM은 자신의 원래 부팅 디스크로 정상 실행되어 있어야 합니다. 이전 점검 디스크가 있으면 사용하지 않는 것을 확인하고 언마운트·분리합니다. 디스크를 삭제하지 않습니다.
3. 중지된 운영 VM에서 운영 부팅 디스크를 **분리**합니다. VM이나 디스크 자체를 삭제하지 않습니다.
4. 실행 중인 복구 VM에 그 디스크를 추가 디스크로 연결합니다. 읽기/쓰기 모드, 장치 이름은 **`sq-operating`**, 부팅 디스크 아님으로 지정합니다. 콘솔에서 장치 이름을 지정할 수 없으면 SDK Shell에서 다음 형식을 사용합니다. `확인한_운영디스크명`은 1단계의 실제 디스크 이름입니다.

```text
gcloud compute instances attach-disk sessionquant-recovery --project=sessionquant --zone=us-central1-a --disk=확인한_운영디스크명 --device-name=sq-operating --mode=rw
```

원본 디스크를 연결한 동안 복구 VM을 재부팅하지 않습니다. 이 디스크에서 MySQL이나 SessionQuant를 실행하지 않습니다.

## 3. 복구 VM에서 자동 시작만 해제

SDK Shell에서 복구 VM에 접속합니다.

```text
gcloud compute ssh sessionquant-recovery --project=sessionquant --zone=us-central1-a --tunnel-through-iap
```

검토한 커밋의 `deploy/gcp/offline_inspection_boot.sh`를 내려받아 제공된 SHA256과 일치하는지 확인합니다. 이 문서의 두 값은 실제 검토된 값으로 바꿉니다. **복구 VM**에서만 실행합니다.

```bash
SQ_REV=검토한_40자리_커밋_SHA
SQ_SHA256=확인한_64자리_파일_SHA256
curl -fsSL "https://raw.githubusercontent.com/taegyoo1104/pennyquant/$SQ_REV/deploy/gcp/offline_inspection_boot.sh" -o /tmp/sq-inspection-boot.sh
printf '%s  %s\n' "$SQ_SHA256" /tmp/sq-inspection-boot.sh | sha256sum --check
```

`OK`를 확인한 뒤에만 아래를 실행합니다.

```bash
sudo mkdir -p /mnt/sq-operating
sudo mount /dev/disk/by-id/google-sq-operating-part1 /mnt/sq-operating
sudo bash /tmp/sq-inspection-boot.sh /mnt/sq-operating
sudo umount /mnt/sq-operating
```

스크립트는 마운트한 디스크가 지정한 운영 장치인지, 복구 VM 자신의 루트가 아닌지, 운영 호스트 이름인지 확인합니다. 기존 장애 복구용 마스크·표시가 있으면 덮어쓰지 않고 중단합니다. 성공 시 `systemctl --root=... disable`로 두 자동 시작 연결만 제거하며 서비스 파일·DB는 보존합니다.

오류가 나면 운영 VM을 켜지 말고 출력·현재 디스크 연결을 확인합니다. 특히 언마운트가 실패한 상태에서 디스크를 강제로 분리하지 않습니다.

## 4. 원래 VM으로 돌리고 정상 업데이트

1. 언마운트 완료 후 복구 VM에서 **운영 디스크만 분리**합니다.
2. 중지된 운영 VM에 **원래 부팅 디스크로** 재연결합니다. 원래 장치 이름을 사용하고, 디스크 자동 삭제는 끕니다. 새 빈 디스크나 스냅샷 복사본으로 바꾸지 않습니다.
3. 부팅 디스크 연결을 확인하고 운영 VM을 시작합니다. 지금은 앱과 옛 종료 예약이 자동 시작되지 않아 SSH로 접속할 수 있습니다.
4. 운영 VM의 tmux에서 검토한 새 업데이트 스크립트를 다운로드·해시 확인하고 **`--off-hours`**로 실행합니다. [GCP 운영 안내](GCP_PAPER.md#휴장폐장-후에도-같은-대시보드-열기)를 따릅니다. 장전이라면 일반 장전 업데이트를 사용합니다. 새 DB 백업을 성공시킨 뒤 코드·화면을 교체합니다.
5. 완료 후 기존 Tailscale HTTPS 주소를 엽니다. 앱과 DB가 정상이어야 하며 화면은 조회 전용입니다. 복구 VM에는 운영 디스크가 남아 있지 않은지 확인하고 복구 VM을 중지합니다. 원본 디스크·스냅샷은 삭제하지 않습니다.

중간에 업데이트를 보류했다면 운영 VM의 프로그램 자동 시작은 해제된 상태입니다. `install.sh`를 다시 실행하거나 초기 DB를 만들지 않습니다. 새 버전 업데이트 완료 시 `sessionquant.service`가 다시 활성화되며, 옛 부팅 20시간 타이머는 해제된 채 유지됩니다.

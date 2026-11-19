# 저장 공간 복구와 재발 방지 — 2026-11-17

**11월 17일 공간 복구는 완료:** 사용자가 40GB 디스크의 여유 약 31GB와 새 백업 업로드를 확인했다. 아래 복구 절차는 당시 기록이며 다시 실행하지 않는다. 11월 18일 발생한 별도의 백업 시간 초과는 이 문서 마지막에 기록했다. 이후 코드 갱신은 [GCP 장전 업데이트 안내](GCP_PAPER.md)의 보존형 업데이트를 사용한다. 특히 옛 묶음의 `history.py`를 최신 코드 위에 복사하지 않는다.

처음 읽는다면 [기획 변화와 장애 요약](PROJECT_STORY.md)을 먼저 참고한다. 아래의 ‘기존 자료 자동 삭제 없음’은 당시 복구 작업의 범위다. 이후 권장 프로필에 추가된 백업 후 오래된 자료 정리는 [현재 보관 정책](reference/DATABASE_STORAGE.md)과 구분한다.

실제 DB 약 767MB, MySQL binlog 약 12GB, 20GB 디스크 99% 사용을 사용자 출력으로 확인했다. 장 종료 백업의 `no space left on device`가 직접 원인이다. 종료는 정상 수행됐지만 새 백업은 없었다. CPU 100% 근처의 관측은 별도 성능 문제이며, 이번 저장 공간 부족과 같은 원인으로 단정하지 않는다.

수정: 단일 Paper DB의 binlog OFF, 압축 SQL 백업, 용량 부족 시 앱 정지, 백업 실패 상태 보존·다음 시작 차단, 동일 분봉 재저장 생략. 변경된 분봉·최초 수신 시각·수집 커서는 계속 저장한다. 기존 자료 자동 삭제·전략 조건 완화·실제 주문 활성화는 하지 않는다.

운영 서버에 직접 접속해 적용한 것은 아니다. 아래는 사용자가 실행할 절차다. 복제/PITR을 쓰는 다른 DB에는 적용하지 않는다. 새 스냅샷을 확보한 뒤 MySQL의 PURGE 명령으로 이전 binlog만 정리하며, 파일을 직접 지우거나 RESET BINARY LOGS AND GTIDS를 사용하지 않는다.

## 1. Cloud Shell에서 수정 파일 준비

제공된 `sessionquant-storage-fix-20261117.tar.gz`를 Cloud Shell 메뉴의 **업로드**로 홈 폴더에 올린다. 복구 서버 SSH 안에 있다면 `exit`하여 `@cloudshell`로 돌아온다.

```bash
mkdir -p "$HOME/sq-storage-fix-20261117"
tar -xzf "$HOME/sessionquant-storage-fix-20261117.tar.gz" -C "$HOME/sq-storage-fix-20261117"
cd "$HOME/sq-storage-fix-20261117"
sha256sum --check SHA256SUMS
bash deploy/gcp/cloudshell_prepare_storage.sh
```

준비 스크립트는 운영 VM 중지·복구 VM 실행·운영 부팅 디스크 이름을 확인한다. 운영 디스크의 새 READY 스냅샷을 만든 뒤, 검사 복사본을 복구 VM에서 분리한다. 운영 디스크를 잠시 복구 VM에 연결해 자동 실행·종료 서비스를 마스킹하고 점검 표시를 남긴 뒤 운영 VM에 다시 연결한다. DB 파일은 수정하지 않는다. SSH 호스트 키 검사도 끄지 않는다.

`운영 디스크 재연결·점검 모드 준비 완료`가 나온 뒤 다음 단계로 이동한다. 도중 실패하면 운영 VM을 시작하지 말고 출력·디스크 연결 상태를 확인한다. 원본 스냅샷과 디스크를 삭제하지 않는다. 복제 디스크를 연결한 채 복구 VM을 재부팅하지 않는다. 동일 PARTUUID로 잘못된 루트를 고를 수 있다.

## 2. Cloud Shell에서 사양 변경·점검 부팅

장중 CPU 100% 관측과 약 10거래일 시험 운영 예산을 반영한 권장값이다. 현재 `e2-small`은 공유 CPU이고, `e2-standard-2`는 vCPU 2개·메모리 8GB다. API 호출 제한은 이 변경으로 없어지지 않는다. 콘솔의 예상 비용·크레딧 잔액을 확인한다. 디스크 확대는 바로 축소할 수 없고 중지 후에도 저장 비용이 발생한다.

```bash
gcloud compute instances set-machine-type sessionquant-paper \
  --project=sessionquant --zone=us-central1-a --machine-type=e2-standard-2
gcloud compute disks resize sessionquant-ubuntu-boot \
  --project=sessionquant --zone=us-central1-a --size=40GB
gcloud compute instances start sessionquant-paper \
  --project=sessionquant --zone=us-central1-a
gcloud compute scp "$HOME/sessionquant-storage-fix-20261117.tar.gz" sessionquant-paper:~/ \
  --project=sessionquant --zone=us-central1-a --tunnel-through-iap < /dev/null
gcloud compute ssh sessionquant-paper \
  --project=sessionquant --zone=us-central1-a --tunnel-through-iap
```

이후 프롬프트가 `@sessionquant-paper`여야 한다. 점검 모드에서는 자동 종료도 막혀 있으므로 작업 후 수동 종료를 잊지 않는다. 외부 IP가 바뀌면 토스 허용 IP를 갱신한다.

## 3. 운영 서버에서 적용

```bash
df -h /
free -h
sudo systemctl is-enabled sessionquant.service sessionquant-cost-guard.timer
```

디스크가 약 39GB로 늘고 서비스·타이머는 `masked`여야 한다. `is-enabled`의 종료 코드가 0이 아니어도 masked 출력은 예상한 결과다. 파일시스템이 여전히 19GB이면 자동 확장을 확인하기 전 DB 정리를 시작하지 않는다. `lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS`로 실제 루트 파티션을 확인해서 확장해야 하며 다른 장치 이름을 추측해 실행하지 않는다.

```bash
bash <<'BASH'
set -Eeuo pipefail
[[ "$(hostname -s)" == sessionquant-paper ]] || exit 1
mkdir -p "$HOME/sq-storage-fix-20261117"
tar -xzf "$HOME/sessionquant-storage-fix-20261117.tar.gz" -C "$HOME/sq-storage-fix-20261117"
cd "$HOME/sq-storage-fix-20261117"
sha256sum --check SHA256SUMS
sudo cp -a /opt/sessionquant/deploy/gcp "/root/sq-gcp-before-storage-fix-$(date -u +%Y%m%dT%H%M%SZ)"
sudo cp -a deploy/gcp/. /opt/sessionquant/deploy/gcp/
sudo cp sessionquant/market/history.py /opt/sessionquant/sessionquant/market/history.py
cd /opt/sessionquant
sudo bash deploy/gcp/apply_storage_fix.sh --snapshot-confirmed < /dev/null
BASH
```

점검용 Compose로 DB만 실행 → 현재 기록 파일을 새로 회전 → 이전 binlog PURGE → 일반 Compose로 재생성하여 `log_bin=0` 확인 → 여유 공간 확인 → 새 백업 업로드 → 앱 이미지 재빌드 → 새 서비스 설치 순서다. 복제 설정·GTID 구성·실행 중 앱이 있으면 중단한다. 비밀번호를 출력하거나 새로 생성하지 않는다. 점검 모드는 성공 후에도 유지한다.

압축 백업은 `database.sql.gz`와 `app/`을 포함한다. SQL.gz는 `import_database.sh`에 그대로 전달할 수 있다. 옛 SQL 형식도 지원한다. 제공 묶음은 Git 커밋을 바꾸지 않고 파일을 적용하므로 서버 `git status`에는 변경이 보인다. 나중에 저장소와 통합할 때 덮어쓰거나 reset하지 말고 diff를 확인한다.

## 4. 결과 확인·다음 장 준비

```bash
df -h /
sudo cat /etc/sessionquant/backup-status
sudo docker compose -f /opt/sessionquant/deploy/gcp/compose.yaml exec -T db sh -c \
  'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root -e "SELECT @@GLOBAL.log_bin AS binlog_enabled;"' < /dev/null
```

`binlog_enabled=0`이면 변경 기록이 꺼졌다. `backup-status`는 `UPLOADED`여야 하며 Cloud Storage `daily/`에 이번 백업이 있어야 한다. 실패하면 점검 표시를 지우지 않는다.

다음 부팅부터 정상 운영하도록 예약하고 지금은 VM을 끄려면, 검증 성공 후 운영 서버에서 실행한다.

```bash
bash <<'BASH'
set -Eeuo pipefail
cd /opt/sessionquant
sudo grep -q '^UPLOADED ' /etc/sessionquant/backup-status
sudo python3 deploy/gcp/storage_check.py --minimum-mib 2048
sudo systemctl enable sessionquant.service sessionquant-cost-guard.timer
sudo rm -f /etc/sessionquant/maintenance /etc/sessionquant/storage-blocked
sudo systemctl poweroff
BASH
```

다음 장에는 VM을 수동으로 켜고 예열 후 대시보드에서 매매 시작을 누른다. `systemctl list-timers --all 'sessionquant-*'`에서 장 마감·비용 방지·저장 공간 감시 타이머를 확인한다. 장 마감에는 프로그램과 VM 모두 종료한다.

복구 VM은 운영 적용과 백업 확인 후 별도로 중지한다. 이번 절차로 검사 디스크는 이미 분리됐지만 옛 복구 DB가 실행 중이라면 먼저 정상 종료한다. 원본·스냅샷은 새 백업 복원 검증 전까지 보존한다. 테스트 종료 후에는 복구 자원과 스냅샷의 보관 필요성을 검토한다.

## 운영 중 확인할 것

- 첫 장중 CPU·메모리, 수집 주기, 종목별 마지막 시세 시각을 비교한다. 사양 증설만으로 집중 종목 0 문제나 ETF 표시 문제를 해결했다고 판단하지 않는다.
- 약 1분마다 1GiB 아래 또는 inode 여유 5% 미만을 감지하면 앱을 멈추고 재시작을 차단한다. 순간 쓰기 폭증까지 보장하는 하드 제한은 아니다.
- 실거래 장부·연구 자료는 자동 삭제하지 않는다. DB 증가 속도를 보고 연구 기록의 보관·내보내기 정책을 별도로 정한다.
- 초기 2GB 서버 기준 MySQL buffer pool 128MB는 이번 변경에서 유지했다. 8GB 증설 후에도 실제 메모리·조회 성능을 보고 조정한다.
- binlog를 끄면 백업 사이의 임의 시점으로 되돌리는 복구는 제공하지 않는다. VM 디스크·스냅샷과 검증된 논리 백업을 유지한다.

## 2026-11-18 백업 시간 초과

운영 디스크의 스냅샷으로 만든 읽기 전용 점검 디스크에서 사용자 제공 로그를 확인했다. 아래 시각은 한국시간이며, 11월 17일 미국 장 종료 뒤의 기록이다.

| 시각 | 확인한 일 |
| --- | --- |
| 11월 18일 06:05:08 | 앱이 멈추고 백업 시작 |
| 06:13:08 | 정확히 480초 뒤 `Terminated`, 백업 `FAILED` |
| 06:13:11 | DB·컨테이너 종료 완료. 당시 서비스는 백업 실패도 성공으로 보고 |
| 06:15경 | VM 중지 확인 |

**확인된 원인:** 종료 스크립트의 `timeout 480`이 오래 걸리는 백업을 중단했다. 당시 디스크 여유는 약 25GB, MySQL 자료는 약 5.4GB였다. 전날의 디스크 공간 부족과는 다른 장애다. 완료된 장 마감 백업 파일도 남지 않아 기존 파일을 업로드하는 것만으로는 해결할 수 없다. 옛 로그에는 단계 정보가 없어 DB 내보내기·검증·파일 묶기 중 어느 단계가 느렸는지는 확정하지 못했다.

**수정:** 백업 제한을 30분으로 늘리고, 프로그램·DB 종료 시간을 포함하는 바깥 systemd 제한도 40분으로 맞췄다. 이미 압축된 SQL을 다시 압축하지 않도록 묶음 방식을 바꿨으며 기존 `.tar.gz` 복원 형식을 유지한다. 각 단계의 시작·완료·소요 시간과 실패 코드를 기록하고, 백업 실패는 종료 서비스에서도 실패로 보고한다. 성공하면 즉시 VM을 종료하고, 제한을 넘기거나 실패해도 원본 디스크·기존 완료 백업을 보존한 뒤 VM을 종료한다.

`backup-status`의 단계는 `DUMP`(DB 내보내기), `VERIFY_SQL`(압축 SQL 검사), `PACKAGE`(자료 묶기), `VERIFY_ARCHIVE`(묶음 검사), `UPLOAD`(업로드)다. 시작 전 실패는 `PREPARE`, 종료 스크립트가 대신 기록한 실패는 `SHUTDOWN`으로 남는다. 업로드가 확인돼야 `UPLOADED`가 된다. 실패 표시를 수동으로 성공으로 바꾸지 않는다.

운영 서버 적용과 실제 장 마감 백업 재검증은 별도로 필요하다. 다음 장전 업데이트 과정에서 새 논리 백업 업로드를 확인하고, 별도 빈 DB에서 복원과 주요 기록을 확인할 때까지 원본·점검 스냅샷을 보존한다. DB 크기와 단계별 시간을 계속 관찰하며, 30분 제한을 넘으면 기록된 단계에 맞춰 추가 개선한다. 이 로그만으로 서버 증설이 필요하다고 판단하지 않는다.

서버에 SSH 접속한 뒤 읽기만 하는 확인 명령:

```bash
sudo cat /etc/sessionquant/backup-status
sudo journalctl -u sessionquant-market-close.service -u sessionquant-cost-guard.service --since today --no-pager -n 100
```

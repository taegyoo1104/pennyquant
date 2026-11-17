#!/bin/bash
# User-operated recovery for the existing sessionquant deployment. No deletion.
set -Eeuo pipefail
trap 'echo "준비가 중단됐습니다. 운영 VM을 켜지 말고 마지막 출력과 디스크 연결 상태를 확인하세요." >&2' ERR
cd "$(dirname "$0")"
gce=(gcloud compute --project=sessionquant)
zone=us-central1-a
[[ $("${gce[@]}" instances describe sessionquant-paper --zone="$zone" --format='value(status)') == TERMINATED ]] || {
    echo '운영 VM이 중지된 상태여야 합니다.' >&2; exit 1;
}
[[ $("${gce[@]}" instances describe sessionquant-recovery --zone="$zone" --format='value(status)') == RUNNING ]] || {
    echo '복구 VM이 정상 부팅된 상태여야 합니다.' >&2; exit 1;
}
boot=$("${gce[@]}" instances describe sessionquant-paper --zone="$zone" --format='json(disks)' | python3 -c \
    'import json,sys; print(next(d["source"].rsplit("/",1)[1] for d in json.load(sys.stdin)["disks"] if d.get("boot")))')
[[ "$boot" == sessionquant-ubuntu-boot ]] || { echo '예상한 운영 부팅 디스크가 아닙니다.' >&2; exit 1; }
snapshot="sessionquant-before-storage-fix-$(date -u +%Y%m%d-%H%M%S)"
"${gce[@]}" snapshots create "$snapshot" --source-disk="$boot" --source-disk-zone="$zone" < /dev/null
[[ $("${gce[@]}" snapshots describe "$snapshot" --format='value(status)') == READY ]]
echo "복구 스냅샷 확인: $snapshot"
"${gce[@]}" scp offline_maintenance.sh sessionquant-recovery:~/sq-offline-maintenance.sh --zone="$zone" --tunnel-through-iap < /dev/null
"${gce[@]}" ssh sessionquant-recovery --zone="$zone" --tunnel-through-iap \
    --command='set -eu; if mountpoint -q /mnt/sq-postclose; then sudo -n umount /mnt/sq-postclose; fi' < /dev/null
devices=$("${gce[@]}" instances describe sessionquant-recovery --zone="$zone" --format='value(disks.deviceName)')
if [[ "$devices" == *sq-postclose* ]]; then
    "${gce[@]}" instances detach-disk sessionquant-recovery --zone="$zone" --disk=sessionquant-postclose-inspect < /dev/null
fi
"${gce[@]}" instances detach-disk sessionquant-paper --zone="$zone" --disk="$boot" < /dev/null
"${gce[@]}" instances attach-disk sessionquant-recovery --zone="$zone" --disk="$boot" --device-name=sq-operating --mode=rw < /dev/null
"${gce[@]}" ssh sessionquant-recovery --zone="$zone" --tunnel-through-iap --command='set -eu
sudo -n mkdir -p /mnt/sq-operating
sudo -n mount /dev/disk/by-id/google-sq-operating-part1 /mnt/sq-operating
sudo -n bash "$HOME/sq-offline-maintenance.sh" /mnt/sq-operating
sudo -n umount /mnt/sq-operating' < /dev/null
"${gce[@]}" instances detach-disk sessionquant-recovery --zone="$zone" --disk="$boot" < /dev/null
"${gce[@]}" instances attach-disk sessionquant-paper --zone="$zone" --disk="$boot" --boot --device-name=sessionquant-ubuntu-boot < /dev/null
"${gce[@]}" instances set-disk-auto-delete sessionquant-paper --zone="$zone" --disk="$boot" --no-auto-delete < /dev/null
echo '운영 디스크 재연결·점검 모드 준비 완료. 운영 VM은 아직 중지 상태입니다.'
echo '이 스크립트를 재실행하지 말고 사양 변경·파일 전송·DB 정리 단계로 진행하세요.'

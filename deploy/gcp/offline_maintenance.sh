#!/bin/bash
# Run on the recovery helper, against the detached OPERATING boot disk mounted rw.
set -Eeuo pipefail
[[ $EUID == 0 && $# == 1 ]] || { echo '사용법: sudo bash offline_maintenance.sh 운영디스크마운트경로' >&2; exit 1; }
root=$(realpath "$1")
[[ "$root" != / && "$root" != /mnt/sq-postclose && -d "$root/etc/sessionquant" && -f "$root/opt/sessionquant/deploy/gcp/compose.yaml" ]] || exit 1
mountpoint -q "$root"
[[ ",$(findmnt -n -o OPTIONS --mountpoint "$root")," == *,rw,* ]] || { echo '쓰기 가능한 운영 디스크가 아닙니다.' >&2; exit 1; }
[[ $(findmnt -n -o SOURCE --mountpoint "$root") != "$(findmnt -n -o SOURCE /)" ]] || exit 1
[[ $(basename "$(readlink -f /dev/disk/by-id/google-sq-operating-part1)") == "$(basename "$(findmnt -n -o SOURCE --mountpoint "$root")")" ]] || {
    echo 'sq-operating 장치와 마운트가 다릅니다.' >&2; exit 1;
}
install -d -m 700 "$root/etc/sessionquant/units-before-storage-repair"
for unit in sessionquant.service sessionquant-cost-guard.timer sessionquant-cost-guard.service sessionquant-market-close.service sessionquant-storage-guard.timer; do
    target="$root/etc/systemd/system/$unit"
    saved="$root/etc/sessionquant/units-before-storage-repair/$unit"
    if [[ -e "$target" || -L "$target" ]]; then
        if [[ $(readlink "$target" || true) != /dev/null && ! -e "$saved" && ! -L "$saved" ]]; then
            cp -a "$target" "$saved"
        fi
    fi
    ln -sfn /dev/null "$target"
done
touch "$root/etc/sessionquant/maintenance"
chmod 600 "$root/etc/sessionquant/maintenance"
sync
echo '운영 디스크 점검 모드 준비 완료. 언마운트·분리 후 운영 VM의 부팅 디스크로 다시 연결하세요.'

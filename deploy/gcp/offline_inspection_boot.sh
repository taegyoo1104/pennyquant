#!/bin/bash
# One-time migration on a helper VM. The OPERATING VM must be stopped and its
# boot disk detached, snapshotted, then mounted rw as google-sq-operating-part1.
# This disables only automatic startup; it never starts/repairs/edits MySQL.
set -Eeuo pipefail
[[ $EUID == 0 && $# == 1 ]] || { echo 'sudo bash offline_inspection_boot.sh 운영디스크마운트경로' >&2; exit 1; }
root=$(realpath "$1")
[[ "$root" != / && -f "$root/opt/sessionquant/deploy/gcp/compose.yaml" && -d "$root/etc/sessionquant" ]] || exit 1
mountpoint -q "$root"
[[ ",$(findmnt -n -o OPTIONS --mountpoint "$root")," == *,rw,* ]] || exit 1
sq_source=$(readlink -f "$(findmnt -n -o SOURCE --mountpoint "$root")")
[[ "$sq_source" == "$(readlink -f /dev/disk/by-id/google-sq-operating-part1)" && \
   "$sq_source" != "$(readlink -f "$(findmnt -n -o SOURCE /)")" ]] || {
    echo '운영 디스크 장치가 다릅니다. 중단합니다.' >&2; exit 1;
}
# GCE may set a transient hostname without creating /etc/hostname. The primary
# identity is the explicitly attached operating disk, not this optional file.
if [[ -e "$root/etc/hostname" || -L "$root/etc/hostname" ]]; then
    [[ -f "$root/etc/hostname" && ! -L "$root/etc/hostname" ]] || {
        echo '호스트명 파일 형식을 확인하지 못했습니다. 중단합니다.' >&2; exit 1;
    }
    [[ $(cut -d. -f1 "$root/etc/hostname") == sessionquant-paper ]] || {
        echo '다른 서버의 호스트명이 기록되어 있습니다. 중단합니다.' >&2; exit 1;
    }
fi
# Reject recovery masks rather than overwriting an existing incident procedure.
for unit in sessionquant.service sessionquant-cost-guard.timer; do
    [[ -f "$root/etc/systemd/system/$unit" && ! -L "$root/etc/systemd/system/$unit" ]] || exit 1
done
for config in app.env db.env; do
    [[ -f "$root/etc/sessionquant/$config" && ! -L "$root/etc/sessionquant/$config" ]] || {
        echo '운영 환경 파일을 확인하지 못했습니다. 중단합니다.' >&2; exit 1;
    }
done
grep -Fxq 'ExecStart=/opt/sessionquant/deploy/gcp/start.sh' "$root/etc/systemd/system/sessionquant.service" || {
    echo '예상한 운영 서비스가 아닙니다. 중단합니다.' >&2; exit 1;
}
[[ ! -e "$root/etc/sessionquant/maintenance" ]] || exit 1
systemctl --root="$root" disable sessionquant.service sessionquant-cost-guard.timer
for unit in sessionquant.service sessionquant-cost-guard.timer; do
    sq_enabled=$(systemctl --root="$root" is-enabled "$unit" 2>/dev/null || true)
    [[ "$sq_enabled" == disabled ]] || { echo "$unit 자동 시작 해제를 확인하지 못했습니다." >&2; exit 1; }
done
sync
echo '자동 시작 해제 완료. DB·환경 파일·앱 코드는 그대로입니다.'
echo '언마운트 후 원래 VM에 부팅 디스크를 재연결하세요. 부팅 후 --off-hours 업데이트를 실행하세요.'

#!/bin/bash
# Download this script at the reviewed commit and run on the operating VM, pre-open.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 && ${1:-} =~ ^[0-9a-f]{40}$ && $# -le 2 && (${2:-} == '' || ${2:-} == --paused-paper) ]] || { echo 'sudo bash update_preopen.sh 검토한커밋SHA [--paused-paper]' >&2; exit 1; }
[[ $(cat /sys/class/dmi/id/product_name 2>/dev/null) == 'Google Compute Engine' && $(hostname -s) == sessionquant-paper ]] || { echo 'sessionquant-paper VM에서 실행하세요.' >&2; exit 1; }
cd /opt/sessionquant
exec 9>/run/sessionquant-update.lock
flock -n 9 || { echo '다른 업데이트가 실행 중입니다.' >&2; exit 1; }
[[ ! -e /etc/sessionquant/maintenance && ! -e /etc/sessionquant/storage-blocked ]] || { echo '기존 점검·저장 공간 차단을 먼저 확인하세요.' >&2; exit 1; }
for unit in sessionquant-market-close.service sessionquant-cost-guard.service; do
    if systemctl is-active --quiet "$unit"; then echo '종료·백업 중에는 업데이트할 수 없습니다.' >&2; exit 1; fi
done
sq_target=$1
sq_mode=${2:-}
git fetch origin refs/heads/main:refs/remotes/origin/main < /dev/null
sq_stage=$(mktemp -d /var/tmp/sessionquant-update.XXXXXXXX)
trap 'rm -rf -- "$sq_stage"' EXIT
trap 'echo "업데이트가 중단됐습니다. 위 오류를 확인하세요. DB·환경 파일은 삭제하지 않았습니다." >&2' ERR
git archive "$sq_target" | tar -x -C "$sq_stage"
python3 "$sq_stage/deploy/gcp/prepare_update.py" --repo /opt/sessionquant --target "$sq_target"
python3 "$sq_stage/deploy/gcp/storage_check.py" --minimum-mib 4096
# Existing image/calendar first: do not race the after-hours automatic shutdown.
check_window() {
if [[ "$sq_mode" == --paused-paper ]]; then
    docker compose -f deploy/gcp/compose.yaml run --rm --no-deps -T app \
        python - "$@" < "$sq_stage/deploy/gcp/update_guard.py"
else
docker run --rm --network none sessionquant:local python -c '
from datetime import datetime,timezone
from sessionquant.market.bars import session_bounds
now=datetime.now(timezone.utc); bounds=session_bounds(now)
if not bounds or now>=bounds[0]: raise SystemExit("오늘 미국 정규장 시작 전, 매매 시작 버튼을 누르기 전에 실행하세요.")
print("장전 시간 확인 완료")
' < /dev/null
fi
}
check_window
sq_image="sessionquant:preopen-${sq_target:0:12}"
docker build --build-arg "SQ_BUILD_REVISION=$sq_target" --build-arg SQ_BUILD_SOURCE=reviewed_archive \
    -t "$sq_image" "$sq_stage" < /dev/null
# A build can take time; recheck both the session and pending shutdown.
check_window
for unit in sessionquant-market-close.service sessionquant-cost-guard.service; do
    if systemctl is-active --quiet "$unit"; then echo '종료·백업 중에는 업데이트할 수 없습니다.' >&2; exit 1; fi
done
# Check unit masks before stopping the current application.
for path in "$sq_stage"/deploy/gcp/sessionquant*.service "$sq_stage"/deploy/gcp/sessionquant*.timer; do
    [[ $(systemctl is-enabled "$(basename "$path")" 2>/dev/null || true) != masked ]] || { echo '서비스가 점검용으로 차단돼 있습니다.' >&2; exit 1; }
done
# Keep credentials and persistent DB/app directories in place.
systemctl stop sessionquant.service
compose=(docker compose -f deploy/gcp/compose.yaml)
"${compose[@]}" stop -t 180 app < /dev/null
"${compose[@]}" up -d --wait --wait-timeout 180 db < /dev/null
if [[ "$sq_mode" == --paused-paper ]]; then check_window --stopped; fi
sq_logging=$("${compose[@]}" exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B -e "SELECT @@GLOBAL.log_bin"' < /dev/null)
[[ "$sq_logging" == 0 ]] || { echo 'DB 변경 기록이 켜져 있습니다. 기존 저장 공간 수정을 먼저 적용하세요.' >&2; exit 1; }
sq_previous_backup=$(cat /etc/sessionquant/backup-status 2>/dev/null || echo MISSING)
bash deploy/gcp/backup.sh < /dev/null
install -d -m 700 /var/lib/sessionquant/update-backups
sq_backup="/var/lib/sessionquant/update-backups/$(date -u +%Y%m%dT%H%M%SZ)-${sq_target:0:12}"
python3 "$sq_stage/deploy/gcp/prepare_update.py" --repo /opt/sessionquant --target "$sq_target" --backup "$sq_backup"
if ! docker image inspect "sessionquant:before-${sq_target:0:12}" >/dev/null 2>&1; then
    docker tag sessionquant:local "sessionquant:before-${sq_target:0:12}"
fi
docker tag "$sq_image" sessionquant:local
chmod 755 deploy/gcp/*.sh
for path in deploy/gcp/sessionquant*.service deploy/gcp/sessionquant*.timer; do
    [[ $(systemctl is-enabled "$(basename "$path")" 2>/dev/null || true) != masked ]] || { echo '서비스가 점검용으로 차단돼 있습니다.' >&2; exit 1; }
    install -m 644 "$path" "/etc/systemd/system/$(basename "$path")"
done
systemctl daemon-reload
"${compose[@]}" up -d --wait --wait-timeout 180 db < /dev/null
# The app is stopped and its original ledger has been backed up. Preserve the
# strict restart-settings guard; upgrade only the reviewed, flat Paper profile.
"${compose[@]}" run --rm --no-deps -T app python -m sessionquant.portfolio.position_limit_upgrade < /dev/null
# The new policy runs only after this update's successful backup, with the app
# still stopped. Bounded passes may leave a backlog; no physical table rebuild.
bash deploy/gcp/retention_after_update.sh "$sq_previous_backup" < /dev/null
bash deploy/gcp/preopen_check.sh < /dev/null
systemctl enable sessionquant.service sessionquant-cost-guard.timer
systemctl start sessionquant-cost-guard.timer
systemctl start sessionquant.service
systemctl is-active --quiet sessionquant-market-close.timer
systemctl is-active --quiet sessionquant-storage-guard.timer
systemctl list-timers --all 'sessionquant-*' --no-pager
"${compose[@]}" ps
echo '업데이트·연결 점검·자동 종료 예약 확인 완료. 대시보드에서 자료 준비 후 매매 시작을 직접 누르세요.'

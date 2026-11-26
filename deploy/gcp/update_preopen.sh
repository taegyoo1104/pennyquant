#!/bin/bash
# Download at the reviewed commit; off-hours inspection must be explicit.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 && ${1:-} =~ ^[0-9a-f]{40}$ && $# -le 2 && (${2:-} == '' || ${2:-} == --paused-paper || ${2:-} == --resume-retention || ${2:-} == --off-hours || ${2:-} == --resume-off-hours) ]] || { echo 'sudo bash update_preopen.sh 검토한커밋SHA [--paused-paper|--resume-retention|--off-hours|--resume-off-hours]' >&2; exit 1; }
[[ $(cat /sys/class/dmi/id/product_name 2>/dev/null) == 'Google Compute Engine' && $(hostname -s) == sessionquant-paper ]] || { echo 'sessionquant-paper VM에서 실행하세요.' >&2; exit 1; }
cd /opt/sessionquant
exec 9>/run/sessionquant-update.lock
flock -n 9 || { echo '다른 업데이트가 실행 중입니다.' >&2; exit 1; }
[[ ! -e /etc/sessionquant/maintenance && ! -e /etc/sessionquant/storage-blocked ]] || { echo '기존 점검·저장 공간 차단을 먼저 확인하세요.' >&2; exit 1; }
for unit in sessionquant-market-close.service sessionquant-cost-guard.service sessionquant-run-limit.service; do
    if systemctl is-active --quiet "$unit"; then echo '종료·백업 중에는 업데이트할 수 없습니다.' >&2; exit 1; fi
done
sq_target=$1
sq_mode=${2:-}
sq_reuse=false
sq_resume=false
if [[ "$sq_mode" == --resume-retention || "$sq_mode" == --resume-off-hours ]]; then sq_resume=true; fi
if [[ "$sq_mode" == --resume-off-hours ]]; then sq_mode=--off-hours; fi
sq_resume_head=$(git rev-parse HEAD)
sq_resume_receipt=$(cat /etc/sessionquant/backup-status 2>/dev/null || echo MISSING)
git fetch origin refs/heads/main:refs/remotes/origin/main < /dev/null
sq_stage=$(mktemp -d /var/tmp/sessionquant-update.XXXXXXXX)
trap 'rm -rf -- "$sq_stage"' EXIT
trap 'echo "업데이트가 중단됐습니다. 위 오류를 확인하세요. DB·환경 파일은 삭제하지 않았습니다." >&2' ERR
git archive "$sq_target" | tar -x -C "$sq_stage"
source "$sq_stage/deploy/gcp/lifecycle_lock.sh"
python3 "$sq_stage/deploy/gcp/prepare_update.py" --repo /opt/sessionquant --target "$sq_target"
python3 "$sq_stage/deploy/gcp/storage_check.py" --minimum-mib 4096
# Existing image/calendar first; do not start the trading engine for inspection.
check_window() {
if [[ "$sq_mode" == --off-hours ]]; then
    local sq_app sq_command
    sq_app=$(docker compose -f deploy/gcp/compose.yaml ps --status running --services app)
    if [[ -n "$sq_app" ]]; then
        sq_command=$(docker inspect sessionquant-app-1 --format '{{json .Config.Cmd}}')
        [[ "$sq_app" == app && "$sq_command" == '["python","-m","sessionquant.inspection_dashboard"]' ]] || {
            echo '매매 프로그램을 종료하고 점검 대시보드로 전환한 뒤 업데이트하세요.' >&2; return 1;
        }
    fi
    docker run --rm --network none sessionquant:local python -c '
from datetime import datetime,timezone
from sessionquant.market.bars import session_bounds
now=datetime.now(timezone.utc); bounds=session_bounds(now)
if bounds and now<bounds[1]: raise SystemExit("휴장일·정규장 마감 후에만 --off-hours를 사용하세요.")
' < /dev/null
    # The one-time offline boot preparation leaves both containers stopped.
    docker compose -f deploy/gcp/compose.yaml up -d --wait --wait-timeout 180 db < /dev/null
    docker compose -f deploy/gcp/compose.yaml run --rm --no-deps -T app \
        python - --off-hours "$@" < "$sq_stage/deploy/gcp/update_guard.py"
elif [[ "$sq_mode" == --paused-paper ]]; then
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
check_resume() {
    [[ "$sq_resume" == true ]] || return 0
    [[ $(git rev-parse HEAD) == "$sq_resume_head" && $(cat /etc/sessionquant/backup-status) == "$sq_resume_receipt" ]] || {
        echo '재개 준비 중 코드 또는 백업 기록이 변경됐습니다.' >&2; return 1;
    }
    grep -q '^FAILED ' /etc/sessionquant/retention-status || {
        echo '자료 정리 실패 후 중단된 업데이트만 재개할 수 있습니다.' >&2; return 1;
    }
    local sq_app
    sq_app=$(docker compose -f deploy/gcp/compose.yaml ps --status running --services app) || return 1
    [[ -z "$sq_app" ]] || { echo '프로그램이 실행 중이므로 기존 백업으로 재개하지 않습니다.' >&2; return 1; }
    docker compose -f deploy/gcp/compose.yaml run --rm --no-deps -T app \
        python - "$sq_resume_head" "$sq_resume_receipt" < "$sq_stage/deploy/gcp/resume_retention_guard.py"
}
check_window
check_resume
if [[ "$sq_mode" == --off-hours ]]; then
    # OFFLINE, flat ledger and absence of an engine have just been verified.
    systemctl disable --now sessionquant-cost-guard.timer
    systemctl stop sessionquant-market-close.timer sessionquant-run-limit.timer 2>/dev/null || true
fi
sq_image="sessionquant:preopen-${sq_target:0:12}"
docker build --build-arg "SQ_BUILD_REVISION=$sq_target" --build-arg SQ_BUILD_SOURCE=reviewed_archive \
    -t "$sq_image" "$sq_stage" < /dev/null
# A build can take time; recheck both the session and pending shutdown.
check_window
for unit in sessionquant-market-close.service sessionquant-cost-guard.service sessionquant-run-limit.service; do
    if systemctl is-active --quiet "$unit"; then echo '종료·백업 중에는 업데이트할 수 없습니다.' >&2; exit 1; fi
done
check_resume
# Check unit masks before stopping the current application.
for path in "$sq_stage"/deploy/gcp/sessionquant*.service "$sq_stage"/deploy/gcp/sessionquant*.timer; do
    [[ $(systemctl is-enabled "$(basename "$path")" 2>/dev/null || true) != masked ]] || { echo '서비스가 점검용으로 차단돼 있습니다.' >&2; exit 1; }
done
# Keep credentials and persistent DB/app directories in place.
systemctl stop sessionquant.service
compose=(docker compose -f deploy/gcp/compose.yaml)
"${compose[@]}" stop -t 180 app < /dev/null
"${compose[@]}" up -d --wait --wait-timeout 180 db < /dev/null
if [[ "$sq_mode" == --paused-paper || "$sq_mode" == --off-hours ]]; then check_window --stopped; fi
sq_logging=$("${compose[@]}" exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B -e "SELECT @@GLOBAL.log_bin"' < /dev/null)
[[ "$sq_logging" == 0 ]] || { echo 'DB 변경 기록이 켜져 있습니다. 기존 저장 공간 수정을 먼저 적용하세요.' >&2; exit 1; }
sq_previous_backup=$(cat /etc/sessionquant/backup-status 2>/dev/null || echo MISSING)
if [[ "$sq_resume" == true ]]; then
    check_resume
    # Same interrupted deployment: validate the existing fresh upload and
    # unchanged OFFLINE heartbeat rather than fabricating a new receipt.
    sq_previous_backup="RESUME $sq_resume_receipt"
    echo '성공한 최근 백업을 확인했습니다. 중단된 자료 정리 단계부터 재개합니다.'
elif [[ -f /etc/sessionquant/nightly-checkpoint.json ]] &&
    python3 "$sq_stage/deploy/gcp/nightly_checkpoint.py" verify; then
    sq_reuse=true
    echo '야간 정리·최종 백업 이후 변경 없음 확인: 전체 백업을 재사용합니다.'
else
    echo '재사용 가능한 야간 백업 없음: 이번에는 새 백업을 만듭니다.'
    timeout --kill-after=30 5400 bash deploy/gcp/backup.sh < /dev/null
fi
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
# Invalidate proof before any migration/runtime writes. Verification happened
# with the old namespace, files, database instance and sealed state unchanged.
if [[ "$sq_reuse" == true ]]; then
    python3 deploy/gcp/nightly_checkpoint.py verify
fi
if [[ -e /etc/sessionquant/nightly-seal.json ]]; then
    python3 deploy/gcp/nightly_checkpoint.py writable
fi
# The app is stopped and its original ledger has been backed up. Preserve the
# strict restart-settings guard; upgrade only the reviewed, flat Paper profile.
"${compose[@]}" run --rm --no-deps -T app python -m sessionquant.portfolio.position_limit_upgrade < /dev/null
# Probe using the new policy/image. A fresh backup from this same operation
# covers cleanup; a reused nightly snapshot is not a fresh deletion receipt.
if [[ "$sq_reuse" == true ]]; then
    python3 deploy/gcp/startup_retention.py < /dev/null
else
    python3 deploy/gcp/startup_retention.py --backup-before "$sq_previous_backup" < /dev/null
fi
systemctl enable sessionquant.service
systemctl disable --now sessionquant-cost-guard.timer
if [[ "$sq_mode" == --off-hours ]]; then
    # A long build may cross midnight/open. Never switch to a trading runtime.
    bash deploy/gcp/preopen_check.sh --offline < /dev/null
    bash deploy/gcp/inspect.sh < /dev/null
else
    bash deploy/gcp/preopen_check.sh < /dev/null
    # systemd does not inherit fd 8. Hand off this operation's completed check,
    # then release the lifecycle lock before asking the service to acquire it.
    if ! python3 deploy/gcp/startup_handoff.py create; then
        rm -f /run/sessionquant-startup-prepared.json
        echo '이번 정리 확인을 전달하지 못했습니다. 서비스 시작에서 다시 확인합니다.' >&2
    fi
    flock -u 8
    exec 8>&-
    systemctl start sessionquant.service
    systemctl is-active --quiet sessionquant-market-close.timer
    systemctl is-active --quiet sessionquant-run-limit.timer
    systemctl is-active --quiet sessionquant-storage-guard.timer
fi
if [[ -f /etc/sessionquant/public-enabled ]]; then
    docker compose -f deploy/gcp/compose.public.yaml up -d --wait --wait-timeout 180 < /dev/null
    docker compose -f deploy/gcp/compose.public.yaml exec -T viewer python -c '
from sessionquant.dashboard.session_app import load_database
from sessionquant.dashboard.access import is_admin
d=load_database("data/sessionquant-paper-local.db",page="대시보드",panel="public")
assert not is_admin() and d.connected and not d.error, "공개 화면 DB 조회 확인 실패"
' < /dev/null
    python3 deploy/gcp/check_public_routes.py --ready
fi
systemctl list-timers --all 'sessionquant-*' --no-pager
"${compose[@]}" ps
if [[ "$sq_mode" == --off-hours ]]; then
    echo '점검 업데이트 완료. 대시보드는 저장 기록 조회 전용이며 VM을 자동 종료하지 않습니다.'
else
    echo '업데이트·연결 점검·프로그램 종료 예약 확인 완료. 대시보드에서 자료 준비 후 매매 시작을 직접 누르세요. VM은 유지합니다.'
fi

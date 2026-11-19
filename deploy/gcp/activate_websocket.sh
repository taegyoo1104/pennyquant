#!/bin/bash
# Explicit pre-open switch after updating the reviewed image; never resets a ledger.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 && $(hostname -s) == sessionquant-paper && $(cat /sys/class/dmi/id/product_name 2>/dev/null) == 'Google Compute Engine' ]] || { echo '운영 VM sessionquant-paper에서 sudo로 실행하세요.' >&2; exit 1; }
cd /opt/sessionquant
exec 9>/run/sessionquant-update.lock
flock -n 9 || { echo '다른 업데이트·프로필 전환이 실행 중입니다.' >&2; exit 1; }
[[ ! -e /etc/sessionquant/maintenance && ! -e /etc/sessionquant/storage-blocked ]] || { echo '점검·저장 공간 차단을 먼저 확인하세요.' >&2; exit 1; }
compose=(docker compose -f deploy/gcp/compose.yaml)
helper=deploy/gcp/websocket_profile.py
sq_fingerprint=$(python3 "$helper" inspect)
if [[ "$sq_fingerprint" == ALREADY ]]; then
    echo '이미 웹소켓 프로필과 장부가 선택돼 있습니다. 설정을 다시 바꾸지 않습니다.'
    "${compose[@]}" ps
    exit 0
fi
check_window() {
    for unit in sessionquant-market-close.service sessionquant-cost-guard.service; do
        if systemctl is-active --quiet "$unit"; then echo '종료·백업 작업 중입니다.' >&2; return 1; fi
    done
    docker run --rm --network none sessionquant:local python -c '
from datetime import datetime, timezone
from pathlib import Path
from sessionquant.market.bars import session_bounds
from sessionquant.session_config import load_session_config
now=datetime.now(timezone.utc); bounds=session_bounds(now)
if not bounds or now>=bounds[0]: raise SystemExit("장전에 매매 시작 버튼을 누르기 전에 전환하세요.")
cfg=load_session_config(Path("config/sessionquant.strategy-lab-websocket.example.yaml"))
if cfg.quote_transport!="websocket": raise SystemExit("웹소켓 코드로 이미지를 먼저 업데이트하세요.")
' < /dev/null
}
check_window
sq_stopped=0
sq_switched=0
sq_backup=''
failed() {
    code=$?
    if [[ $(python3 "$helper" inspect 2>/dev/null || true) == ALREADY ]]; then sq_switched=1; fi
    echo '웹소켓 전환이 중단됐습니다. 위 오류를 확인하세요.' >&2
    if ((sq_stopped && !sq_switched)); then
        echo '기존 설정을 유지하고 원래 서비스를 다시 시작합니다.' >&2
        systemctl start sessionquant.service || echo '원래 서비스 재시작 실패: DB·백업 상태를 확인하세요.' >&2
    elif ((sq_switched)); then
        echo "새 장부를 숨기지 않도록 프로필을 유지합니다. 원래 설정 백업: $sq_backup/app.env" >&2
    fi
    exit "$code"
}
trap failed ERR
sq_stopped=1
systemctl stop sessionquant.service
"${compose[@]}" stop -t 180 app < /dev/null
"${compose[@]}" up -d --wait --wait-timeout 180 db < /dev/null
# Check ORIGINAL env/namespace after the writer has stopped, including WARMING starts.
"${compose[@]}" run --rm --no-deps -T app python - --stopped < deploy/gcp/update_guard.py
"${compose[@]}" run --rm --no-deps -T app python - probe-target < "$helper"
sq_before=$(cat /etc/sessionquant/backup-status 2>/dev/null || true)
bash deploy/gcp/backup.sh < /dev/null
sq_after=$(cat /etc/sessionquant/backup-status)
[[ "$sq_after" == UPLOADED\ * && "$sq_after" != "$sq_before" ]] || { echo '새 Cloud Storage 백업 성공 기록이 없습니다.' >&2; false; }
check_window
install -d -m 700 /var/lib/sessionquant/update-backups
sq_backup="/var/lib/sessionquant/update-backups/websocket-profile-$(date -u +%Y%m%dT%H%M%SZ)-$(cat /proc/sys/kernel/random/uuid)"
python3 "$helper" apply --expected "$sq_fingerprint" --backup "$sq_backup"
sq_switched=1
systemctl start sessionquant.service
systemctl is-active --quiet sessionquant-market-close.timer
systemctl is-active --quiet sessionquant-storage-guard.timer
systemctl is-active --quiet sessionquant-cost-guard.timer
"${compose[@]}" ps
systemctl list-timers --all 'sessionquant-*' --no-pager
echo '웹소켓 프로필 전환 완료. 대시보드에서 실제 수신·자료 준비를 확인한 뒤 매매 시작을 누르세요.'

#!/bin/bash
set -Eeuo pipefail
umask 077
cd /opt/sessionquant
compose=(docker compose -f deploy/gcp/compose.yaml)
if [[ -e /etc/sessionquant/maintenance ]]; then
    echo '점검 모드: 프로그램·장 마감 타이머를 시작하지 않습니다.'
    exit 0
fi
if systemctl is-active --quiet sessionquant-market-close.service || systemctl is-active --quiet sessionquant-cost-guard.service || systemctl is-active --quiet sessionquant-run-limit.service; then
    echo '마감 저장·백업이 진행 중입니다.' >&2
    exit 1
fi
# The VM remains available for inspection. Bound each program run instead of
# measuring from VM boot (which may have been several days ago).
systemctl disable --now sessionquant-cost-guard.timer
systemctl stop sessionquant-market-close.timer sessionquant-run-limit.timer 2>/dev/null || true
systemctl reset-failed sessionquant-market-close.service sessionquant-run-limit.service 2>/dev/null || true
# Pin to this boot's monotonic clock. OnActiveSec is rebased by daemon-reload;
# OnBootSec keeps the original instant even when host service files are reloaded.
shutdown_usec() {
    python3 - "$1" <<'PY_CLOCK'
import sys, time
print(time.monotonic_ns() // 1000 + int(sys.argv[1]) * 1_000_000)
PY_CLOCK
}
if ! delay=$(docker run --rm --network none sessionquant:local python -m sessionquant.market_shutdown --startup); then
    echo '거래소 달력 확인 실패: 매매를 시작하지 않고 점검 대시보드만 엽니다.' >&2
    bash deploy/gcp/inspect.sh
    exit 1
fi
[[ "$delay" =~ ^[0-9]+$ ]] || exit 1
if ((delay == 0)); then
    echo '휴장일 또는 오늘 종료 시각 이후입니다. 점검 대시보드로 시작합니다.'
    bash deploy/gcp/inspect.sh
    exit 0
fi
sq_close_usec=$(shutdown_usec "$delay")
systemd-run --unit=sessionquant-market-close --on-boot="${sq_close_usec}us" \
    --property=Type=oneshot --property=TimeoutStartSec=4200 \
    /opt/sessionquant/deploy/gcp/stop.sh
sq_limit_usec=$(shutdown_usec 72000)
systemd-run --unit=sessionquant-run-limit --on-boot="${sq_limit_usec}us" \
    --property=Type=oneshot --property=TimeoutStartSec=4200 \
    /opt/sessionquant/deploy/gcp/stop.sh
if [[ -e /etc/sessionquant/storage-blocked ]]; then
    echo '저장 공간 점검이 필요합니다. 복구·백업 확인 후 storage-blocked 표시를 해제하세요.' >&2
    exit 1
fi
python3 deploy/gcp/storage_check.py --minimum-mib 2048
if [[ -f /etc/sessionquant/backup-status ]] && ! grep -q '^UPLOADED ' /etc/sessionquant/backup-status; then
    echo '이전 백업이 완료되지 않았습니다. DB만 켜서 backup.sh를 성공시킨 후 재시작하세요.' >&2
    exit 1
fi
systemctl start sessionquant-storage-guard.timer
# Allow DB startup plus application grace/probes within the service deadline.
# Compose returns immediately once both services become healthy.
"${compose[@]}" up -d --wait --wait-timeout 480
echo '예열 후 대시보드에 접속해 매매 시작을 누르세요. 이번 실행의 장 마감·최대 20시간 종료를 예약했습니다. VM은 자동 종료하지 않습니다.'

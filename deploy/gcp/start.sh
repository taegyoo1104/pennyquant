#!/bin/bash
set -Eeuo pipefail
cd /opt/sessionquant
compose=(docker compose -f deploy/gcp/compose.yaml)
if systemctl is-active --quiet sessionquant-market-close.service || systemctl is-active --quiet sessionquant-cost-guard.service; then
    echo '마감 저장·백업이 진행 중입니다.' >&2
    exit 1
fi
# Boot-scoped timer; an explicit service restart must not extend today's deadline.
if ! delay=$(docker run --rm --network none sessionquant:local python -m sessionquant.market_shutdown); then
    echo '거래소 달력 확인 실패: 매매는 시작하지 않고 15분 후 VM을 종료합니다.' >&2
    systemd-run --unit=sessionquant-market-close --on-active=900s --property=Type=oneshot --property=TimeoutStartSec=900 /opt/sessionquant/deploy/gcp/stop.sh
    exit 1
fi
[[ "$delay" =~ ^[0-9]+$ ]] || exit 1
systemctl stop sessionquant-market-close.timer sessionquant-market-close.service 2>/dev/null || true
systemctl reset-failed sessionquant-market-close.service 2>/dev/null || true
systemd-run --unit=sessionquant-market-close --on-active="$((delay > 0 ? delay : 60))s" \
    --property=Type=oneshot --property=TimeoutStartSec=900 \
    /opt/sessionquant/deploy/gcp/stop.sh
if ((delay == 0)); then
    echo '휴장일 또는 오늘 종료 시각 이후입니다. 새 수집 없이 1분 후 VM을 종료합니다.'
    exit 0
fi
"${compose[@]}" up -d --wait --wait-timeout 180
echo '예열 후 SSH 터널로 대시보드에 접속해 매매 시작을 누르세요.'

#!/bin/bash
set -Eeuo pipefail
cd /opt/sessionquant
[[ ! -e /etc/sessionquant/maintenance ]] || exit 0
compose=(docker compose -f deploy/gcp/compose.yaml)
[[ $("${compose[@]}" ps --status running --services app) == app ]] || exit 0
if ! python3 deploy/gcp/storage_check.py --minimum-mib 1024; then
    install -d -m 700 /etc/sessionquant
    # Latch: freeing a little space must not automatically restart trading.
    touch /etc/sessionquant/storage-blocked
    chmod 600 /etc/sessionquant/storage-blocked
    echo '저장 공간 부족: 수집·모의매매를 종료하고 DB와 종료 예약을 유지합니다.' >&2
    "${compose[@]}" stop -t 180 app
    exit 1
fi

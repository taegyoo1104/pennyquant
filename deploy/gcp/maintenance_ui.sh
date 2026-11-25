#!/bin/bash
# File-only UI on the existing private/Tailscale port; no DB/API credentials.
set -Eeuo pipefail
[[ $# == 1 && ( $1 == start || $1 == stop ) ]] || exit 1
name=sessionquant-maintenance-ui
if docker container inspect "$name" >/dev/null 2>&1; then
    command=$(docker inspect "$name" --format '{{json .Config.Cmd}}')
    [[ "$command" == '["python","-m","sessionquant.maintenance_dashboard","--serve"]' ]] || exit 1
    docker rm -f "$name" >/dev/null
fi
if [[ $1 == start ]]; then
    install -d -o 1000 -g 1000 -m 755 /var/lib/sessionquant/app/maintenance
    # Never display a previous run as this run's completed work.
    rm -f /var/lib/sessionquant/app/maintenance/progress.json
    docker run --rm -d --name "$name" --init --network host --read-only --tmpfs /tmp \
        --cap-drop ALL --security-opt no-new-privileges --log-opt max-size=5m --log-opt max-file=2 \
        -e STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
        -v /var/lib/sessionquant/app/maintenance:/app/maintenance:ro \
        sessionquant:local python -m sessionquant.maintenance_dashboard --serve >/dev/null
fi

#!/bin/bash
# Called only after this shutdown's backup succeeds; never from backup.sh.
set -Eeuo pipefail
cd /opt/sessionquant
compose=(docker compose -f deploy/gcp/compose.yaml)
status=/etc/sessionquant/backup-status
report=/var/lib/sessionquant/app/retention-last.json
[[ $# == 1 ]] || { echo '이번 종료 직전의 백업 상태가 필요합니다.' >&2; exit 1; }
previous=$1
current=$(cat "$status")
if [[ "$current" != UPLOADED\ * || "$current" == "$previous" ]]; then
    echo '이번 종료의 새 백업 업로드를 확인하지 못했습니다. 자료 정리를 생략합니다.' >&2
    exit 1
fi
if ! app_services=$("${compose[@]}" ps --status running --services app); then
    echo '프로그램 실행 상태 조회 실패: 자료를 삭제하지 않습니다.' >&2
    exit 1
fi
if [[ -n "$app_services" ]]; then
    echo '프로그램이 실행 중입니다. 자료를 삭제하지 않습니다.' >&2
    exit 1
fi
umask 077
container="sessionquant-retention-$(cat /proc/sys/kernel/random/uuid)"
part=$(mktemp /var/lib/sessionquant/app/.retention.XXXXXXXX)
proof=''
cleanup() {
    local code=$?
    trap - EXIT
    # Stopping the docker client alone does not stop its one-off container.
    timeout --kill-after=2 8 docker rm -f "$container" >/dev/null 2>&1 || true
    rm -f -- "$part"
    [[ -z "$proof" ]] || rm -f -- "$proof"
    if ((code != 0)); then
        printf 'FAILED %s exit_code=%s\n' "$(date -u +%FT%TZ)" "$code" > /etc/sessionquant/retention-status
    fi
    exit "$code"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
# The normal app runs as uid 1000; /etc/sessionquant/backup-status is root-only.
# Share only this non-secret receipt, never the configuration directory.
proof=$(mktemp /var/lib/sessionquant/app/.retention-proof.XXXXXXXX)
printf '%s\n' "$current" > "$proof"
chown 1000:1000 "$proof"
printf 'RUNNING %s\n' "$(date -u +%FT%TZ)" > /etc/sessionquant/retention-status
"${compose[@]}" run --rm --no-deps -T --name "$container" \
    --volume "$proof:/run/sessionquant-backup-status:ro" app \
    sh -c 'exec python -m sessionquant data-retention --config "${SQ_SESSION_CONFIG:-config/sessionquant.realtime-paper.example.yaml}" --apply --backup-receipt /run/sessionquant-backup-status' > "$part" < /dev/null &
worker=$!
wait "$worker"
# Validate a bounded, content-free report before replacing the previous result.
summary=$(python3 - "$part" <<'PY'
import json,sys
with open(sys.argv[1],encoding='utf-8') as stream:
    report=json.load(stream)
status=report['status']
if status not in {'COMPLETE','PARTIAL','DISABLED'} or report['dry_run'] is not False:
    raise ValueError('자료 정리 결과를 확인할 수 없습니다.')
print(f"{status} scanned={report['scanned']} deleted={report['deleted']}")
PY
)
chown 1000:1000 "$part"
mv -- "$part" "$report"
printf '%s %s\n' "$summary" "$(date -u +%FT%TZ)" > /etc/sessionquant/retention-status
echo "오래된 자료 정리: $summary · 상세 /var/lib/sessionquant/app/retention-last.json"

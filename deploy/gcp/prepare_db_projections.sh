#!/bin/bash
# Explicit opt-in. No app stop/start, source deletion, backup, or DB unsealing.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 && $# -ge 1 && $1 == --apply ]] || {
    echo 'sudo bash deploy/gcp/prepare_db_projections.sh --apply [--seconds 60] [--restart]' >&2; exit 1;
}
[[ $(hostname -s) == sessionquant-paper ]] || { echo 'sessionquant-paper에서 실행하세요.' >&2; exit 1; }
shift
seconds=60
restart=()
while (($#)); do
    case "$1" in
        --seconds) [[ $# -ge 2 && $2 =~ ^[0-9]+$ && $2 -ge 1 && $2 -le 300 ]] || exit 1
            seconds=$2; shift 2 ;;
        --restart) restart=(--restart); shift ;;
        *) echo '지원하지 않는 옵션입니다.' >&2; exit 1 ;;
    esac
done
cd /opt/sessionquant
source deploy/gcp/lifecycle_lock.sh
exec 9>/run/sessionquant-backup.lock
flock -n 9 || { echo '백업 작업이 진행 중입니다.' >&2; exit 1; }
[[ ! -e /etc/sessionquant/maintenance && ! -e /etc/sessionquant/storage-blocked ]] || {
    echo '저장 공간·복구 점검 표시를 먼저 확인하세요.' >&2; exit 1;
}
compose=(docker compose -f deploy/gcp/compose.yaml)
active=$("${compose[@]}" ps --status running --services app)
if [[ -n "$active" ]]; then
    command=$(docker inspect sessionquant-app-1 --format '{{json .Config.Cmd}}')
    [[ "$active" == app && "$command" == '["python","-m","sessionquant.inspection_dashboard"]' ]] || {
        echo '매매 프로그램이 실행 중입니다. 종료된 점검 상태에서 실행하세요.' >&2; exit 1;
    }
fi
install -d -m 755 /var/lib/sessionquant/health
part=$(mktemp /var/lib/sessionquant/health/.projections.XXXXXXXX)
container="sessionquant-projections-$(cat /proc/sys/kernel/random/uuid)"
stop_worker() {
    local found
    found=$(timeout --kill-after=2 8 docker ps -a --filter "name=^${container}$" --format '{{.Names}}') || return 1
    if [[ -n "$found" ]]; then
        [[ "$found" == "$container" ]] || return 1
        timeout --kill-after=2 8 docker rm -f "$container" >/dev/null 2>&1 || return 1
    fi
    found=$(timeout --kill-after=2 8 docker ps -a --filter "name=^${container}$" --format '{{.Names}}') || return 1
    [[ -z "$found" ]]
}
block_uncertain_worker() {
    # Future lifecycle/CD jobs must not begin until the worker is known stopped.
    printf '%s\n' "$container" > /etc/sessionquant/projection-preparation-blocked
    printf '%s\n' '{"status":"FAILED","reason":"WORKER_STOP_UNCONFIRMED"}' > "$part"
    mv -f -- "$part" /var/lib/sessionquant/health/db-projections-last.json
    echo '조회 요약 작업의 종료를 확인하지 못했습니다. 유지보수 차단 표시를 남겼습니다.' >&2
}
cleanup() {
    local result=$?
    trap - EXIT
    if ! stop_worker; then block_uncertain_worker; result=1; fi
    rm -f -- "$part"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
set +e
timeout --signal=TERM --kill-after=10 "$((seconds + 30))" "${compose[@]}" run --rm --no-deps -T --name "$container" \
    --volume /opt/sessionquant/deploy/gcp:/run/sessionquant-projections:ro app \
    python /run/sessionquant-projections/prepare_db_projections.py --apply --seconds "$seconds" "${restart[@]}" > "$part" < /dev/null
code=$?
set -e
if ! stop_worker; then block_uncertain_worker; exit 1; fi
# Publish only a bounded, structured result; malformed output is a new failure.
python3 - "$part" "$code" <<'PY'
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
path=Path(sys.argv[1]); code=int(sys.argv[2])
if code in (124,137):
    result={'status':'PENDING','reason':'PROCESS_TIME_LIMIT','checked_at':datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(result)+'\n')
else:
    try:
        if path.stat().st_size > 65536:
            raise ValueError('report size')
        result=json.loads(path.read_text())
        if not isinstance(result,dict) or result.get('status') not in {'COMPLETE','PENDING','FAILED'}:
            raise ValueError('report status')
        if code and result['status'] != 'FAILED':
            raise ValueError('process failed')
    except (OSError,ValueError,TypeError):
        result={'status':'FAILED','reason':'INVALID_WORKER_RESULT','exit_code':code}
        path.write_text(json.dumps(result)+'\n')
path.replace('/var/lib/sessionquant/health/db-projections-last.json')
print(json.dumps(result,ensure_ascii=False))
if result['status']=='FAILED':
    raise SystemExit(1)
PY
if ((code != 0 && code != 124 && code != 137)); then exit "$code"; fi

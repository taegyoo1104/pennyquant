#!/bin/bash
set -uo pipefail
exec 9>/run/sessionquant-stop.lock
flock -n 9 || exit 0
cd /opt/sessionquant || exit 1
compose=(docker compose -f deploy/gcp/compose.yaml)
echo '장 마감: 프로그램 저장·종료 후 백업합니다. 잔량이 있으면 다음 실행에서 복구합니다.'
"${compose[@]}" stop -t 180 app || echo '프로그램 종료 상태를 확인하세요.' >&2
if [[ $("${compose[@]}" ps --status running --services db) == db ]]; then
    if ! timeout 480 /opt/sessionquant/deploy/gcp/backup.sh; then
        # Also covers an early refusal, timeout, or a killed backup process.
        install -d -m 700 /etc/sessionquant
        (umask 077; printf 'FAILED %s\n' "$(date -u +%FT%TZ)" > /etc/sessionquant/backup-status) || true
        echo '백업 실패: VM의 DB 디스크는 유지됩니다. 다음 시작 시 로그를 확인하세요.' >&2
    fi
else
    echo '실행 중인 DB가 없습니다. 기존 디스크를 보존하고 종료합니다.'
fi
# Never remove volumes or the persistent boot disk.
"${compose[@]}" down --timeout 120 || echo '컨테이너 종료 오류: OS 종료로 마무리합니다.' >&2
sync
/usr/bin/systemctl poweroff

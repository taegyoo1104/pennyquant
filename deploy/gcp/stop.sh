#!/bin/bash
set -uo pipefail
exec 9>/run/sessionquant-stop.lock
flock -n 9 || exit 0
cd /opt/sessionquant || exit 1
compose=(docker compose -f deploy/gcp/compose.yaml)
result=0
systemctl stop sessionquant-market-close.timer sessionquant-run-limit.timer sessionquant-cost-guard.timer || true
echo '장 마감: 프로그램 저장·종료 후 백업합니다. 잔량이 있으면 다음 실행에서 복구합니다. VM은 유지합니다.'
"${compose[@]}" stop -t 180 app || echo '프로그램 종료 상태를 확인하세요.' >&2
db_services=$("${compose[@]}" ps --status running --services db)
db_status=$?
if ((db_status != 0)); then
    result=1
    install -d -m 700 /etc/sessionquant
    (umask 077; printf 'FAILED %s phase=DB_STATUS exit_code=%s\n' \
        "$(date -u +%FT%TZ)" "$db_status" > /etc/sessionquant/backup-status) || true
    echo 'DB 실행 상태 조회 실패: 이번 백업을 확인하지 못했습니다. 기존 디스크를 보존하고 종료합니다.' >&2
elif [[ "$db_services" == db ]]; then
    # Reserve time inside the 70-minute service for app stop, a 45-minute
    # backup, bounded cleanup, DB stop and disk flush. Never report timeout
    # as a successful upload.
    backup_status_before=$(cat /etc/sessionquant/backup-status 2>/dev/null || echo MISSING)
    timeout --kill-after=30 2700 /opt/sessionquant/deploy/gcp/backup.sh
    backup_result=$?
    if ((backup_result != 0)); then
        result=1
        # Also covers an early refusal, timeout, or a killed backup process.
        install -d -m 700 /etc/sessionquant
        # Keep the stage/error recorded by backup.sh; cover hard kills and early
        # failures separately so an old UPLOADED status cannot permit restart.
        if ! grep -q '^FAILED ' /etc/sessionquant/backup-status 2>/dev/null || \
            [[ $(cat /etc/sessionquant/backup-status 2>/dev/null) == "$backup_status_before" ]]; then
            (umask 077; printf 'FAILED %s phase=SHUTDOWN exit_code=%s\n' \
                "$(date -u +%FT%TZ)" "$backup_result" > /etc/sessionquant/backup-status) || true
        fi
        if ((backup_result == 124 || backup_result == 137)); then
            echo '백업 제한 시간 2700초 초과: 미완료 상태를 남기고 DB 디스크를 보존합니다.' >&2
        else
            echo "백업 실패: 종료 코드 $backup_result. DB 디스크는 유지됩니다." >&2
        fi
    else
        # At most five passes within 600+15 seconds; each pass retains its
        # own 90-second/row budget and verified-backup checks.
        # A failed backup must never be followed by data deletion.
        timeout --kill-after=15 600 bash /opt/sessionquant/deploy/gcp/retention_after_update.sh "$backup_status_before"
        retention_result=$?
        if ((retention_result != 0)); then
            result=1
            (umask 077; printf 'FAILED %s exit_code=%s\n' "$(date -u +%FT%TZ)" "$retention_result" \
                > /etc/sessionquant/retention-status) || true
            echo '자료 정리가 끝나지 않았습니다. 성공한 백업을 유지하고 점검 화면에서 확인하세요.' >&2
        fi
    fi
else
    echo '실행 중인 DB가 없습니다. 기존 디스크를 보존합니다.'
fi
# Never remove volumes or the persistent boot disk.
"${compose[@]}" down --timeout 120 || { echo '컨테이너 종료 오류: 점검 화면을 시작하지 않습니다.' >&2; exit 1; }
sync
bash deploy/gcp/inspect.sh || result=1
# An available inspection dashboard is not proof of a successful backup.
exit "$result"

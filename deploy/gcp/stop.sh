#!/bin/bash
set -uo pipefail
exec 9>/run/sessionquant-stop.lock
flock -n 9 || exit 0
exec 7>/run/sessionquant-update.lock
flock -n 7 || { echo "업데이트 중입니다. 마감 정리를 동시에 실행하지 않습니다." >&2; exit 1; }
cd /opt/sessionquant || exit 1
compose=(docker compose -f deploy/gcp/compose.yaml)
result=0
systemctl stop sessionquant-market-close.timer sessionquant-run-limit.timer sessionquant-cost-guard.timer || true
echo '장 마감: 저장·종료 → 백업 → 자료 정리 → 최종 백업을 진행합니다. 전체 최대 4시간 30분이며 VM은 유지합니다.'
"${compose[@]}" stop -t 180 app || { echo '프로그램 종료 실패: 자료를 변경하지 않습니다.' >&2; exit 1; }
python3 deploy/gcp/nightly_checkpoint.py writable || exit 1
db_services=$("${compose[@]}" ps --status running --services db)
db_status=$?
if ((db_status != 0)); then
    result=1
    install -d -m 700 /etc/sessionquant
    (umask 077; printf 'FAILED %s phase=DB_STATUS exit_code=%s\n' \
        "$(date -u +%FT%TZ)" "$db_status" > /etc/sessionquant/backup-status) || true
    echo 'DB 실행 상태 조회 실패: 이번 백업을 확인하지 못했습니다. 기존 디스크를 보존하고 종료합니다.' >&2
elif [[ "$db_services" == db ]]; then
    # Two 90-minute backups, 45-minute cleanup and ample shutdown grace
    # fit inside the 4.5-hour service. Never treat timeout as success.
    backup_status_before=$(cat /etc/sessionquant/backup-status 2>/dev/null || echo MISSING)
    timeout --kill-after=30 5400 /opt/sessionquant/deploy/gcp/backup.sh
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
            echo '백업 제한 시간 5400초 초과: 미완료 상태를 남기고 DB 디스크를 보존합니다.' >&2
        else
            echo "백업 실패: 종료 코드 $backup_result. DB 디스크는 유지됩니다." >&2
        fi
    else
        # Keep per-query/per-pass bounds; allow up to 45 minutes overall.
        # The backup receipt stays within the existing one-hour deletion rule.
        timeout --kill-after=15 2700 bash /opt/sessionquant/deploy/gcp/retention_after_update.sh "$backup_status_before" --nightly
        retention_result=$?
        if ((retention_result != 0)); then
            result=1
            (umask 077; printf 'FAILED %s exit_code=%s\n' "$(date -u +%FT%TZ)" "$retention_result" \
                > /etc/sessionquant/retention-status) || true
            echo '자료 정리가 끝나지 않았습니다. 성공한 백업을 유지하고 점검 화면에서 확인하세요.' >&2
        else
            # Freeze before the final compact logical backup. Inspection keeps
            # this DB instance running; a restart invalidates reuse.
            if python3 deploy/gcp/nightly_checkpoint.py seal &&
                timeout --kill-after=30 5400 bash /opt/sessionquant/deploy/gcp/backup.sh &&
                python3 deploy/gcp/nightly_checkpoint.py record; then
                echo '야간 정리·최종 백업 완료. 다음 업데이트는 변경이 없으면 백업·정리를 생략합니다.'
            else
                result=1
                echo '최종 백업·완료 증명 실패: 낮 업데이트에서 재사용하지 않습니다. 이전 백업은 보존됩니다.' >&2
            fi
        fi
    fi
else
    echo '실행 중인 DB가 없습니다. 기존 디스크를 보존합니다.'
fi
# Keep the sealed DB running for read-only inspection and backup reuse.
# Volumes and disks are never removed; failed work never gets a checkpoint.
sync
bash deploy/gcp/inspect.sh || result=1
# An available inspection dashboard is not proof of a successful backup.
exit "$result"

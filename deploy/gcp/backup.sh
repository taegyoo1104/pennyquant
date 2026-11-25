#!/bin/bash
set -Eeuo pipefail
umask 077
exec 8>/run/sessionquant-backup.lock
flock -n 8 || { echo '다른 백업이 실행 중입니다.' >&2; exit 1; }
cd /opt/sessionquant
status_file=/etc/sessionquant/backup-status
if app_services=$(docker compose -f deploy/gcp/compose.yaml ps --status running --services app); then
    :
else
    query_status=$?
    install -d -m 700 /etc/sessionquant
    printf 'FAILED %s phase=APP_STATUS exit_code=%s\n' \
        "$(date -u +%FT%TZ)" "$query_status" > "$status_file" || true
    echo '프로그램 실행 상태 조회 실패: 백업을 시작하지 않습니다.' >&2
    exit "$query_status"
fi
if [[ "$app_services" == app ]]; then
    echo '일관된 백업을 위해 먼저 app 컨테이너를 종료하세요.' >&2
    exit 1
fi
backup_dir=/var/lib/sessionquant/backups
install -d -m 700 "$backup_dir"
install -d -m 700 /etc/sessionquant
printf 'RUNNING %s\n' "$(date -u +%FT%TZ)" > "$status_file"
tmp=''
name=''
phase=PREPARE
phase_started=$SECONDS
phase_begin() {
    phase=$1
    phase_started=$SECONDS
    state=RUNNING
    [[ "$phase" != UPLOAD ]] || state=LOCAL_ONLY
    printf '%s %s phase=%s elapsed_seconds=%s archive=%s\n' \
        "$state" "$(date -u +%FT%TZ)" "$phase" "$SECONDS" "${name:--}" > "$status_file"
    printf '백업 단계 시작: %s · %s\n' "$phase" "$(date -u +%FT%TZ)"
}
phase_end() {
    printf '백업 단계 완료: %s · 단계 %s초 · 전체 %s초\n' "$phase" "$((SECONDS-phase_started))" "$SECONDS"
}
cleanup() {
    result=$?
    if ((result != 0)); then
        printf 'FAILED %s phase=%s exit_code=%s elapsed_seconds=%s archive=%s\n' \
            "$(date -u +%FT%TZ)" "$phase" "$result" "$SECONDS" "${name:--}" > "$status_file" || true
        echo "백업 실패: 단계 $phase · 종료 코드 $result · 전체 $SECONDS초. 이전 백업은 보존했습니다." >&2
    fi
    [[ -z "$tmp" ]] || rm -rf -- "$tmp"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
python3 deploy/gcp/storage_check.py --minimum-mib 512
phase_begin CAPACITY
python3 deploy/gcp/backup_capacity.py check
phase_end
tmp=$(mktemp -d "$backup_dir/.working.XXXXXXXX")
phase_begin DUMP
docker compose -f deploy/gcp/compose.yaml exec -T db sh -c \
    'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump -u "$MYSQL_USER" --single-transaction --quick --no-tablespaces --set-gtid-purged=OFF "$MYSQL_DATABASE"' < /dev/null | gzip -1 | python3 deploy/gcp/backup_capacity.py dump "$tmp/database.sql.gz.part"
mv "$tmp/database.sql.gz.part" "$tmp/database.sql.gz"
phase_end
printf '압축 DB 크기: %s bytes\n' "$(stat -c %s "$tmp/database.sql.gz")"
phase_begin VERIFY_SQL
gzip -t "$tmp/database.sql.gz"
# Reject empty output even when gzip itself succeeds.
python3 - "$tmp/database.sql.gz" <<'PY'
import gzip, sys
with gzip.open(sys.argv[1], 'rb') as stream:
    if not stream.read(1):
        raise SystemExit('빈 SQL 백업입니다.')
PY
phase_end
name="sessionquant-$(date -u +%Y%m%dT%H%M%SZ)-$(cat /proc/sys/kernel/random/uuid).tar.gz"
# Only compressed SQL is staged; no potentially multi-GB plaintext dump.
phase_begin PACKAGE
python3 deploy/gcp/backup_capacity.py package "$tmp/database.sql.gz"
python3 deploy/gcp/package_backup.py "$tmp/database.sql.gz" /var/lib/sessionquant/app "$tmp/$name"
phase_end
phase_begin VERIFY_ARCHIVE
gzip -t "$tmp/$name"
phase_end
chmod 600 "$tmp/$name"
mv "$tmp/$name" "$backup_dir/$name"
echo "로컬 백업 완료: $name"
printf 'LOCAL_ONLY %s %s\n' "$(date -u +%FT%TZ)" "$name" > "$status_file"
phase_begin UPLOAD
python3 deploy/gcp/upload_backup.py "$backup_dir/$name"
phase_end
printf 'UPLOADED %s %s\n' "$(date -u +%FT%TZ)" "$name" > "$status_file"
# Prune only after a verified upload, never remove the previous backup to make room.
python3 - "$backup_dir" <<'PY' || echo '백업 업로드는 완료됐지만 이전 로컬 백업 정리가 필요합니다.' >&2
from pathlib import Path
import sys
for path in sorted(Path(sys.argv[1]).glob('sessionquant-*.tar.gz'), reverse=True)[3:]:
    path.unlink()
PY
printf '백업 완료: 전체 %s초\n' "$SECONDS"

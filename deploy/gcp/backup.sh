#!/bin/bash
set -Eeuo pipefail
umask 077
exec 8>/run/sessionquant-backup.lock
flock -n 8 || { echo '다른 백업이 실행 중입니다.' >&2; exit 1; }
cd /opt/sessionquant
if [[ $(docker compose -f deploy/gcp/compose.yaml ps --status running --services app) == app ]]; then
    echo '일관된 백업을 위해 먼저 app 컨테이너를 종료하세요.' >&2
    exit 1
fi
backup_dir=/var/lib/sessionquant/backups
install -d -m 700 "$backup_dir"
status_file=/etc/sessionquant/backup-status
install -d -m 700 /etc/sessionquant
printf 'RUNNING %s\n' "$(date -u +%FT%TZ)" > "$status_file"
tmp=''
cleanup() {
    result=$?
    if ((result != 0)); then
        printf 'FAILED %s\n' "$(date -u +%FT%TZ)" > "$status_file" || true
        echo '백업 실패: 이전 백업은 보존했습니다. /etc/sessionquant/backup-status를 확인하세요.' >&2
    fi
    [[ -z "$tmp" ]] || rm -rf -- "$tmp"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
python3 deploy/gcp/storage_check.py --minimum-mib 512
tmp=$(mktemp -d "$backup_dir/.working.XXXXXXXX")
docker compose -f deploy/gcp/compose.yaml exec -T db sh -c \
    'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump -u "$MYSQL_USER" --single-transaction --quick --no-tablespaces --set-gtid-purged=OFF "$MYSQL_DATABASE"' < /dev/null | gzip -1 > "$tmp/database.sql.gz"
gzip -t "$tmp/database.sql.gz"
# Reject empty output even when gzip itself succeeds.
python3 - "$tmp/database.sql.gz" <<'PY'
import gzip, sys
with gzip.open(sys.argv[1], 'rb') as stream:
    if not stream.read(1):
        raise SystemExit('빈 SQL 백업입니다.')
PY
name="sessionquant-$(date -u +%Y%m%dT%H%M%SZ)-$(cat /proc/sys/kernel/random/uuid).tar.gz"
# Only compressed SQL is staged; no potentially multi-GB plaintext dump.
tar -czf "$tmp/$name" -C "$tmp" database.sql.gz -C /var/lib/sessionquant app
gzip -t "$tmp/$name"
chmod 600 "$tmp/$name"
mv "$tmp/$name" "$backup_dir/$name"
echo "로컬 백업 완료: $name"
printf 'LOCAL_ONLY %s %s\n' "$(date -u +%FT%TZ)" "$name" > "$status_file"
python3 deploy/gcp/upload_backup.py "$backup_dir/$name"
printf 'UPLOADED %s %s\n' "$(date -u +%FT%TZ)" "$name" > "$status_file"
# Prune only after a verified upload, never remove the previous backup to make room.
python3 - "$backup_dir" <<'PY'
from pathlib import Path
import sys
for path in sorted(Path(sys.argv[1]).glob('sessionquant-*.tar.gz'), reverse=True)[3:]:
    path.unlink()
PY

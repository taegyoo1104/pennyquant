#!/bin/bash
set -Eeuo pipefail
cd /opt/sessionquant
if [[ $(docker compose -f deploy/gcp/compose.yaml ps --status running --services app) == app ]]; then
    echo '일관된 백업을 위해 먼저 app 컨테이너를 종료하세요.' >&2
    exit 1
fi
backup_dir=/var/lib/sessionquant/backups
install -d -m 700 "$backup_dir"
tmp=$(mktemp -d "$backup_dir/.working.XXXXXXXX")
trap 'rm -rf -- "$tmp"' EXIT
docker compose -f deploy/gcp/compose.yaml exec -T db sh -c \
    'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump -u "$MYSQL_USER" --single-transaction --quick --no-tablespaces --set-gtid-purged=OFF "$MYSQL_DATABASE"' > "$tmp/database.sql"
test -s "$tmp/database.sql"
name="sessionquant-$(date -u +%Y%m%dT%H%M%SZ)-$(cat /proc/sys/kernel/random/uuid).tar.gz"
tar -czf "$tmp/$name" -C "$tmp" database.sql -C /var/lib/sessionquant app
gzip -t "$tmp/$name"
chmod 600 "$tmp/$name"
mv "$tmp/$name" "$backup_dir/$name"
echo "로컬 백업 완료: $name"
# Keep three completed archives locally even if the network upload fails.
python3 - "$backup_dir" <<'PY'
from pathlib import Path
import sys
for path in sorted(Path(sys.argv[1]).glob('sessionquant-*.tar.gz'), reverse=True)[3:]:
    path.unlink()
PY
python3 deploy/gcp/upload_backup.py "$backup_dir/$name"

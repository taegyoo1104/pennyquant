#!/bin/bash
set -Eeuo pipefail
[[ $# == 1 && -s "$1" ]] || { echo '사용법: sudo deploy/gcp/import_database.sh SQL덤프파일(.sql 또는 .sql.gz)' >&2; exit 1; }
dump=$(realpath "$1")
cd /opt/sessionquant
compose=(docker compose -f deploy/gcp/compose.yaml)
if [[ $("${compose[@]}" ps --status running --services app) == app ]]; then
    echo '앱을 먼저 종료하세요.' >&2
    exit 1
fi
tables=$("${compose[@]}" exec -T db sh -c \
    'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -N -B -u "$MYSQL_USER" "$MYSQL_DATABASE" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()"')
[[ "$tables" == 0 ]] || { echo '빈 DB가 아닙니다. 기존 자료를 덮어쓰지 않습니다.' >&2; exit 1; }
if [[ "$dump" == *.gz ]]; then
    gzip -t "$dump"
    gzip -dc "$dump" | "${compose[@]}" exec -T db sh -c \
        'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql --binary-mode -u "$MYSQL_USER" "$MYSQL_DATABASE"'
else
    "${compose[@]}" exec -T db sh -c \
        'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql --binary-mode -u "$MYSQL_USER" "$MYSQL_DATABASE"' < "$dump"
fi
echo 'DB 가져오기 완료. 분봉·장부 상태를 확인하세요.'

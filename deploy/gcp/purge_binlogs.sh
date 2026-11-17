#!/bin/bash
# Destructive to binlog history, not table data. Requires a verified pre-repair snapshot.
set -Eeuo pipefail
[[ ${1:-} == --snapshot-confirmed ]] || {
    echo '검증된 복구 스냅샷이 있는 단일 Paper DB에서만 --snapshot-confirmed로 실행하세요.' >&2
    exit 1
}
[[ -e /etc/sessionquant/maintenance ]] || { echo '점검 모드가 아닙니다.' >&2; exit 1; }
cd /opt/sessionquant
compose=(docker compose -f deploy/gcp/compose.yaml -f deploy/gcp/compose.maintenance.yaml)
[[ $("${compose[@]}" ps --status running --services app) != app ]] || { echo '앱을 먼저 종료하세요.' >&2; exit 1; }
"${compose[@]}" exec -T db sh -eu <<'SH'
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
sql() { mysql -u root --batch --skip-column-names -e "$1"; }
test "$(sql 'SELECT @@GLOBAL.log_bin')" = 1 || { echo '변경 기록을 켠 점검 DB가 필요합니다.' >&2; exit 1; }
test "$(sql 'SELECT @@GLOBAL.gtid_mode')" = OFF || { echo 'GTID 구성은 자동 정리하지 않습니다.' >&2; exit 1; }
test "$(sql 'SELECT COUNT(*) FROM mysql.slave_master_info')" = 0 || { echo '복제 구성이 있어 중단합니다.' >&2; exit 1; }
test -z "$(sql 'SHOW PROCESSLIST' | awk 'tolower($0) ~ /binlog dump/ {print}')" || { echo '복제 연결이 있어 중단합니다.' >&2; exit 1; }
sql 'FLUSH BINARY LOGS'
current=$(sql 'SHOW BINARY LOG STATUS' | cut -f1)
case "$current" in
    binlog.[0-9][0-9][0-9][0-9][0-9][0-9]) ;;
    *) echo '변경 기록 파일명을 확인할 수 없습니다.' >&2; exit 1 ;;
esac
sql "PURGE BINARY LOGS TO '$current'"
sql 'SHOW BINARY LOGS'
SH
echo '이전 변경 기록 정리 완료. 일반 Compose로 DB를 재생성하고 log_bin=0을 확인하세요.'

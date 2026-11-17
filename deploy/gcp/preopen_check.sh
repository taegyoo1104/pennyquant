#!/bin/bash
# Read-only checks. Never submit orders or request an LLM generation.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 && $(hostname -s) == sessionquant-paper ]] || { echo '운영 VM에서 sudo로 실행하세요.' >&2; exit 1; }
cd /opt/sessionquant
python3 deploy/gcp/storage_check.py --minimum-mib 2048
compose=(docker compose -f deploy/gcp/compose.yaml)
sq_values=$("${compose[@]}" exec -T db sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B -e "SELECT @@GLOBAL.log_bin, @@GLOBAL.max_allowed_packet, (SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE())" "$MYSQL_DATABASE"' < /dev/null)
read -r sq_binlog sq_packet sq_tables <<< "$sq_values"
[[ "$sq_binlog" == 0 && "$sq_packet" =~ ^[0-9]+$ && "$sq_tables" =~ ^[0-9]+$ ]] || { echo 'DB 설정을 확인할 수 없습니다.' >&2; exit 1; }
((sq_packet >= 67108864 && sq_tables >= 43)) || { echo 'DB 전송 크기·테이블 준비를 확인하세요.' >&2; exit 1; }
grep -q '^UPLOADED ' /etc/sessionquant/backup-status || { echo '완료된 클라우드 백업이 없습니다.' >&2; exit 1; }
sq_report=$(mktemp /var/tmp/sq-toss-check.XXXXXXXX)
trap 'rm -f -- "$sq_report"' EXIT
sq_toss_exit=0
"${compose[@]}" run --rm --no-deps -T app python -m sessionquant check-toss --symbol AAPL < /dev/null > "$sq_report" || sq_toss_exit=$?
python3 - "$sq_report" "$sq_toss_exit" <<'PY'
import json,sys
try:
    with open(sys.argv[1]) as source:data=json.load(source)
    if not isinstance(data,dict):raise ValueError()
except (OSError,ValueError):
    raise SystemExit('토스 점검 결과를 읽지 못했습니다. 위 컨테이너 오류를 확인하세요.')
if sys.argv[2]!='0' or data.get('success') is not True or data.get('live_orders_enabled') is not False:
    error=data.get('error')
    code=error.get('http_status') if isinstance(error,dict) else None
    detail=f' (HTTP {code})' if type(code) is int and 100<=code<=599 else ''
    print('토스 조회 실패'+detail+'. 인증·등록 IP·API 권한을 확인하세요.')
    raise SystemExit(1)
print('토스 인증·현재가 조회 성공. 장전 가격은 최신 장중 호가 검증과 별개입니다.')
PY
echo 'DB·저장 공간·백업·토스 조회 점검 통과. 실제 주문·AI 분석 요청은 하지 않았습니다.'

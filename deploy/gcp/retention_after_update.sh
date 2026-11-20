#!/bin/bash
# Catch up after this update's backup; never restart the app or rebuild tables.
set -Eeuo pipefail
cd /opt/sessionquant
[[ $# == 1 ]] || { echo '업데이트 직전의 백업 상태가 필요합니다.' >&2; exit 1; }
for sq_pass in 1 2 3 4 5; do
    echo "업데이트 후 자료 정리: $sq_pass / 5"
    timeout --kill-after=15 120 bash deploy/gcp/retention.sh "$1" < /dev/null
    sq_pending=$(python3 - /var/lib/sessionquant/app/retention-last.json <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as stream:
    report = json.load(stream)
if report.get('dry_run') is not False or report.get('status') not in {'COMPLETE','PARTIAL','DISABLED'}:
    raise SystemExit('이번 자료 정리 결과를 확인할 수 없습니다.')
if report.get('errors'):
    raise SystemExit('자료 정리 조회 오류: '+', '.join(item['table']+' (코드 '+str(item['code'])+')' for item in report['errors']))
print('yes' if report['status']=='PARTIAL' or report.get('observability',{}).get('backlog_status')=='PRESENT' else 'no')
PY
)
    if [[ "$sq_pending" == no ]]; then exit 0; fi
done
echo '자료 정리 5회 한도에 도달했습니다. 남은 대상은 시스템·설정에서 확인하세요. DB 파일 축소는 수행하지 않았습니다.'

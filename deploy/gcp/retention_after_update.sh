#!/bin/bash
# One connection/engine lease for the entire stopped-runtime maintenance window.
set -Eeuo pipefail
cd /opt/sessionquant
[[ $# == 1 || ( $# == 2 && $2 == --nightly ) ]] || exit 1
sq_seconds=600
if [[ ${2:-} == --nightly ]]; then sq_seconds=${SQ_MAINTENANCE_SECONDS:-2700}; fi
[[ $sq_seconds =~ ^[0-9]+$ ]] && ((sq_seconds >= 1 && sq_seconds <= 21600)) || exit 1
bash deploy/gcp/retention.sh "$1" "$sq_seconds"
# The worker validates the fresh receipt once and keeps the lease until done.
# No timestamp refresh, fresh backup loop, or unverified skipped row.
python3 - "${2:-}" <<'PY_REPORT'
import json,sys
with open('/var/lib/sessionquant/app/retention-last.json') as stream: r=json.load(stream)
if r.get('dry_run') is not False or r.get('errors') or r.get('status') not in {'COMPLETE','PARTIAL','DISABLED'}:
    raise SystemExit('자료 정리 결과를 확인하지 못했습니다.')
if r['status']=='PARTIAL':
    print('정리 시간 안에 모두 끝나지 않았습니다. 진행 위치와 미완료 상태를 보존합니다.')
    if sys.argv[1]=='--nightly': raise SystemExit(2)
PY_REPORT

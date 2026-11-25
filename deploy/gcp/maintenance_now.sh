#!/bin/bash
# Explicit off-hours catch-up. The usual automatic close keeps its 45-min budget.
set -Eeuo pipefail
[[ $EUID == 0 && $# == 1 && $1 == --catch-up ]] || {
    echo 'sudo bash deploy/gcp/maintenance_now.sh --catch-up (휴장·폐장 후 전용)' >&2; exit 1;
}
cd /opt/sessionquant
# Calendar/config only: no database, network, credentials or engine startup.
docker run --rm --network none sessionquant:local python -c '
from datetime import datetime,timezone
from sessionquant.market.bars import session_bounds
now=datetime.now(timezone.utc); bounds=session_bounds(now)
if bounds and now < bounds[1]:
    raise SystemExit("밀린 자료 정리는 휴장일·정규장 마감 후에 실행하세요.")
' < /dev/null
systemd-run --unit=sessionquant-maintenance-catchup --collect --no-block \
    --property=Type=oneshot --property=TimeoutStartSec=34200 --property=TimeoutStopSec=60 \
    --property=Environment=SQ_MAINTENANCE_SECONDS=21600 \
    /opt/sessionquant/deploy/gcp/stop.sh
echo '밀린 자료 정리를 서버 작업으로 시작했습니다. 정상 종료·백업 후 최대 6시간 정리하며 전체 한도는 9시간 30분입니다.'
echo 'VM·DB는 유지하며 수집·매매는 재시작하지 않습니다. 접속 주소에서 진행 상황을 확인하세요.'
echo '상세 로그: sudo journalctl -u sessionquant-maintenance-catchup.service -f'

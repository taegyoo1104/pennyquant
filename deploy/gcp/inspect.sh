#!/bin/bash
# Start the stored-record UI only. Never starts the trading runtime.
set -Eeuo pipefail
cd /opt/sessionquant
source deploy/gcp/lifecycle_lock.sh
[[ ! -e /etc/sessionquant/maintenance && ! -e /etc/sessionquant/storage-blocked ]] || {
    echo '저장 공간·복구 점검 표시를 먼저 확인하세요.' >&2; exit 1;
}
python3 deploy/gcp/storage_check.py --minimum-mib 2048
sq_app=$(docker compose -f deploy/gcp/compose.yaml ps --status running --services app)
if [[ -n "$sq_app" ]]; then
    sq_command=$(docker inspect sessionquant-app-1 --format '{{json .Config.Cmd}}')
    [[ "$sq_app" == app && "$sq_command" == '["python","-m","sessionquant.inspection_dashboard"]' ]] || {
        echo '매매 프로그램이 실행 중입니다. 저장·종료가 끝나기 전에는 점검 화면으로 바꾸지 않습니다.' >&2; exit 1;
    }
fi
systemctl start sessionquant-storage-guard.timer
docker compose -f deploy/gcp/compose.yaml -f deploy/gcp/compose.inspection.yaml \
    up -d --wait --wait-timeout 480 < /dev/null
echo '점검 대시보드 준비 완료. 저장 기록만 조회하며 매매·시세 수집·AI 호출은 하지 않습니다.'
echo 'VM은 켜진 상태로 유지됩니다. 사용을 마치면 직접 종료하세요.'

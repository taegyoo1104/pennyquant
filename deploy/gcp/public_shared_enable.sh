#!/bin/bash
# Explicit migration of the existing public URL. Never starts the trading engine.
set -Eeuo pipefail
[[ $EUID == 0 ]] || exit 1
cd /opt/sessionquant
source deploy/gcp/lifecycle_lock.sh
exec 7>/run/sessionquant-backup.lock
flock -n 7 || { echo '백업이 끝난 뒤 전환하세요.' >&2; exit 1; }
[[ ! -e /etc/sessionquant/maintenance && ! -e /etc/sessionquant/storage-blocked ]] || exit 1
python3 deploy/gcp/shared_setup.py
bash deploy/gcp/inspect.sh
python3 deploy/gcp/check_public_routes.py --shared
tailscale funnel --bg --https=8443 http://127.0.0.1:8501
python3 deploy/gcp/check_public_routes.py --shared --ready
install -m 600 /dev/null /etc/sessionquant/public-shared
# Only the obsolete viewer is removed; no data volumes or DB accounts are deleted.
docker compose -f deploy/gcp/compose.public.yaml down
echo '공개 주소(:8443)에서 모든 페이지를 볼 수 있습니다. 매매 시작·청산·저장은 관리자 로그인 후 가능합니다.'

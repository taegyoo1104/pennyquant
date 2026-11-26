#!/bin/bash
# Explicit first-time publication. Keep private Serve on 443, public Funnel on 8443.
set -Eeuo pipefail
[[ $EUID == 0 ]] || exit 1
cd /opt/sessionquant
source deploy/gcp/lifecycle_lock.sh
exec 7>/run/sessionquant-backup.lock
flock -n 7 || { echo '백업이 끝난 뒤 공개 설정을 진행하세요.' >&2; exit 1; }
[[ ! -e /etc/sessionquant/maintenance && ! -e /etc/sessionquant/storage-blocked ]] || exit 1
python3 deploy/gcp/check_public_routes.py
python3 deploy/gcp/public_setup.py
bash deploy/gcp/inspect.sh
# Confirm the running admin image supports auth and received the configured role.
docker compose -f deploy/gcp/compose.yaml exec -T app python -c '
import os
from sessionquant.dashboard.access import verify_password
assert os.environ.get("SQ_DASHBOARD_ACCESS")=="admin"
assert os.environ.get("SQ_DASHBOARD_ADMIN_HASH", "").startswith("scrypt$")
'
docker compose -f deploy/gcp/compose.public.yaml up -d --wait --wait-timeout 180
docker compose -f deploy/gcp/compose.public.yaml exec -T viewer python -c '
import os
from sessionquant.dashboard.session_app import load_database
from sessionquant.dashboard.access import is_admin
assert not is_admin()
d=load_database("data/sessionquant-paper-local.db",page="대시보드")
assert d.connected and not d.error, "조회 전용 DB 연결 확인 실패"
'
# Explicit port-scoped commands; never expose localhost:8501 via Funnel.
tailscale serve --bg --https=443 http://127.0.0.1:8501
tailscale funnel --bg --https=8443 http://127.0.0.1:8502
python3 deploy/gcp/check_public_routes.py --ready
install -m 600 /dev/null /etc/sessionquant/public-enabled
echo '공개 조회: 위 Funnel 주소(:8443) · 관리자: 기존 Tailscale 주소(443).'

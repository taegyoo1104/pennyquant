#!/bin/bash
# Run only inside the dedicated GCE VM, from /opt/sessionquant.
set -Eeuo pipefail
[[ $EUID == 0 ]] || { echo 'sudo로 실행하세요.' >&2; exit 1; }
[[ ${1:-} =~ ^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$ ]] || { echo '사용법: sudo deploy/gcp/install.sh 백업버킷이름' >&2; exit 1; }
[[ $(pwd) == /opt/sessionquant ]] || { echo '/opt/sessionquant에서 실행하세요.' >&2; exit 1; }
# Refuse installation on the user's PC or an unrelated non-GCE machine.
sq_machine=$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)
[[ "$sq_machine" == 'Google Compute Engine' ]] || { echo 'GCE VM에서만 설치할 수 있습니다.' >&2; exit 1; }
. /etc/os-release
case "$ID:$VERSION_ID" in
    debian:12) sq_docker_os=debian; sq_docker_suite=bookworm ;;
    ubuntu:24.04) sq_docker_os=ubuntu; sq_docker_suite=noble ;;
    *) echo 'Debian 12 또는 Ubuntu 24.04 전용 설치입니다.' >&2; exit 1 ;;
esac
apt-get update
apt-get install -y ca-certificates curl git python3
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/$sq_docker_os/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
    "$(dpkg --print-architecture)" "$sq_docker_os" "$sq_docker_suite" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
install -d -m 700 /etc/sessionquant /var/lib/sessionquant/backups
install -d -m 700 -o 1000 -g 1000 /var/lib/sessionquant/app
install -d -m 700 /var/lib/sessionquant/mysql
python3 - <<'PY'
import os
from pathlib import Path
import secrets
os.umask(0o077)
root=Path('/etc/sessionquant')
db,app=root/'db.env',root/'app.env'
if db.exists()!=app.exists():
    raise SystemExit('DB/앱 환경 파일 중 하나가 없습니다. 기존 비밀번호를 복구한 뒤 다시 실행하세요.')
if not db.exists():
    password=secrets.token_urlsafe(32)
    db.write_text('MYSQL_DATABASE=sessionquant\nMYSQL_USER=sessionquant\nMYSQL_PASSWORD='+password+
                  '\nMYSQL_ROOT_PASSWORD='+secrets.token_urlsafe(32)+'\n')
    app.write_text('SQ_DB_BACKEND=mysql\nSQ_ENVIRONMENT=PAPER_LOCAL\nSQ_MYSQL_HOST=127.0.0.1\n'
                  'SQ_MYSQL_PORT=3306\nSQ_MYSQL_DATABASE=sessionquant\nSQ_MYSQL_USER=sessionquant\n'
                  'SQ_DB_NAMESPACE=sessionquant-realtime-paper-v1\nSQ_MYSQL_PASSWORD='+password+'\n'
                  'TOSS_CLIENT_ID=\nTOSS_CLIENT_SECRET=\nTOSS_ACCOUNT_SEQ=\nOPENAI_API_KEY=\n'
                  'SEC_USER_AGENT=\nFRED_API_KEY=\nAPCA_API_KEY_ID=\nAPCA_API_SECRET_KEY=\n')
PY
printf '%s\n' "$1" > /etc/sessionquant/backup-bucket
chmod 600 /etc/sessionquant/*
docker build -t sessionquant:local .
docker compose -f deploy/gcp/compose.yaml pull db
chmod 755 deploy/gcp/*.sh
install -m 644 deploy/gcp/sessionquant*.service deploy/gcp/sessionquant*.timer /etc/systemd/system/
systemctl daemon-reload
echo '설치 완료. API 키 입력·DB 이전·백업 확인 후 안내대로 서비스를 활성화하세요. 지금은 자동 실행/종료를 활성화하지 않았습니다.'

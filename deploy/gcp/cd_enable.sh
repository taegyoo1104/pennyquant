#!/bin/bash
set -Eeuo pipefail
[[ $EUID == 0 && $(hostname -s) == sessionquant-paper ]] || exit 1
cd /opt/sessionquant
for name in sessionquant-cd.service sessionquant-cd.timer; do
    install -m 644 "deploy/gcp/$name" "/etc/systemd/system/$name"
done
install -d -m 700 /var/lib/sessionquant/cd
install -m 600 /dev/null /etc/sessionquant/cd-enabled
systemctl daemon-reload
systemctl enable --now sessionquant-cd.timer
echo 'CD 활성화: main 회귀검사 통과 후, 휴장·폐장 및 매매 종료 상태에서만 자동 배포합니다.'

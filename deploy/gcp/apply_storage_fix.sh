#!/bin/bash
# Run only on the restored operating VM, with offline maintenance already prepared.
set -Eeuo pipefail
[[ $EUID == 0 && ${1:-} == --snapshot-confirmed ]] || {
    echo '사용법: sudo bash deploy/gcp/apply_storage_fix.sh --snapshot-confirmed' >&2; exit 1;
}
[[ $(hostname -s) == sessionquant-paper && -e /etc/sessionquant/maintenance ]] || {
    echo '운영 서버의 점검 모드에서 실행하세요.' >&2; exit 1;
}
cd /opt/sessionquant
compose=(docker compose -f deploy/gcp/compose.yaml)
[[ $("${compose[@]}" ps --status running --services app) != app ]] || { echo '앱이 실행 중입니다.' >&2; exit 1; }
# Skip the maintenance override on repeat execution once log_bin=0 was verified.
if [[ ! -e /etc/sessionquant/binlog-disabled-verified ]]; then
    "${compose[@]}" -f deploy/gcp/compose.maintenance.yaml up -d --wait --wait-timeout 180 db < /dev/null
    bash deploy/gcp/purge_binlogs.sh --snapshot-confirmed < /dev/null
fi
"${compose[@]}" up -d --wait --wait-timeout 180 db < /dev/null
logging=$("${compose[@]}" exec -T db sh -c \
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root -N -B -e "SELECT @@GLOBAL.log_bin"' < /dev/null)
[[ "$logging" == 0 ]] || { echo '변경 기록 비활성화 확인 실패' >&2; exit 1; }
install -m 600 /dev/null /etc/sessionquant/binlog-disabled-verified
python3 deploy/gcp/storage_check.py --minimum-mib 2048
bash deploy/gcp/backup.sh < /dev/null
# Recovery patches make a working-tree build distinct from the base commit.
sq_build_revision=$(git rev-parse HEAD 2>/dev/null || true)
docker build --build-arg "SQ_BUILD_REVISION=$sq_build_revision" --build-arg SQ_BUILD_SOURCE=working_tree \
    -t sessionquant:local . < /dev/null
python3 deploy/gcp/storage_check.py --minimum-mib 2048
chmod 755 deploy/gcp/*.sh
for path in deploy/gcp/sessionquant*.service deploy/gcp/sessionquant*.timer; do
    target="/etc/systemd/system/$(basename "$path")"
    if [[ -L "$target" && $(readlink "$target") == /dev/null ]]; then rm -- "$target"; fi
    install -m 644 "$path" "$target"
done
# This unit is created transiently by start.sh; remove only our offline mask.
if [[ -L /etc/systemd/system/sessionquant-market-close.service && $(readlink /etc/systemd/system/sessionquant-market-close.service) == /dev/null ]]; then
    rm /etc/systemd/system/sessionquant-market-close.service
fi
systemctl daemon-reload
echo '수정·DB 확인·클라우드 백업·이미지 빌드 완료. 점검 모드는 유지합니다.'
cat /etc/sessionquant/backup-status
echo '점검 중에는 자동 종료 예약이 꺼져 있습니다. 완료 후 서버를 수동 종료하거나 운영 재개 절차를 따르세요.'

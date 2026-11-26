# Sourced by lifecycle entry points. Child helpers inherit fd 8 and its lock;
# systemd starts a separate process, so update must release it before start.
sq_lifecycle_lock=/run/sessionquant-lifecycle.lock
if [[ $(readlink "/proc/$$/fd/8" 2>/dev/null || true) != "$sq_lifecycle_lock" ]]; then
    exec 8>"$sq_lifecycle_lock"
fi
flock -n 8 || { echo '다른 시작·업데이트·마감 작업이 진행 중입니다.' >&2; exit 1; }
[[ ! -e /etc/sessionquant/projection-preparation-blocked ]] || {
    echo '조회 요약 준비 작업의 종료를 확인하지 못했습니다. 해당 컨테이너를 확인한 뒤 재개하세요.' >&2; exit 1;
}

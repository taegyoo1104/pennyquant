"""Migrate an existing publication without rotating credentials or writing to DB."""
import json
import os
from pathlib import Path
import subprocess

from check_public_routes import validate, verify_shared_runtime
from public_setup import env_update


def main():
    if os.geteuid() != 0 or os.uname().nodename.split('.')[0] != 'sessionquant-paper':
        raise SystemExit('sessionquant-paper에서 sudo로 실행하세요.')
    root = Path('/etc/sessionquant')
    if not (root/'public-enabled').is_file():
        raise SystemExit('public_enable.sh로 최초 관리자 비밀번호·공개 설정을 먼저 완료하세요.')
    command = subprocess.check_output(['docker', 'inspect', 'sessionquant-app-1', '--format',
                                      '{{json .Config.Cmd}}'], text=True).strip()
    if command != '["python","-m","sessionquant.inspection_dashboard"]':
        raise SystemExit('매매 프로그램을 종료하고 점검 화면에서 전환하세요.')
    verify_shared_runtime(allow_admin=True)
    config = json.loads(subprocess.check_output(['tailscale', 'serve', 'status', '--json'], text=True))
    # A retry may already have switched 8443. The verified image authenticates
    # all mutations in either admin or shared mode; 443 must remain private.
    validate(config, shared=True)
    env_update(root/'app.env', {'SQ_DASHBOARD_ACCESS': 'shared'})


if __name__ == '__main__':
    main()

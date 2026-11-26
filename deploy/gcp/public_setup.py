#!/usr/bin/env python3
"""One-time root setup. Passwords stay in stdin/root-only env files, never command arguments."""
import getpass
import hashlib
import os
from pathlib import Path
import re
import secrets
import subprocess

ROOT = Path('/etc/sessionquant')
COMPOSE = ['docker', 'compose', '-f', '/opt/sessionquant/deploy/gcp/compose.yaml']


def env_update(path, values):
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if line.split('=', 1)[0] not in values]
    for key, value in values.items():
        if any(c in value for c in "\n\r'"):
            raise ValueError('환경 파일 값 형식을 확인하세요.')
        lines.append(f"{key}='{value}'")
    temporary = path.with_suffix('.tmp')
    temporary.write_text('\n'.join(lines)+'\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    if os.geteuid() != 0 or os.uname().nodename.split('.')[0] != 'sessionquant-paper':
        raise SystemExit('sessionquant-paper에서 sudo python3로 실행하세요.')
    os.umask(0o077)
    if not os.isatty(0):
        raise SystemExit('비밀번호를 안전하게 입력할 수 있도록 PuTTY 터미널에서 직접 실행하세요.')
    # Fail before rotating any credentials if a trading runtime is running.
    running = subprocess.check_output(COMPOSE+['ps', '--status', 'running', '--services', 'app'], text=True).strip()
    if running:
        command = subprocess.check_output(['docker', 'inspect', 'sessionquant-app-1', '--format', '{{json .Config.Cmd}}'], text=True).strip()
        if command != '["python","-m","sessionquant.inspection_dashboard"]':
            raise SystemExit('매매 프로그램을 종료하고 점검 화면으로 전환한 뒤 설정하세요.')
    password = getpass.getpass('관리자 비밀번호 (16~256자): ')
    if not 16 <= len(password) <= 256 or password != getpass.getpass('비밀번호 다시 입력: '):
        raise SystemExit('비밀번호 길이 또는 일치 여부를 확인하세요.')
    salt = secrets.token_bytes(16)
    hashed = 'scrypt$'+salt.hex()+'$'+hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()
    # Read non-secret resolved namespace from the app image, without printing credentials.
    ns = subprocess.check_output(COMPOSE+['run', '--rm', '--no-deps', '-T', 'app', 'python', '-c',
        'import os; print(os.environ.get("SQ_DB_NAMESPACE", ""))'], text=True).strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', ns):
        raise SystemExit('SQ_DB_NAMESPACE를 확인하세요. 공개 화면을 만들지 않았습니다.')
    database = subprocess.check_output(COMPOSE+['exec', '-T', 'db', 'printenv', 'MYSQL_DATABASE'], text=True).strip()
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', database):
        raise SystemExit('DB 이름을 확인하세요.')
    viewer_password = secrets.token_hex(32)
    # Fixed identity, SELECT only; revoke stale privileges on setup/rekey.
    sql = ("CREATE USER IF NOT EXISTS 'sq_public_viewer'@'%' IDENTIFIED BY '"+viewer_password+"';\n"
           "ALTER USER 'sq_public_viewer'@'%' IDENTIFIED BY '"+viewer_password+"' WITH MAX_USER_CONNECTIONS 4;\n"
           "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'sq_public_viewer'@'%';\n")
    for table in ('pq_controls', 'pq_engine_state', 'pq_executions', 'pq_quote_snapshots', 'pq_orders'):
        sql += f"GRANT SELECT ON `{database}`.`{table}` TO 'sq_public_viewer'@'%';\n"
    if (ROOT/'nightly-seal.json').exists():
        subprocess.run(['python3', '/opt/sessionquant/deploy/gcp/nightly_checkpoint.py', 'writable'], check=True)
    subprocess.run(COMPOSE+['exec', '-T', 'db', 'sh', '-c',
        'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql -u root'], input=sql, text=True, check=True,
        stdout=subprocess.DEVNULL)
    security = {'STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION': 'true', 'STREAMLIT_SERVER_ENABLE_CORS': 'true',
                'STREAMLIT_CLIENT_SHOW_ERROR_DETAILS': 'none', 'STREAMLIT_BROWSER_GATHER_USAGE_STATS': 'false',
                'STREAMLIT_SERVER_MAX_UPLOAD_SIZE': '1', 'STREAMLIT_SERVER_MAX_MESSAGE_SIZE': '20'}
    env_update(ROOT/'public.env', dict(security, SQ_DASHBOARD_ACCESS='viewer', SQ_DASHBOARD_READ_ONLY='1',
        SQ_ENVIRONMENT='PAPER_LOCAL', SQ_DB_BACKEND='mysql', SQ_DB_NAMESPACE=ns, SQ_MYSQL_HOST='127.0.0.1',
        SQ_MYSQL_DATABASE=database, SQ_MYSQL_USER='sq_public_viewer', SQ_MYSQL_PASSWORD=viewer_password,
        SQ_MYSQL_READ_TIMEOUT='5'))
    env_update(ROOT/'app.env', dict(security, SQ_DASHBOARD_ACCESS='admin', SQ_DASHBOARD_ADMIN_HASH=hashed))
    print('조회 전용 DB 계정과 관리자 로그인을 설정했습니다. 외부 공개는 아직 시작하지 않았습니다.')


if __name__ == '__main__':
    main()

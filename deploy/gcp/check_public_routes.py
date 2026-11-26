#!/usr/bin/env python3
"""Validate legacy viewer routes or an explicitly authenticated shared dashboard."""
import json
from pathlib import Path
import subprocess


def verify_shared_runtime(*, allow_admin=False):
    # Import the actual running image's access module. Missing/old code, missing
    # credentials or local/unknown roles must fail before changing public routes.
    roles = ['shared', 'admin'] if allow_admin else ['shared']
    code = r'''
import os, re
from sessionquant.dashboard.access import can_browse, is_admin, shared_dashboard
assert os.environ.get('SQ_DASHBOARD_ACCESS') in ROLES
assert re.fullmatch(r'scrypt\$[0-9a-f]{32}\$[0-9a-f]{128}', os.environ.get('SQ_DASHBOARD_ADMIN_HASH', ''))
assert not is_admin(), 'non-session access must never have admin privileges'
assert os.environ.get('STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION') == 'true'
assert os.environ.get('STREAMLIT_SERVER_ENABLE_CORS') == 'true'
'''.replace('ROLES', repr(roles))
    subprocess.run(['docker', 'compose', '-f', '/opt/sessionquant/deploy/gcp/compose.yaml',
                    'exec', '-T', 'app', 'python', '-c', code], check=True, stdout=subprocess.DEVNULL)


def validate(config, *, ready=False, shared=False):
    funnel = config.get('AllowFunnel') or {}
    web = config.get('Web') or {}
    for address, enabled in funnel.items():
        if not enabled:
            continue
        if address.endswith(':443'):
            raise ValueError('443 관리자 주소에 Funnel이 켜져 있습니다. 공개를 해제한 뒤 다시 설정하세요.')
        for handler in (web.get(address, {}).get('Handlers') or {}).values():
            if '8501' in handler.get('Proxy', '') and not (
                    shared and address.endswith(':8443') and handler.get('Proxy') in {
                        'http://127.0.0.1:8501', 'http://127.0.0.1:8501/'}):
                raise ValueError('관리자 포트를 공개하는 Funnel 경로가 있습니다.')
    if ready:
        public = [address for address, enabled in funnel.items() if enabled and address.endswith(':8443')]
        if len(public) != 1:
            raise ValueError('8443 공개 주소를 확인하지 못했습니다.')
        handlers = web.get(public[0], {}).get('Handlers', {})
        port = 8501 if shared else 8502
        if (set(handlers) != {'/'} or set(handlers['/']) != {'Proxy'}
                or handlers['/']['Proxy'] not in {f'http://127.0.0.1:{port}', f'http://127.0.0.1:{port}/'}):
            raise ValueError(f'공개 경로는 검증한 {port} 포트만 연결해야 합니다.')


if __name__ == '__main__':
    import sys
    try:
        shared = '--shared' in sys.argv or Path('/etc/sessionquant/public-shared').is_file()
        if shared:
            verify_shared_runtime()
        validate(json.loads(subprocess.check_output(['tailscale', 'serve', 'status', '--json'], text=True)),
                 ready='--ready' in sys.argv, shared=shared)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

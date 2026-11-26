#!/usr/bin/env python3
"""Fail closed if the private admin listener is exposed through Funnel."""
import json
import subprocess


def validate(config, *, ready=False):
    funnel = config.get('AllowFunnel') or {}
    web = config.get('Web') or {}
    for address, enabled in funnel.items():
        if not enabled:
            continue
        if address.endswith(':443'):
            raise ValueError('443 관리자 주소에 Funnel이 켜져 있습니다. 공개를 해제한 뒤 다시 설정하세요.')
        for handler in (web.get(address, {}).get('Handlers') or {}).values():
            if '8501' in handler.get('Proxy', ''):
                raise ValueError('관리자 포트를 공개하는 Funnel 경로가 있습니다.')
    if ready:
        public = [address for address, enabled in funnel.items() if enabled and address.endswith(':8443')]
        if len(public) != 1:
            raise ValueError('8443 공개 주소를 확인하지 못했습니다.')
        handlers = web.get(public[0], {}).get('Handlers', {})
        if (set(handlers) != {'/'} or set(handlers['/']) != {'Proxy'}
                or handlers['/']['Proxy'] not in {'http://127.0.0.1:8502', 'http://127.0.0.1:8502/'}):
            raise ValueError('공개 경로는 조회 전용 8502 포트만 연결해야 합니다.')


if __name__ == '__main__':
    import sys
    try:
        validate(json.loads(subprocess.check_output(['tailscale', 'serve', 'status', '--json'], text=True)),
                 ready='--ready' in sys.argv)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

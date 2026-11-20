#!/usr/bin/env python3
"""Cloud Shell dashboard access. Standard library only; never starts/stops the VM."""
from collections import deque
from contextlib import contextmanager
import argparse
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


PROJECT = 'sessionquant'
ZONE = 'us-central1-a'
INSTANCE = 'sessionquant-paper'
REMOTE_PORT = 8501
READY = 'SQ_DASHBOARD_READY'


class AccessError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


class Process:
    """Own one process group and bounded output tails, including long-lived SSH."""
    def __init__(self, command):
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True)
        self.output = [deque(maxlen=16), deque(maxlen=16)]
        self.readers = []
        for stream, tail in zip((self.process.stdout, self.process.stderr), self.output):
            reader = threading.Thread(target=self._read, args=(stream, tail), daemon=True)
            reader.start()
            self.readers.append(reader)

    @staticmethod
    def _read(stream, tail):
        try:
            while chunk := stream.read(512):
                tail.append(chunk)
        finally:
            stream.close()

    def text(self, index):
        return b''.join(self.output[index]).decode('utf-8', errors='replace')

    def poll(self):
        # Keep the group leader unreaped until close(). Its reserved PID makes
        # killpg safe even if gcloud exits before SSH/IAP children finish.
        result = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is None:
            return None
        return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status

    def drain(self):
        for reader in self.readers:
            reader.join(timeout=1)

    def close(self):
        # This PGID was created here with start_new_session, never read from a
        # PID file or inferred from a port owner. Also clean remaining children
        # if the gcloud parent has already exited.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                continue
            if sig == signal.SIGTERM:
                deadline = time.monotonic()+2
                while self.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
        self.process.wait(timeout=2)
        self.drain()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def run(command, timeout=25, progress=None):
    with Process(command) as process:
        deadline, next_message = time.monotonic() + timeout, time.monotonic() + 15
        while process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                raise AccessError('응답 대기 시간이 끝났습니다. Cloud Shell 인증·VM 연결 상태를 확인하세요.')
            if progress and now >= next_message:
                print(progress, flush=True)
                next_message = now + 15
            time.sleep(min(.2, max(0, deadline-now)))
        process.drain()
        return process.poll(), process.text(0), process.text(1)


def cloud_command(*args):
    return ['gcloud', '--quiet', *args, f'--project={PROJECT}']


def ssh_command(*args):
    return cloud_command('compute', 'ssh', INSTANCE, f'--zone={ZONE}', '--tunnel-through-iap', *args)


def error_message(stderr, code):
    """Do not print raw auth URLs, account data, tracebacks or remote bodies."""
    lower = stderr.lower()
    if any(word in lower for word in ('active account', 'credentials', 'invalid_grant', 'reauth', 'login required')):
        return 'Cloud Shell 인증을 다시 확인하세요. 콘솔에서 인증을 완료한 뒤 이 명령을 다시 실행하세요.'
    if any(word in lower for word in ('host identification has changed', 'host key verification failed')):
        return 'SSH 서버 키 확인이 필요합니다. VM 교체 이력과 서버 키를 확인하세요.'
    if any(word in lower for word in ('permission denied', 'permission_denied', '403', 'not authorized')):
        return 'VM 접속 권한 또는 SSH 인증을 확인하세요. 현재 계정으로 IAP·SSH 접속 권한이 필요합니다.'
    if 'address already in use' in lower or 'forwarding request failed' in lower:
        return '선택한 로컬 포트를 다른 작업이 먼저 사용했습니다. 기존 작업을 종료하지 않았습니다. 다시 실행하세요.'
    return f'GCP·SSH 연결을 완료하지 못했습니다(종료 코드 {code}). Cloud Shell 인증과 VM 상태를 확인하세요.'


def check_location():
    host = socket.gethostname().split('.', 1)[0].lower()
    if host == INSTANCE:
        raise AccessError('지금은 sessionquant-paper VM 안입니다. 별도 Cloud Shell 터미널(@cloudshell)에서 실행하세요.')
    is_cloud_shell = (os.environ.get('CLOUD_SHELL', '').lower() in {'true', '1'}
                      or bool(os.environ.get('DEVSHELL_PROJECT_ID')) or 'cloudshell' in host)
    if not sys.platform.startswith('linux') or not is_cloud_shell:
        raise AccessError('이 도우미는 GCP Cloud Shell용입니다. 콘솔의 Cloud Shell(@cloudshell)에서 실행하세요. '
                          '로컬 PC 접속은 문서의 수동 SSH 안내를 사용하세요.')
    if not shutil.which('gcloud') or not shutil.which('ssh'):
        raise AccessError('gcloud 또는 ssh를 찾지 못했습니다. GCP Cloud Shell에서 실행하세요.')


def check_account_and_vm():
    code, output, error = run(['gcloud', '--quiet', 'auth', 'list', '--filter=status:ACTIVE', '--format=value(account)'])
    if code:
        raise AccessError(error_message(error, code))
    if not output.strip():
        raise AccessError('활성 로그인 계정이 없습니다. 콘솔에서 Cloud Shell 인증을 완료한 뒤 다시 실행하세요.')
    code, output, error = run(cloud_command('compute', 'instances', 'describe', INSTANCE,
                                          f'--zone={ZONE}', '--format=value(status)'))
    if code:
        raise AccessError(error_message(error, code))
    if output.strip() != 'RUNNING':
        raise AccessError('VM이 실행 중이 아닙니다. sessionquant-paper를 먼저 켠 뒤 다시 실행하세요.')


def remote_probe(seconds):
    # The remote loop avoids repeated authentication/IAP round trips while the
    # application restores its DB. Only a fixed localhost health URL is read.
    program = f'''import time, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({{}}),NoRedirect())
deadline=time.monotonic()+{seconds}
while time.monotonic()<deadline:
    try:
        with opener.open("http://127.0.0.1:{REMOTE_PORT}/_stcore/health",timeout=3) as response:
            if response.status==200 and response.read(16).strip().lower()==b"ok":
                print("{READY}",flush=True)
                raise SystemExit(0)
    except Exception:
        pass
    time.sleep(2)
raise SystemExit(42)
'''
    return ssh_command('--command=' + 'python3 -c ' + shlex.quote(program)) + [
        '--', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']


def wait_remote(seconds):
    print(f'VM의 화면 서버를 기다립니다. 최대 {seconds}초 · 프로그램을 다시 시작하지 않습니다.', flush=True)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        remaining = max(1, int(deadline-time.monotonic()))
        code, output, error = run(remote_probe(remaining), timeout=remaining+15,
                                  progress='화면 서버 준비를 기다리는 중입니다. DB 복원 중이면 시간이 걸릴 수 있습니다.')
        if code == 0 and READY in output.splitlines():
            return
        if code == 42:
            break
        # A just-started VM may be RUNNING before port22 is listening. Retry
        # only this recognizable startup failure; auth/host-key failures stop.
        if any(word in error.lower() for word in ('failed to connect to backend', 'connection refused', 'connection timed out')):
            print('VM의 SSH가 아직 준비되지 않았습니다. 잠시 뒤 다시 확인합니다.', flush=True)
            time.sleep(min(5, max(0, deadline-time.monotonic())))
            continue
        raise AccessError(error_message(error, code))
    raise AccessError('화면 서버가 준비 시간 안에 응답하지 않았습니다. VM의 app 상태·로그를 확인하세요. '
                      'SSH 터널은 열지 않았으며 프로그램을 재시작하지 않았습니다.')


def free_port(requested=None):
    for port in [requested] if requested is not None else range(8501, 8511):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise AccessError('사용 가능한 로컬 포트가 없습니다. 다른 작업을 종료하지 않았습니다. '
                      '--port 번호로 다른 포트를 지정하세요.')


def healthy(port):
    try:
        # Ignore shell proxy settings for this loopback-only probe.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(f'http://127.0.0.1:{port}/_stcore/health', timeout=2) as response:
            return response.status == 200 and response.read(16).strip().lower() == b'ok'
    except (OSError, urllib.error.URLError, ValueError):
        return False


def control_command(path, operation, port=None):
    command = ['ssh', '-S', str(path), '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-O', operation]
    if port is not None:
        command += ['-L', f'127.0.0.1:{port}:127.0.0.1:{REMOTE_PORT}']
    return command + [INSTANCE]


def connect(port):
    # Private invocation-only socket; never adopt an old tunnel or trust an
    # arbitrary listener's HTTP 200 as proof of a connection to this VM.
    with tempfile.TemporaryDirectory(prefix='sq-dashboard-', dir='/tmp') as directory:
        control = Path(directory)/'control'
        command = ssh_command() + ['--', '-N', '-M', '-S', str(control), '-o', 'ControlPersist=no',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3']
        with Process(command) as process:
            deadline = time.monotonic()+45
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    process.drain()
                    raise AccessError(error_message(process.text(1), process.poll()))
                if control.exists() and run(control_command(control, 'check'), timeout=3)[0] == 0:
                    break
                time.sleep(.2)
            else:
                raise AccessError('SSH 연결 준비 시간이 끝났습니다. Cloud Shell 인증·IAP 연결을 확인하세요.')
            # An explicit mux forward success proves *this* SSH owns the bind.
            # A competing listener can appear after free_port(), so a process
            # alive check plus HTTP alone would be insufficient.
            code, _, error = run(control_command(control, 'forward', port), timeout=5)
            if code:
                raise AccessError(error_message(error, code))
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    process.drain()
                    raise AccessError(error_message(process.text(1), process.poll()))
                if healthy(port):
                    if process.poll() is not None:
                        raise AccessError('SSH 연결이 종료됐습니다. 접속 도우미를 다시 실행하세요.')
                    break
                time.sleep(.5)
            else:
                raise AccessError('터널은 연결됐지만 화면 HTTP 응답을 확인하지 못했습니다. VM 앱 상태를 확인하세요.')
            print(f'대시보드 접속 준비 완료 · Cloud Shell 포트 {port} → VM 포트 {REMOTE_PORT}', flush=True)
            print(f'Cloud Shell 상단 웹 미리보기 → 포트 변경 → {port}으로 여세요.', flush=True)
            print('이 터미널을 유지하세요. Ctrl+C는 이 접속만 닫고 VM의 프로그램·매매는 계속 실행됩니다.', flush=True)
            while process.poll() is None:
                time.sleep(.5)
            process.drain()
            raise AccessError('대시보드 접속이 종료됐습니다. 이 명령을 다시 실행하세요. '
                              'VM 프로그램 종료 여부는 별도로 확인해야 합니다.')


@contextmanager
def interruption_handlers():
    def interrupt(*_):
        # A repeated Ctrl+C must not interrupt cleanup of the owned children.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        raise KeyboardInterrupt
    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Cloud Shell에서 SessionQuant 대시보드 접속을 확인하고 엽니다.')
    parser.add_argument('--port', type=int, help='Cloud Shell에서 사용할 포트. 기본: 8501~8510 중 비어 있는 포트')
    parser.add_argument('--wait-seconds', type=int, default=480, help='VM 화면 서버 준비 대기 한도(기본 480초)')
    args = parser.parse_args(argv)
    if args.port is not None and not 2000 <= args.port <= 65000:
        parser.error('--port는 Cloud Shell 미리보기 범위인 2000~65000이어야 합니다.')
    if not 1 <= args.wait_seconds <= 600:
        parser.error('--wait-seconds는 1~600이어야 합니다.')
    try:
        with interruption_handlers():
            check_location()
            print('대시보드 접속 점검 · Cloud Shell → sessionquant-paper', flush=True)
            check_account_and_vm()
            wait_remote(args.wait_seconds)
            port = free_port(args.port)
            if port != REMOTE_PORT:
                print(f'로컬 8501 대신 {port}을 사용합니다. VM의 화면 포트는 8501 그대로입니다.', flush=True)
            connect(port)
    except KeyboardInterrupt:
        print('\n대시보드 접속을 닫았습니다. VM 프로그램에 종료 명령을 보내지 않았습니다.', flush=True)
        return 130
    except (AccessError, OSError) as exc:
        print('접속 확인 중단: '+str(exc), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Cloud Shell access is exercised with local processes and loopback HTTP only."""
from contextlib import contextmanager
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1]/'deploy/gcp/dashboard_access.py'


@pytest.fixture
def access():
    spec = importlib.util.spec_from_file_location('dashboard_access', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clock(access, monkeypatch):
    state = SimpleNamespace(now=0)
    def sleep(seconds):
        state.now += seconds
    monkeypatch.setattr(access, 'time', SimpleNamespace(monotonic=lambda:state.now, sleep=sleep))
    return state


def test_vm_location_rejected_even_if_cloud_shell_environment_leaked(access, monkeypatch):
    monkeypatch.setenv('CLOUD_SHELL', 'true')
    monkeypatch.setattr(access.socket, 'gethostname', lambda:'sessionquant-paper.example.internal')
    with pytest.raises(access.AccessError, match='VM 안'):
        access.check_location()


def test_ambiguous_location_rejected_without_starting_any_process(access, monkeypatch, capsys):
    monkeypatch.delenv('CLOUD_SHELL', raising=False)
    monkeypatch.delenv('DEVSHELL_PROJECT_ID', raising=False)
    monkeypatch.setattr(access.socket, 'gethostname', lambda:'my-computer')
    monkeypatch.setattr(access, 'run', lambda *_a, **_k:pytest.fail('must not run gcloud'))
    assert access.main([]) == 1
    assert 'Cloud Shell용' in capsys.readouterr().err


@pytest.mark.parametrize('account,status,message', [('', 'RUNNING', '활성 로그인'),
    ('fixture@example.invalid\n', 'TERMINATED\n', '실행 중이 아닙니다')])
def test_missing_account_or_stopped_vm_never_opens_ssh(access, monkeypatch, account, status, message):
    commands = []
    def run(command, **_):
        commands.append(command)
        return 0, account if 'auth' in command else status, ''
    monkeypatch.setattr(access, 'run', run)
    with pytest.raises(access.AccessError, match=message):
        access.check_account_and_vm()
    assert all('ssh' not in command for command in commands)
    assert len(commands) == (1 if not account else 2)
    if account:
        assert '--project=sessionquant' in commands[-1] and '--zone=us-central1-a' in commands[-1]


def test_warming_vm_retries_only_startup_ssh_error_then_accepts_health_with_warning(access, monkeypatch, clock):
    replies = iter([(255, '', "failed to connect to backend: connection refused"),
                    (0, access.READY+'\n', 'WARNING: install NumPy for tunnel performance')])
    commands = []
    def run(command, **_):
        commands.append(command)
        return next(replies)
    monkeypatch.setattr(access, 'run', run)
    access.wait_remote(20)
    assert len(commands) == 2 and clock.now == 5
    assert all('--tunnel-through-iap' in command for command in commands)
    probe = next(arg for arg in commands[0] if arg.startswith('--command='))
    assert 'ProxyHandler({})' in probe and 'NoRedirect()' in probe
    assert '127.0.0.1:8501/_stcore/health' in probe


@pytest.mark.parametrize('code,error,message', [(255, 'invalid_grant secret-auth-url', '인증'),
    (255, 'REMOTE HOST IDENTIFICATION HAS CHANGED', '서버 키'), (42, '', '준비 시간')])
def test_remote_failure_stops_without_retrying_auth_or_opening_tunnel(access, monkeypatch, clock, code, error, message):
    calls = []
    def run(command, **_):
        calls.append(command)
        return code, '', error
    monkeypatch.setattr(access, 'run', run)
    with pytest.raises(access.AccessError, match=message) as exc:
        access.wait_remote(20)
    assert len(calls) == 1 and 'secret-auth-url' not in str(exc.value)


def test_free_port_skips_foreign_listeners_without_reusing_or_closing_them(access, monkeypatch):
    attempted = []
    class Socket:
        def __enter__(self):return self
        def __exit__(self, *_):pass
        def bind(self, target):
            attempted.append(target)
            if target[1] in (8501, 8502):raise OSError('occupied')
    monkeypatch.setattr(access.socket, 'socket', lambda *_:Socket())
    assert access.free_port() == 8503
    assert attempted == [('127.0.0.1', 8501), ('127.0.0.1', 8502), ('127.0.0.1', 8503)]
    with pytest.raises(access.AccessError, match='사용 가능한'):
        access.free_port(8501)


@contextmanager
def http_server(*, redirect=None):
    state = {'requests':0}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['requests'] += 1
            if redirect:
                self.send_response(302)
                self.send_header('Location', redirect)
            else:
                self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')
        def log_message(self, *_):pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_local_health_ignores_proxy_and_rejects_redirect(access, monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://unreachable.invalid:9999')
    monkeypatch.setenv('http_proxy', 'http://unreachable.invalid:9999')
    monkeypatch.setenv('no_proxy', '')
    with http_server() as (target, state):
        assert access.healthy(target)
        with http_server(redirect=f'http://127.0.0.1:{target}/_stcore/health') as (source, _):
            assert not access.healthy(source)
        assert state['requests'] == 1


def fake_master(access, monkeypatch):
    state = SimpleNamespace(closed=False, commands=[])
    class Master:
        def __init__(self, command):
            state.commands.append(command)
            state.control = Path(command[command.index('-S')+1])
            state.control.touch()
        def poll(self):return None
        def drain(self):pass
        def text(self, _):return ''
        def __enter__(self):return self
        def __exit__(self, *_):state.closed=True
    monkeypatch.setattr(access, 'Process', Master)
    return state


def test_bind_race_cannot_report_foreign_http_200_as_ready(access, monkeypatch, clock, capsys):
    state = fake_master(access, monkeypatch)
    commands = []
    def run(command, **_):
        commands.append(command)
        return (0, '', '') if 'check' in command else (255, '', 'forwarding request failed: Address already in use')
    monkeypatch.setattr(access, 'run', run)
    monkeypatch.setattr(access, 'healthy', lambda *_:pytest.fail('foreign HTTP must not be queried after failed forward'))
    with pytest.raises(access.AccessError, match='다른 작업'):
        access.connect(8503)
    assert state.closed and not state.control.parent.exists()
    assert '준비 완료' not in capsys.readouterr().out
    assert [command[command.index('-O')+1] for command in commands] == ['check', 'forward']
    assert '127.0.0.1:8503:127.0.0.1:8501' in commands[-1]
    assert '-L' not in state.commands[0]
    assert '-M' in state.commands[0] and 'ControlPersist=no' in state.commands[0]


def test_bound_tunnel_without_http_readiness_never_claims_ready(access, monkeypatch, clock, capsys):
    state = fake_master(access, monkeypatch)
    monkeypatch.setattr(access, 'run', lambda *_a, **_k:(0, '', 'harmless stderr'))
    monkeypatch.setattr(access, 'healthy', lambda *_:False)
    with pytest.raises(access.AccessError, match='HTTP 응답'):
        access.connect(8502)
    assert state.closed and clock.now >= 15
    assert '준비 완료' not in capsys.readouterr().out


def test_real_process_early_parent_exit_cleans_stubborn_child_and_preserves_foreign_listener(access, tmp_path):
    port_file = tmp_path/'owned-port'
    child = f'''import os,signal,socket,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
s=socket.socket();s.bind(("127.0.0.1",0));s.listen()
open({str(port_file)!r},"w").write(str(s.getsockname()[1]))
os.close(1);os.close(2)
time.sleep(60)
'''
    parent = f'''import subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable,"-c",{child!r}])
while not Path({str(port_file)!r}).exists():time.sleep(.01)
'''
    with http_server() as (foreign, _):
        with access.Process([sys.executable, '-c', parent]) as process:
            deadline = time.monotonic()+4
            while process.poll() is None and time.monotonic()<deadline:time.sleep(.01)
            assert process.poll() == 0
            # WNOWAIT has not reaped the leader; its PGID cannot be recycled.
            assert Path(f'/proc/{process.process.pid}').exists()
            owned = int(port_file.read_text())
            with socket.create_connection(('127.0.0.1', owned), timeout=1):pass
        assert process.process.returncode == 0
        deadline = time.monotonic()+2
        while time.monotonic()<deadline:
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', owned)) != 0:break
            time.sleep(.01)
        else:pytest.fail('owned child listener survived cleanup')
        assert access.healthy(foreign)


FAKE_GCLOUD = '''#!/usr/bin/env python3
import json,os,socket,subprocess,sys,time
from pathlib import Path
args=sys.argv[1:]
if "auth" in args:
    print("fixture@example.invalid");raise SystemExit(0)
if "describe" in args:
    print("RUNNING");raise SystemExit(0)
if any(x.startswith("--command=") for x in args):
    print("SQ_DASHBOARD_READY");raise SystemExit(0)
control=Path(args[args.index("-S")+1])
listener=socket.socket(socket.AF_UNIX);listener.bind(str(control));listener.listen()
Path(os.environ["FIXTURE_STATE"]).joinpath("master_pid").write_text(str(os.getpid()))
while True:
    connection,_=listener.accept()
    with connection:
        request=json.loads(connection.recv(4096))
        if request["operation"]=="forward":
            code="""from http.server import BaseHTTPRequestHandler,HTTPServer
import signal
signal.signal(signal.SIGTERM,signal.SIG_IGN)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.end_headers();self.wfile.write(b'ok')
    def log_message(self,*args):pass
HTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
""".replace("PORT",str(request["port"]))
            child=subprocess.Popen([sys.executable,"-c",code])
            Path(os.environ["FIXTURE_STATE"]).joinpath("child_pid").write_text(str(child.pid))
        connection.sendall(b"ok")
'''


FAKE_SSH = '''#!/usr/bin/env python3
import json,socket,sys
args=sys.argv[1:]
request={"operation":args[args.index("-O")+1]}
if "-L" in args:request["port"]=int(args[args.index("-L")+1].split(":")[1])
with socket.socket(socket.AF_UNIX) as connection:
    connection.connect(args[args.index("-S")+1])
    connection.sendall(json.dumps(request).encode())
    assert connection.recv(4096)==b"ok"
'''


@pytest.mark.parametrize('stop_signal', [signal.SIGINT, signal.SIGTERM])
def test_cli_owned_tunnel_ready_and_signal_cleanup_with_fake_gcloud(access, tmp_path, stop_signal):
    bindir = tmp_path/'bin'
    bindir.mkdir()
    for name, content in [('gcloud', FAKE_GCLOUD), ('ssh', FAKE_SSH)]:
        path = bindir/name
        path.write_text(content)
        path.chmod(0o755)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    log = tmp_path/'helper.log'
    env = {**os.environ, 'PATH':str(bindir)+':'+os.environ['PATH'], 'CLOUD_SHELL':'true',
           'FIXTURE_STATE':str(tmp_path), 'PYTHONUNBUFFERED':'1'}
    with http_server() as (foreign, _), log.open('w') as output:
        helper = subprocess.Popen([sys.executable, str(SCRIPT), '--port', str(port), '--wait-seconds', '2'],
                                  env=env, stdout=output, stderr=output)
        try:
            deadline = time.monotonic()+10
            while time.monotonic()<deadline:
                if '접속 준비 완료' in log.read_text():break
                if helper.poll() is not None:pytest.fail(log.read_text())
                time.sleep(.05)
            else:pytest.fail(log.read_text())
            assert f'포트 {port} → VM 포트 8501' in log.read_text()
            assert access.healthy(port)
            helper.send_signal(stop_signal)
            assert helper.wait(timeout=6) == 130
            assert '종료 명령을 보내지 않았습니다' in log.read_text()
            assert access.healthy(foreign)
            assert not access.healthy(port)
            for filename in ('master_pid', 'child_pid'):
                pid = int((tmp_path/filename).read_text())
                stat = Path(f'/proc/{pid}/stat')
                assert not stat.exists() or stat.read_text().split(') ', 1)[1].split()[0] == 'Z'
        finally:
            if helper.poll() is None:
                helper.send_signal(signal.SIGTERM)
                helper.wait(timeout=6)


def test_cli_rejects_invalid_ports_before_any_cloud_request(access):
    with pytest.raises(SystemExit) as exc:
        access.main(['--port', '80'])
    assert exc.value.code == 2

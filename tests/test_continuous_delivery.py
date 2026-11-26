import importlib.util
from pathlib import Path
from unittest.mock import Mock
import subprocess

import pytest


def module():
    spec = importlib.util.spec_from_file_location('sq_cd_test', 'deploy/gcp/continuous_delivery.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_record(**extra):
    return dict(id=42, head_sha='a'*40, event='push', head_branch='main',
                path='.github/workflows/ci.yml', repository={'full_name': 'taegyoo1104/pennyquant'},
                status='completed', conclusion='success', **extra)


def test_only_latest_successful_main_ci_is_eligible():
    cd = module()
    good = run_record()
    assert cd.ci_passed([good], 'a'*40)
    for key, wrong in [('event', 'pull_request'), ('head_branch', 'feature'), ('conclusion', 'failure'),
                       ('status', 'in_progress'), ('head_sha', 'b'*40), ('path', '.github/workflows/other.yml'),
                       ('repository', {'full_name': 'attacker/repo'})]:
        assert not cd.ci_passed([dict(good, **{key: wrong})], 'a'*40)
    assert not cd.ci_passed([good, dict(good, id=43, conclusion='failure')], 'a'*40)


def test_failed_attempt_stops_even_new_revision(tmp_path, monkeypatch):
    cd = module()
    monkeypatch.setattr(cd, 'STATE', tmp_path)
    (tmp_path/'attempt.json').write_text('{}')
    original = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda p: True if str(p).endswith('cd-enabled') else original(p))
    api = Mock(side_effect=AssertionError('must not fetch or redeploy'))
    monkeypatch.setattr(cd, 'api', api)
    cd.deliver()
    assert 'BLOCKED' in (tmp_path/'status.json').read_text()
    api.assert_not_called()


@pytest.mark.parametrize('command', ['["sh","-c","exec trading"]', '["unexpected"]'])
def test_active_runtime_is_never_stopped(tmp_path, monkeypatch, command):
    cd = module()
    monkeypatch.setattr(Path, 'exists', lambda p: False)
    monkeypatch.setattr(cd.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess([], 3))
    import builtins
    original = builtins.open
    monkeypatch.setattr(builtins, 'open', lambda p, mode: original(tmp_path/Path(p).name, mode))
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return command if args[:2] == ['docker', 'inspect'] else 'app'
    monkeypatch.setattr(cd, 'run', run)
    assert cd.readiness() == 'TRADING_RUNTIME_ACTIVE'
    assert not any('stop' in args for args in calls)


def test_public_compose_has_no_admin_environment_or_writable_volume():
    import yaml
    viewer = yaml.safe_load(Path('deploy/gcp/compose.public.yaml').read_text())['services']['viewer']
    assert viewer['env_file'] == '/etc/sessionquant/public.env'
    assert viewer['read_only'] is True
    assert not viewer.get('volumes')
    assert viewer['command'] == ['python', '-m', 'sessionquant.public_dashboard']


def test_public_route_cannot_expose_admin():
    spec = importlib.util.spec_from_file_location('routes', 'deploy/gcp/check_public_routes.py')
    routes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routes)
    config = {'AllowFunnel': {'node:8443': True}, 'Web': {
        'node:8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8502'}}}}}
    routes.validate(config, ready=True)
    config['AllowFunnel']['node:443'] = True
    with pytest.raises(ValueError):
        routes.validate(config, ready=True)
    del config['AllowFunnel']['node:443']
    config['Web']['node:8443']['Handlers']['/']['Proxy'] = 'http://127.0.0.1:8501'
    with pytest.raises(ValueError):
        routes.validate(config, ready=True)


def test_failed_deployment_keeps_marker_and_does_not_repeat(tmp_path, monkeypatch):
    cd = module()
    monkeypatch.setattr(cd, 'STATE', tmp_path)
    original = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda p: True if str(p).endswith('cd-enabled') else original(p))
    sha = 'a'*40
    monkeypatch.setattr(cd, 'api', lambda path: {'sha': sha} if path == 'commits/main' else {'workflow_runs': [run_record()]})
    monkeypatch.setattr(cd, 'readiness', lambda: None)
    def run(args, **kwargs):
        if args == ['git', 'rev-parse', 'HEAD']:
            return 'b'*40
        if args == ['git', 'rev-parse', 'origin/main']:
            return sha
        return '#!/bin/bash\nexit 1' if 'show' in args else ''
    monkeypatch.setattr(cd, 'run', run)
    deploy = Mock(side_effect=subprocess.CalledProcessError(1, 'updater'))
    monkeypatch.setattr(cd.subprocess, 'run', deploy)
    with pytest.raises(subprocess.CalledProcessError):
        cd.deliver()
    assert (tmp_path/'attempt.json').exists()
    assert 'FAILED' in (tmp_path/'status.json').read_text()
    cd.deliver()
    assert deploy.call_count == 1
    assert 'BLOCKED' in (tmp_path/'status.json').read_text()


def test_readiness_deferral_creates_no_attempt_or_backup(tmp_path, monkeypatch):
    cd = module()
    monkeypatch.setattr(cd, 'STATE', tmp_path)
    original = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda p: True if str(p).endswith('cd-enabled') else original(p))
    monkeypatch.setattr(cd, 'api', lambda path: {'sha': 'a'*40} if path == 'commits/main' else {'workflow_runs': [run_record()]})
    monkeypatch.setattr(cd, 'run', lambda *a, **kw: 'b'*40)
    monkeypatch.setattr(cd, 'readiness', lambda: 'MAINTENANCE_RUNNING')
    deploy = Mock(side_effect=AssertionError('must not deploy'))
    monkeypatch.setattr(cd.subprocess, 'run', deploy)
    cd.deliver()
    assert not (tmp_path/'attempt.json').exists()
    assert 'DEFERRED' in (tmp_path/'status.json').read_text()
    assert 'MAINTENANCE_RUNNING' in (tmp_path/'status.json').read_text()
    deploy.assert_not_called()


def test_successful_deployment_checks_revision_health_and_clears_marker(tmp_path, monkeypatch):
    cd = module()
    monkeypatch.setattr(cd, 'STATE', tmp_path)
    original = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda p: True if str(p).endswith('cd-enabled') else original(p))
    monkeypatch.setattr(cd, 'api', lambda path: {'sha': 'a'*40} if path == 'commits/main' else {'workflow_runs': [run_record()]})
    monkeypatch.setattr(cd, 'readiness', lambda: None)
    heads = iter(['b'*40, 'a'*40])
    def run(args, **kwargs):
        if args == ['git', 'rev-parse', 'HEAD']:
            return next(heads)
        if args == ['git', 'rev-parse', 'origin/main']:
            return 'a'*40
        if args[:2] == ['docker', 'inspect']:
            return '["python","-m","sessionquant.inspection_dashboard"]'
        return '#!/bin/bash\nexit 0' if 'show' in args else ''
    monkeypatch.setattr(cd, 'run', run)
    def deploy(args, **kwargs):
        assert (tmp_path/'attempt.json').is_file()
        assert args[-2:] == ['a'*40, '--off-hours']
    monkeypatch.setattr(cd.subprocess, 'run', deploy)
    from contextlib import nullcontext
    response = Mock(status=200)
    response.read.return_value = b'ok'
    monkeypatch.setattr(cd, 'urlopen', lambda *a, **kw: nullcontext(response))
    cd.deliver()
    assert not (tmp_path/'attempt.json').exists()
    assert 'COMPLETE' in (tmp_path/'status.json').read_text()

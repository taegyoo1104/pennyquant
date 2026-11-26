"""GCP can provide its kernel hostname without an /etc/hostname file."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(name, f'deploy/gcp/{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize('name', ['public_setup', 'continuous_delivery'])
@pytest.mark.parametrize('hostname', ['sessionquant-paper', 'sessionquant-paper.us-central1-a.c.sessionquant.internal'])
def test_kernel_identity_without_hostname_file(name, hostname, monkeypatch, tmp_path):
    mod = load(name)
    monkeypatch.setattr(mod.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(mod.os, 'uname', lambda: SimpleNamespace(nodename=hostname))
    read = Path.read_text
    def missing(path, *args, **kwargs):
        if str(path) == '/etc/hostname':
            raise FileNotFoundError('/etc/hostname')
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', missing)
    if name == 'public_setup':
        monkeypatch.setattr(mod.os, 'isatty', lambda _: False)
        command = Mock(side_effect=AssertionError('no DB or subprocess before interactive setup'))
        monkeypatch.setattr(mod.subprocess, 'check_output', command)
        with pytest.raises(SystemExit, match='PuTTY'):
            mod.main()
        command.assert_not_called()
    else:
        import builtins
        original = builtins.open
        monkeypatch.setattr(builtins, 'open', lambda path, mode: original(tmp_path/'cd.lock', mode))
        deliver = Mock()
        monkeypatch.setattr(mod, 'deliver', deliver)
        mod.main()
        deliver.assert_called_once_with()


@pytest.mark.parametrize('name', ['public_setup', 'continuous_delivery'])
@pytest.mark.parametrize('uid,hostname', [(1000, 'sessionquant-paper'), (0, 'sessionquant-recovery'), (0, 'sessionquant-paper-fake')])
def test_wrong_host_and_nonroot_still_rejected(name, uid, hostname, monkeypatch):
    mod = load(name)
    monkeypatch.setattr(mod.os, 'geteuid', lambda: uid)
    monkeypatch.setattr(mod.os, 'uname', lambda: SimpleNamespace(nodename=hostname))
    with pytest.raises(SystemExit, match='sessionquant-paper'):
        mod.main()

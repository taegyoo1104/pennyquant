"""Running image identity must not follow mutable checkout/env/DB state."""
from datetime import datetime, timezone
import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest
from streamlit.testing.v1 import AppTest

from sessionquant import build_info
from sessionquant.dashboard.version_panel import version_summary


ROOT = Path(__file__).parents[1]
SHA = '1234567890abcdef1234567890abcdef12345678'
NOW = datetime(2026, 11, 20, 5, 30, tzinfo=timezone.utc)


def manifest(tmp_path, *, source='reviewed_archive', revision=SHA):
    path = tmp_path/'_build_info.json'
    value = build_info.write_build_info(path, project_file=ROOT/'pyproject.toml',
        source=source, revision=revision, now=NOW)
    return path, value


def test_baked_revision_ignores_environment_and_current_checkout(tmp_path, monkeypatch):
    path, baked = manifest(tmp_path)
    monkeypatch.setattr(build_info, 'MANIFEST', path)
    monkeypatch.chdir(tmp_path)
    (tmp_path/'.git').mkdir()
    (tmp_path/'.git/HEAD').write_text('f'*40)
    monkeypatch.setenv('SQ_BUILD_REVISION', 'e'*40)
    monkeypatch.setenv('SQ_BUILD_SOURCE', 'working_tree')
    monkeypatch.setenv('GIT_COMMIT', 'd'*40)
    assert build_info.read_build_info() == baked
    display = version_summary(baked)
    assert display['revision'] == SHA and display['revision_label'] == '실행 코드'
    assert display['built_at'] == '2026-11-20 14:30:00 KST'
    assert display['revision_url'].endswith('/commit/'+SHA)


@pytest.mark.parametrize('source,revision', [('working_tree', SHA), ('working_tree', ''), ('unknown', '')])
def test_mutable_and_generic_builds_do_not_claim_exact_reviewed_code(tmp_path, source, revision):
    path, _ = manifest(tmp_path, source=source, revision=revision)
    display = version_summary(build_info.read_build_info(path))
    assert display['revision_label'] != '실행 코드'
    assert '수정 포함 가능' in display['source_label'] if source == 'working_tree' else display['source_label'] == '코드 버전 미확인'
    assert display['revision'] == (revision or None)


@pytest.mark.parametrize('change', [
    {'schema_version': True}, {'schema_version': 2}, {'source': 'ENV'},
    {'revision': 'main'}, {'revision': None}, {'source': 'unknown'},
    {'built_at': '2026-11-20T05:30:00'}, {'built_at': None},
    {'version': '<script>bad</script>'},
])
def test_invalid_build_identity_becomes_unknown_without_echoing_input(tmp_path, change):
    path, value = manifest(tmp_path)
    path.write_text(json.dumps({**value, **change}))
    loaded = build_info.read_build_info(path)
    assert loaded['source'] == 'unknown' and loaded['revision'] is None and loaded['built_at'] is None


@pytest.mark.parametrize('content', [None, b'invalid', b'\xff', b'{}', b'[]', b'x'*4097])
def test_missing_old_or_corrupt_manifest_does_not_fall_back_to_environment(tmp_path, monkeypatch, content):
    path = tmp_path/'missing.json'
    if content is not None:
        path.write_bytes(content)
    monkeypatch.setenv('SQ_BUILD_REVISION', SHA)
    assert build_info.read_build_info(path)['revision'] is None


def test_reviewed_archive_requires_full_revision_and_aware_build_time(tmp_path):
    with pytest.raises(ValueError):
        manifest(tmp_path, revision='')
    with pytest.raises(ValueError):
        build_info.write_build_info(tmp_path/'invalid.json', project_file=ROOT/'pyproject.toml',
            source='reviewed_archive', revision=SHA, now=NOW.replace(tzinfo=None))


def test_settings_version_remains_visible_when_database_read_fails(monkeypatch, tmp_path):
    _, baked = manifest(tmp_path)
    from sessionquant.dashboard import session_app, version_panel
    monkeypatch.setattr(version_panel, 'read_build_info', lambda: baked)
    monkeypatch.setattr(session_app, 'load_database', lambda *a, **kw: session_app.DashboardData(error='DB 연결 실패'))
    app = AppTest.from_string(
        "from pathlib import Path\nfrom sessionquant.dashboard.session_app import render_dashboard\n"
        "render_dashboard(Path('unused.db'), selected_page='시스템·설정')\n").run()
    assert not app.exception and app.error[0].value == 'DB 연결 실패'
    assert any(item.value == '실행 중인 프로그램' for item in app.subheader)
    assert any(SHA in item.value and '실행 코드' in item.value for item in app.markdown)


def test_baked_manifest_is_packaged_and_read_from_installed_code_outside_checkout(tmp_path):
    """Exercise packaging instead of merely checking a package-data string."""
    source = tmp_path/'source'
    package = source/'sessionquant'
    package.mkdir(parents=True)
    shutil.copy2(ROOT/'pyproject.toml', source/'pyproject.toml')
    shutil.copy2(ROOT/'sessionquant/__init__.py', package/'__init__.py')
    shutil.copy2(ROOT/'sessionquant/build_info.py', package/'build_info.py')
    (source/'README.md').write_text('Build metadata fixture\n')
    build_info.write_build_info(package/'_build_info.json', project_file=source/'pyproject.toml',
        source='reviewed_archive', revision=SHA, now=NOW)
    wheels = tmp_path/'wheels'
    # uv-created test environments may omit packaging tools even when their base
    # Python has them. Building a local wheel installs nothing into either one.
    builder = sys.executable if all(importlib.util.find_spec(name) for name in ('pip', 'setuptools', 'wheel')) else sys._base_executable
    probe = subprocess.run([builder, '-c', 'import importlib.util; import sys; sys.exit(not all(importlib.util.find_spec(n) for n in ("pip","setuptools","wheel")))'],
        capture_output=True, timeout=10)
    if probe.returncode:
        pytest.skip('No local wheel build backend; Docker build smoke verifies packaging separately')
    result = subprocess.run([builder, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
        '--no-cache-dir', '--no-index', '--wheel-dir', str(wheels), str(source)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    wheel = next(wheels.glob('sessionquant-*.whl'))
    installed = tmp_path/'installed'
    with zipfile.ZipFile(wheel) as archive:
        assert json.loads(archive.read('sessionquant/_build_info.json'))['revision'] == SHA
        archive.extractall(installed)
    elsewhere = tmp_path/'elsewhere'
    elsewhere.mkdir()
    result = subprocess.run([sys.executable, '-c',
        'import json; from sessionquant import build_info; print(json.dumps(build_info.read_build_info())); print(build_info.__file__)'],
        cwd=elsewhere, env={**os.environ, 'PYTHONPATH': str(installed), 'SQ_BUILD_REVISION': 'f'*40},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert json.loads(lines[0])['revision'] == SHA
    assert Path(lines[1]).is_relative_to(installed)

"""Cross-process exclusion, crash recovery and concurrent capture readers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sessionquant.capture import capture_file
from sessionquant.cli import metadata
from sessionquant.runtime import engine_lease

ROOT = Path(__file__).resolve().parents[1]
CHILD = """
import json, sys
from pathlib import Path
from sessionquant.capture import capture_file
from sessionquant.runtime import engine_lease
manager = engine_lease if sys.argv[1] == 'engine' else capture_file
try:
    with manager(Path(sys.argv[2])):
        print(json.dumps({'acquired': True}))
except (RuntimeError, ValueError) as exc:
    print(json.dumps({'acquired': False, 'message': str(exc)}))
"""


def child(code: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run([sys.executable, "-c", code, *args], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.parametrize("kind,manager,error", [
    ("engine", engine_lease, "another engine"),
    ("capture", capture_file, "Another collector"),
])
def test_second_process_is_blocked_and_can_acquire_after_release(tmp_path, kind, manager, error):
    path = tmp_path / "한글 경로와 공백" / ("test.db" if kind == "engine" else "feed.jsonl")
    with manager(path):
        response = json.loads(child(CHILD, kind, str(path)).stdout)
        assert response["acquired"] is False
        assert error in response["message"]
    response = json.loads(child(CHILD, kind, str(path)).stdout)
    assert response["acquired"] is True


def test_capture_lock_does_not_block_a_simultaneous_reader(tmp_path):
    path = tmp_path / "feed.jsonl"
    with capture_file(path) as stream:
        stream.write('recognizable\n')
        stream.flush()
        reader = child("from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text(encoding='utf-8'))", str(path))
        assert reader.stdout == "recognizable\n\n"


def test_exception_releases_engine_lease(tmp_path):
    path = tmp_path / "paper.db"
    with pytest.raises(ValueError, match="body failed"):
        with engine_lease(path):
            raise ValueError("body failed")
    with engine_lease(path):
        pass


def test_abrupt_process_exit_releases_engine_lease(tmp_path):
    path = tmp_path / "paper.db"
    child("from sessionquant.runtime import engine_lease; import os, sys\nwith engine_lease(sys.argv[1]):\n    os._exit(0)\n", str(path))
    with engine_lease(path):
        pass


def test_metadata_reads_utf8_under_a_legacy_windows_default(tmp_path, monkeypatch):
    path = tmp_path / "입력.jsonl"
    path.with_suffix(".meta.json").write_text(json.dumps({"source_label": "한글 합성 데이터"}, ensure_ascii=False), encoding="utf-8")
    original = Path.read_text
    def legacy_read(self, encoding=None, *args, **kwargs):
        return original(self, encoding=encoding or "cp949", *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", legacy_read)
    assert metadata(path)["source_label"] == "한글 합성 데이터"

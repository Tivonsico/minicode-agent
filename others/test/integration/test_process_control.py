"""Project shutdown must kill its owner, never unrelated or stale PIDs."""

import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from backend.runtime.process import stop
from backend.storage.project_lock import ProjectLock

ROOT = Path(__file__).resolve().parents[3]


def child(root, *, legacy=False):
    code = """
import subprocess, sys, time
from pathlib import Path
from backend.storage.project_lock import ProjectLock
lock = ProjectLock(Path(sys.argv[1])).acquire()
if sys.argv[2] == "legacy":
    lock.file.seek(0)
    lock.file.truncate()
    lock.file.write(b"0")
    lock.file.flush()
nested = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(nested.pid, flush=True)
sys.stdin.read()
nested.terminate()
nested.wait()
lock.release()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(root), "legacy" if legacy else "current"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    nested = int(process.stdout.readline().strip())
    return process, nested


def test_shutdown_terminates_owner_and_children_and_releases_lock(tmp_path):
    process, nested = child(tmp_path)
    try:
        assert stop(tmp_path)
        process.wait(timeout=5)
        assert (
            not psutil.pid_exists(nested) or psutil.Process(nested).status() == psutil.STATUS_ZOMBIE
        )
        with ProjectLock(tmp_path):
            pass
        assert not stop(tmp_path)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX graceful signal; Windows uses forced fallback")
def test_shutdown_allows_owner_to_release_persistent_resources_before_exit(tmp_path):
    code = """
import signal, sys, time
from pathlib import Path
from backend.storage.project_lock import ProjectLock
root = Path(sys.argv[1])
lock = ProjectLock(root).acquire()
def close(*args):
    (root / 'released').write_text('database ownership released')
    lock.release()
    raise SystemExit(0)
signal.signal(signal.SIGINT, close)
print('ready', flush=True)
while True:
    time.sleep(1)
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        assert stop(tmp_path)
        process.wait(timeout=5)
        assert (tmp_path / "released").read_text() == "database ownership released"
        with ProjectLock(tmp_path):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX graceful signal; Windows uses forced fallback")
def test_shutdown_force_stops_an_unresponsive_owner_after_grace_timeout(tmp_path):
    code = """
import signal, sys, time
from pathlib import Path
from backend.storage.project_lock import ProjectLock
lock = ProjectLock(Path(sys.argv[1])).acquire()
signal.signal(signal.SIGINT, signal.SIG_IGN)
print('ready', flush=True)
while True:
    time.sleep(1)
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        assert stop(tmp_path, grace_timeout=0.1)
        process.wait(timeout=5)
        with ProjectLock(tmp_path):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_stale_owner_cannot_kill_an_unrelated_process(tmp_path):
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with ProjectLock(tmp_path) as lock:
            path = lock.path
        path.write_text(
            json.dumps(
                {
                    "pid": unrelated.pid,
                    "created_at": psutil.Process(unrelated.pid).create_time(),
                    "project": str(tmp_path),
                }
            )
        )
        assert not stop(tmp_path)
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


def test_legacy_lock_owner_can_be_stopped_without_port_guessing(tmp_path):
    process, nested = child(tmp_path, legacy=True)
    try:
        assert stop(tmp_path)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_shutdown_does_not_create_state_for_an_unused_project(tmp_path):
    assert not stop(tmp_path)
    assert not (tmp_path / ".minicode").exists()


def test_start_replaces_existing_owner_and_prints_selected_port(tmp_path, monkeypatch, capsys):
    import uvicorn

    from app import main as app_main
    from backend.config import Config

    process, nested = child(tmp_path)
    config = Config(tmp_path, "test-key", context_window=10000, max_output_tokens=1000)
    monkeypatch.setattr(app_main, "load_config", lambda _: config)
    observations = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: observations.append(process.poll()))
    try:
        assert app_main.main(["--project", str(tmp_path), "--no-browser", "--port", "9001"]) == 0
        assert observations and observations[0] is not None
        output = capsys.readouterr().out
        assert "已终止旧进程" in output and "http://127.0.0.1:9001" in output
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_stop_script_works_outside_project_without_configuration(tmp_path):
    process, nested = child(tmp_path)
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "stop.py"), "--project", str(tmp_path)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0 and "已终止" in result.stdout
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

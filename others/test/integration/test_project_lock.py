import subprocess
import sys
from pathlib import Path

from memory.project_lock import ProjectLock


def attempt(root):
    code = "from pathlib import Path; from memory.project_lock import ProjectLock; import sys; lock=ProjectLock(Path(sys.argv[1])); lock.acquire(); lock.release()"
    return subprocess.run(
        [sys.executable, "-c", code, str(root)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=10,
    )


def test_second_process_refused_until_lock_released(tmp_path):
    lock = ProjectLock(tmp_path).acquire()
    try:
        assert attempt(tmp_path).returncode != 0
    finally:
        lock.release()
    assert attempt(tmp_path).returncode == 0


def test_process_exit_releases_lock_and_projects_are_independent(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    code = "from pathlib import Path; from memory.project_lock import ProjectLock; import sys; lock=ProjectLock(Path(sys.argv[1])); lock.acquire(); print('locked', flush=True); sys.stdin.read()"
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(one)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        assert attempt(one).returncode != 0
        assert attempt(two).returncode == 0
    finally:
        child.kill()
        child.communicate(timeout=10)
    assert attempt(one).returncode == 0

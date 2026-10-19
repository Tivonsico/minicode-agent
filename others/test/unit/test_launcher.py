import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "start.py"
spec = importlib.util.spec_from_file_location("minicode_launcher", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.mark.parametrize(
    "platform, relative", [("nt", "Scripts/python.exe"), ("posix", "bin/python")]
)
def test_launcher_selects_project_environment_and_preserves_arguments(
    tmp_path, monkeypatch, platform, relative
):
    python = tmp_path / ".venv" / relative
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name=platform, environ={}))
    calls = []
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda args: calls.append(args) or SimpleNamespace(returncode=7)
    )
    assert launcher.main(["--port", "9001", "--no-browser"]) == 7
    assert calls == [[str(python), str(SCRIPT), "--port", "9001", "--no-browser"]]


def test_launcher_uses_script_root_and_allows_explicit_project(tmp_path, monkeypatch):
    from app import main as app_main

    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(launcher.importlib.util, "find_spec", lambda name: object())
    calls = []
    monkeypatch.setattr(app_main, "main", lambda args: calls.append(args) or 0)
    assert launcher.main(["--project", "other", "--check-config"]) == 0
    assert calls == [["--project", str(tmp_path), "--project", "other", "--check-config"]]


def test_missing_dependencies_reports_installation_without_uv(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(launcher.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    assert launcher.main([]) == 1
    assert "python -m pip install -e ." in capsys.readouterr().out


def test_uv_bootstrap_cannot_recursively_restart(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(launcher.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "uv")
    monkeypatch.setenv(launcher.BOOTSTRAP_FLAG, "1")
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *args, **kwargs: pytest.fail("Bootstrap repeated")
    )
    assert launcher.main([]) == 1
    assert "uv sync" in capsys.readouterr().out

import os
import subprocess
import sys
from pathlib import Path


def invoke(project, **values):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"OPENAI_API_KEY", "CONTEXT_WINDOW", "MAX_OUTPUT_TOKENS", "MODEL"}
    }
    env.update(values, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "app", "--project", str(project), "--check-config"],
        env=env,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def test_startup_missing_key_does_not_create_session(tmp_path):
    result = invoke(tmp_path)
    assert result.returncode == 1
    assert "OPENAI_API_KEY" in result.stdout
    assert not (tmp_path / ".minicode").exists()


def test_local_configuration_check_never_claims_remote_validation(tmp_path):
    result = invoke(
        tmp_path,
        OPENAI_API_KEY="local-placeholder-token",
        CONTEXT_WINDOW="10000",
        MAX_OUTPUT_TOKENS="1000",
        MODEL="GLM-5.3-Flash",
    )
    assert result.returncode == 0
    assert "GLM-5.3-Flash" in result.stdout
    assert "尚未验证服务端" in result.stdout
    assert "local-placeholder-token" not in result.stdout
    assert not (tmp_path / ".minicode").exists()


def test_python_launcher_runs_from_another_directory_without_model_request(tmp_path):
    script = Path(__file__).resolve().parents[3] / "start.py"
    env = {
        **os.environ,
        "OPENAI_API_KEY": "local-placeholder-token",
        "CONTEXT_WINDOW": "10000",
        "MAX_OUTPUT_TOKENS": "1000",
        "MODEL": "GLM-5.3-Flash",
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, str(script), "--project", str(tmp_path), "--check-config"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0
    assert "配置有效" in result.stdout and "尚未验证服务端" in result.stdout
    assert "local-placeholder-token" not in result.stdout
    assert not (tmp_path / ".minicode").exists()

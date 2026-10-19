from app import main as app_main
from backend.config import Config


def test_web_launcher_is_local_and_has_no_windows_console_dependency(tmp_path, monkeypatch):
    import uvicorn

    calls = []
    config = Config(tmp_path, "test-key", context_window=10000, max_output_tokens=1000)
    monkeypatch.setattr(app_main, "load_config", lambda _: config)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    assert app_main.main(["--project", str(tmp_path), "--no-browser", "--port", "9001"]) == 0
    assert calls == [
        {"host": "127.0.0.1", "port": 9001, "log_level": "warning", "timeout_graceful_shutdown": 5}
    ]


def test_invalid_port_does_not_start_backend(tmp_path, monkeypatch):
    config = Config(tmp_path, "test-key", context_window=10000, max_output_tokens=1000)
    monkeypatch.setattr(app_main, "load_config", lambda _: config)
    assert app_main.main(["--no-browser", "--port", "0"]) == 1
    assert not (tmp_path / ".minicode").exists()

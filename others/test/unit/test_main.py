from app import main as app_main


def test_main_reports_missing_windows_console(monkeypatch, capsys):
    class FakeParser:
        def parse_args(self):
            return type("Args", (), {"project": ".", "check_config": False})()

    class FakeArgumentParser:
        def __init__(self, *args, **kwargs):
            pass

        def add_argument(self, *args, **kwargs):
            return None

        def parse_args(self):
            return FakeParser().parse_args()

    monkeypatch.setattr(app_main.argparse, "ArgumentParser", FakeArgumentParser)
    monkeypatch.setattr(app_main, "load_config", lambda _: type("Config", (), {"root": "."})())

    class FakeLock:
        def __init__(self, root):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(app_main, "ProjectLock", FakeLock)

    class FakeTerminal:
        def __init__(self, config):
            pass

        async def run(self):
            raise app_main.NoConsoleScreenBufferError()

    monkeypatch.setitem(__import__("sys").modules, "app.terminal", type("Module", (), {"TerminalApp": FakeTerminal}))
    assert app_main.main() == 1
    assert "未检测到 Windows 控制台" in capsys.readouterr().out

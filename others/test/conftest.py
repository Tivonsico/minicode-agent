import pytest

from backend.tools.executor import ToolExecutor
from test.support.tooling import Outputs


@pytest.fixture
def setup_tools(tmp_path):
    approvals, events = [], []

    async def approve(request):
        approvals.append(request)
        return True

    output = Outputs(tmp_path)
    executor = ToolExecutor(
        tmp_path, output, approve, lambda kind, **data: events.append((kind, data))
    )
    return executor, output, approvals, events


@pytest.fixture(autouse=True)
def isolated_execution_gate(tmp_path, monkeypatch):
    from backend.execution import gate

    monkeypatch.setattr(gate, "lock_directory", lambda: tmp_path.parent / ("gate-" + tmp_path.name))
    monkeypatch.setattr(gate, "mysql_domain_path", lambda: gate.lock_directory() / "mysql-domain.json")

import pytest

from test.support.tooling import Outputs
from tools import ToolExecutor


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

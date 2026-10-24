"""Public Gmail delivery service and model tool behavior; no live mail."""
import pytest

from backend.gmail import GmailService


class MailAPI:
    account_id = "test-account"

    def __init__(self):
        self.deliveries = []

    def send(self, raw):
        self.deliveries.append(raw)
        return {"id": "gmail-123", "threadId": "thread-123"}


def test_sent_mail_survives_restart_and_repeated_preparation(tmp_path):
    api = MailAPI()
    service = GmailService(tmp_path, api=api)
    prepared = service.prepare(to=["2046325952@qq.com"], subject="测试", body="你好")
    sent = service.send(prepared["request_id"])
    restarted = GmailService(tmp_path, api=api)
    again = restarted.prepare(to=["2046325952@qq.com"], subject="测试", body="你好")
    assert again["request_id"] == prepared["request_id"]
    assert restarted.send(again["request_id"])["gmail_message_id"] == "gmail-123"
    assert sent["status"] == "sent"
    assert len(api.deliveries) == 1


def test_ambiguous_send_never_retries_even_after_restart(tmp_path):
    class LostResponse(MailAPI):
        def send(self, raw):
            self.deliveries.append(raw)
            raise TimeoutError("response lost after Gmail accepted email")

    api = LostResponse()
    service = GmailService(tmp_path, api=api)
    prepared = service.prepare(to=["2046325952@qq.com"], subject="Timeout", body="Once")
    assert service.send(prepared["request_id"])["status"] == "unknown"
    restarted = GmailService(tmp_path, api=api)
    assert restarted.send(prepared["request_id"])["status"] == "unknown"
    assert len(api.deliveries) == 1


@pytest.mark.asyncio
async def test_model_prepares_immutable_mail_and_sends_only_after_approval(tmp_path, setup_tools):
    from test.support.tooling import call

    executor, _, approvals, _ = setup_tools
    api = MailAPI()
    executor.gmail = GmailService(tmp_path, api=api)
    prepared = (await executor.execute_batch([call("GmailPrepareEmail", {
        "to": ["2046325952@qq.com"], "subject": "测试", "body": "审批后的邮件",
    })]))[0]
    import json
    request_id = json.loads(prepared.content)["request_id"]
    assert not api.deliveries
    result = (await executor.execute_batch([call("GmailSendEmail", {"request_id": request_id})]))[0]
    assert result.status == "succeeded"
    assert json.loads(result.content)["status"] == "sent"
    assert len(approvals) == 1
    assert "2046325952@qq.com" in approvals[0]["details"]
    assert "审批后的邮件" in approvals[0]["details"]
    await executor.execute_batch([call("GmailSendEmail", {"request_id": request_id}, "retry")])
    assert len(api.deliveries) == 1
    assert len(approvals) == 1


def test_concurrent_senders_claim_delivery_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()

    class SlowAPI(MailAPI):
        def send(self, raw):
            entered.set()
            assert release.wait(5)
            return super().send(raw)

    api = SlowAPI()
    service = GmailService(tmp_path, api=api)
    request = service.prepare(to=["2046325952@qq.com"], subject="Concurrent", body="Once")
    with ThreadPoolExecutor() as pool:
        first = pool.submit(service.send, request["request_id"])
        assert entered.wait(5)
        try:
            assert GmailService(tmp_path, api=api).send(request["request_id"])["status"] == "unknown"
        finally:
            release.set()
        assert first.result()["status"] == "sent"
    assert len(api.deliveries) == 1


@pytest.mark.asyncio
async def test_auto_mode_still_requires_approval_and_plan_cannot_send(tmp_path, setup_tools):
    from test.support.tooling import call

    executor, *_ = setup_tools
    executor.gmail = GmailService(tmp_path, api=MailAPI())
    prepared = executor.gmail.prepare(to=["2046325952@qq.com"], subject="Denied", body="No send")
    approvals = []

    async def deny(request):
        approvals.append(request)
        return False

    executor.approve = deny
    executor.agent_mode = "auto"
    result = (await executor.execute_batch([
        call("GmailSendEmail", {"request_id": prepared["request_id"]})
    ]))[0]
    assert result.status == "rejected"
    assert len(approvals) == 1
    assert executor.gmail.status(prepared["request_id"])["status"] == "prepared"
    executor.reset_turn()
    executor.agent_mode = "plan"
    result = (await executor.execute_batch([
        call("GmailSendEmail", {"request_id": prepared["request_id"]})
    ]))[0]
    assert result.error_type == "permission"
    assert "GmailSendEmail" not in [s["function"]["name"] for s in executor.schemas()]


def test_crash_after_claim_blocks_replay(tmp_path):
    class CrashAPI(MailAPI):
        def send(self, raw):
            raise SystemExit("process died")

    service = GmailService(tmp_path, api=CrashAPI())
    request = service.prepare(to=["2046325952@qq.com"], subject="Crash", body="Once")
    with pytest.raises(SystemExit):
        service.send(request["request_id"])
    api = MailAPI()
    restarted = GmailService(tmp_path, api=api)
    assert restarted.send(request["request_id"])["status"] == "unknown"
    assert not api.deliveries


def test_explicit_resend_is_deduplicated_and_unknown_cannot_be_resent(tmp_path):
    from backend.tools.base import ToolError

    api = MailAPI()
    service = GmailService(tmp_path, api=api)
    args = dict(to=["2046325952@qq.com"], subject="Again", body="Once")
    first = service.prepare(**args)
    with pytest.raises(ToolError):
        service.prepare(**args, resend_of=first["request_id"])
    service.send(first["request_id"])
    second = service.prepare(**args, resend_of=first["request_id"])
    assert second["request_id"] != first["request_id"]
    assert service.prepare(**args, resend_of=first["request_id"])["request_id"] == second["request_id"]
    service.send(second["request_id"])
    assert len(api.deliveries) == 2


def test_account_change_cannot_send_old_approved_intent(tmp_path):
    from backend.tools.base import ToolError

    api = MailAPI()
    service = GmailService(tmp_path, api=api)
    request = service.prepare(to=["2046325952@qq.com"], subject="Identity", body="Once")
    api.account_id = "different-account"
    with pytest.raises(ToolError, match="account changed"):
        service.send(request["request_id"])
    assert not api.deliveries


@pytest.mark.parametrize("changes", [
    {"to": ["victim@example.com\r\nBcc: other@example.com"]},
    {"subject": "Hi\nBcc: other@example.com"},
    {"to": []}, {"body": ""},
])
def test_invalid_mail_never_reaches_api(tmp_path, changes):
    from backend.tools.base import ToolError

    api = MailAPI()
    service = GmailService(tmp_path, api=api)
    args = dict(to=["2046325952@qq.com"], subject="Valid", body="Hello") | changes
    with pytest.raises(ToolError):
        service.prepare(**args)
    assert not api.deliveries


@pytest.mark.parametrize("http_status, response_body, expected", [
    (None, None, "unknown"),
    (401, {"error": {"message": "expired"}}, "unknown"),
    (503, {"error": {"message": "unavailable"}}, "unknown"),
    (307, {}, "unknown"),
    (200, {}, "unknown"),
    (200, {"id": "gmail-accepted"}, "sent"),
])
def test_official_sdk_sends_once_and_records_response(
    tmp_path, monkeypatch, http_status, response_body, expected
):
    import json

    import httpx

    from backend.gmail.google_api import GoogleGmailAPI, save_token, token_path

    path = token_path(tmp_path)
    save_token(path, {
        "token": "fake-access-token", "refresh_token": "fake-refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake.apps.googleusercontent.com", "client_secret": "fake-secret",
        "expiry": "2099-01-01T00:00:00Z", "minicode_account_id": "test",
        "scopes": ["https://www.googleapis.com/auth/gmail.send"],
    })
    posts = []

    def disconnected(request):
        posts.append(request)
        if http_status is None:
            raise httpx.RemoteProtocolError("server closed after accepting POST")
        return httpx.Response(http_status, json=response_body, headers={
            "content-type": "application/json", "location": "https://gmail.googleapis.com/redirect",
        })

    client = httpx.Client(transport=httpx.MockTransport(disconnected))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    service = GmailService(tmp_path, api=GoogleGmailAPI(tmp_path))
    prepared = service.prepare(to=["2046325952@qq.com"], subject="Transport", body="Once")
    assert service.send(prepared["request_id"])["status"] == expected
    assert len(posts) == 1
    assert posts[0].method == "POST"
    assert posts[0].url.host == "gmail.googleapis.com"
    assert "raw" in json.loads(posts[0].content)

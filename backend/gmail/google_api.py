"""Google's maintained Python SDK; OAuth tokens stay outside model context."""
import json
import os
import uuid
from pathlib import Path

from backend.storage.files import internal_directory, sync_directory
from backend.tools.base import ToolError

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class SingleAttemptHTTP:
    """SDK HTTP contract backed by HTTPX, with no replay or redirects.

    httplib2 can replay a POST on BadStatusLine even with SDK retries disabled.
    Credentials are refreshed before this transport is constructed.
    """

    def __init__(self, credentials):
        import httpx

        self.credentials = credentials
        self.client = httpx.Client(timeout=30, follow_redirects=False)

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        from httplib2 import Response

        headers = dict(headers or {})
        self.credentials.apply(headers)
        response = self.client.request(method, uri, content=body, headers=headers)
        return Response({**response.headers, "status": str(response.status_code)}), response.content

    def close(self):
        self.client.close()


def token_path(root):
    return internal_directory(Path(root), "gmail") / "token.json"


def save_token(path, value):
    if path.is_symlink():
        raise ToolError("Gmail token must not be a link.", "gmail_credentials")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class GoogleGmailAPI:
    def __init__(self, root):
        self.path = token_path(root)
        self._account_id = None

    def _token(self):
        if not self.path.exists():
            raise ToolError(
                "Gmail is not connected. Run: python -m backend.gmail connect --credentials /path/to/client.json",
                "gmail_not_connected",
            )
        if self.path.is_symlink():
            raise ToolError("Gmail token must not be a link.", "gmail_credentials")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not value.get("minicode_account_id"):
            raise ToolError("Reconnect Gmail with the connect command.", "gmail_credentials")
        if self._account_id and self._account_id != value["minicode_account_id"]:
            raise ToolError("Gmail connection changed. Restart minicode.", "gmail_account_changed")
        self._account_id = value["minicode_account_id"]
        return value

    @property
    def account_id(self):
        return self._token()["minicode_account_id"]

    def send(self, raw):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        value = self._token()
        credentials = Credentials.from_authorized_user_info(value, SCOPES)
        if not credentials.valid:
            credentials.refresh(Request())
            refreshed = json.loads(credentials.to_json())
            refreshed["minicode_account_id"] = value["minicode_account_id"]
            save_token(self.path, refreshed)
        http = SingleAttemptHTTP(credentials)
        try:
            service = build("gmail", "v1", http=http, cache_discovery=False)
            return service.users().messages().send(userId="me", body={"raw": raw}).execute(num_retries=0)
        finally:
            http.close()


def connect(root, credentials_path):
    from google_auth_oauthlib.flow import InstalledAppFlow

    path = token_path(root)
    path.parent.chmod(0o700)
    if path.exists():
        raise ToolError("Gmail is already connected. Disconnect before changing accounts.", "gmail_connected")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, access_type="offline", prompt="consent", timeout_seconds=300
    )
    value = json.loads(credentials.to_json())
    value["minicode_account_id"] = uuid.uuid4().hex
    save_token(path, value)

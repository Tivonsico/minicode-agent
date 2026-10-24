import base64
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from email.message import EmailMessage
from email.policy import SMTP

from backend.storage.files import internal_directory
from backend.tools.base import ToolError


class GmailService:
    def __init__(self, root, api=None):
        from .google_api import GoogleGmailAPI

        self.api = api or GoogleGmailAPI(root)
        self.directory = internal_directory(root, "gmail")
        self.directory.chmod(0o700)
        self.path = self.directory / "outbox.sqlite3"
        if self.path.is_symlink():
            raise ToolError("Gmail outbox must not be a link.", "gmail_storage")
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                request_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL,
                gmail_message_id TEXT, thread_id TEXT)""")
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA synchronous=FULL")
                yield db
        finally:
            db.close()

    def prepare(self, *, to, subject, body, cc=None, bcc=None, resend_of=None):
        payload = {"to": to, "cc": cc or [], "bcc": bcc or [], "subject": subject, "body": body}
        for field in ("to", "cc", "bcc"):
            addresses = payload[field]
            if not isinstance(addresses, list) or (field == "to" and not addresses):
                raise ToolError("At least one To address is required.", "gmail_validation")
            if len(addresses) > 100:
                raise ToolError("Too many recipients.", "gmail_validation")
            for address in addresses:
                if not isinstance(address, str) or not re.fullmatch(
                    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", address
                ):
                    raise ToolError("Use plain email addresses without display names.", "gmail_validation")
            payload[field] = sorted(set(addresses))
        if not isinstance(subject, str) or not subject.strip() or any(c in subject for c in "\r\n\x00"):
            raise ToolError("Subject must be nonempty and contain no newlines.", "gmail_validation")
        if not isinstance(body, str) or not body.strip() or len(body.encode()) > 1_000_000:
            raise ToolError("Body must be nonempty and at most 1 MB.", "gmail_validation")
        account = self.api.account_id
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        request_id = hashlib.sha256(
            json.dumps([account, text, resend_of], ensure_ascii=False).encode()
        ).hexdigest()
        with self._db() as db:
            if resend_of:
                previous = db.execute(
                    "SELECT * FROM deliveries WHERE request_id=?", (resend_of,)
                ).fetchone()
                if not previous or previous["status"] != "sent" or previous["account_id"] != account:
                    raise ToolError("resend_of must identify a confirmed sent email in this account.", "gmail_validation")
            db.execute(
                "INSERT OR IGNORE INTO deliveries(request_id,account_id,payload,status) VALUES(?,?,?,'prepared')",
                (request_id, account, text),
            )
        return self.status(request_id)

    def status(self, request_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM deliveries WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise ToolError("Unknown Gmail request_id.", "gmail_not_found")
        result = dict(row)
        result.pop("account_id")
        result["email"] = json.loads(result.pop("payload"))
        # A process can die between committing the claim and saving the response.
        if result["status"] == "sending":
            result["status"] = "unknown"
        if result["status"] == "unknown":
            result["notice"] = "Delivery may have happened. Check Gmail Sent manually; never retry with a new intent."
        return result

    def send(self, request_id):
        account = self.api.account_id  # Fail before claiming if not connected.
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM deliveries WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                raise ToolError("Unknown Gmail request_id.", "gmail_not_found")
            if row["account_id"] != account:
                raise ToolError("Gmail account changed; prepare and approve a new email.", "gmail_account_changed")
            if row["status"] != "prepared":
                return self.status(request_id)
            payload = json.loads(row["payload"])
            message = EmailMessage(policy=SMTP)
            for field in ("to", "cc", "bcc"):
                if payload[field]:
                    message[field] = ", ".join(payload[field])
            message["Subject"] = payload["subject"]
            message["Message-ID"] = f"<minicode.{request_id}@minicode.local>"
            message.set_content(payload["body"])
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
            db.execute("UPDATE deliveries SET status='sending' WHERE request_id=?", (request_id,))
        # Commit the exclusive claim BEFORE the network call. Never automatically retry it.
        try:
            response = self.api.send(raw)
            if not isinstance(response.get("id"), str) or not response["id"]:
                raise ValueError("Gmail returned no message ID")
        except Exception:
            with self._db() as db:
                db.execute("UPDATE deliveries SET status='unknown' WHERE request_id=?", (request_id,))
            return self.status(request_id)
        with self._db() as db:
            db.execute(
                "UPDATE deliveries SET status='sent',gmail_message_id=?,thread_id=? WHERE request_id=?",
                (response["id"], response.get("threadId"), request_id),
            )
        return self.status(request_id)

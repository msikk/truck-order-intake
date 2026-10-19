"""GraphMailbox against a fake Microsoft Graph HTTP session (no network)."""

import base64
from types import SimpleNamespace

from truck_orders.config import Settings
from truck_orders.mail import graph as graph_mod
from truck_orders.mail.graph import GraphMailbox

CFG = Settings(
    ms_tenant_id="t", ms_client_id="c", ms_client_secret="s",
    ms_mailbox="orders@example.com", ms_source_folder="inbox", ms_processed_folder="Orders Processed",
)


class FakeMsal:
    def __init__(self, *a, **k):
        pass

    def acquire_token_for_client(self, scopes):
        return {"access_token": "tok"}


class FakeSession:
    def __init__(self):
        self.calls = []
        self.throttled_once = False

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, params, json, dict(headers or {})))
        base = "https://graph.microsoft.com/v1.0/users/orders%40example.com"

        def ok(body=None, content=b""):
            return SimpleNamespace(ok=True, status_code=200, headers={}, json=lambda: body, content=content, text="")

        if method == "GET" and url == f"{base}/mailFolders/inbox/messages":
            if not self.throttled_once:
                self.throttled_once = True
                return SimpleNamespace(ok=False, status_code=429, headers={"Retry-After": "0"}, text="slow down")
            return ok({"value": [
                {"id": "m2", "subject": "Second", "receivedDateTime": "2026-10-19T12:00:00Z",
                 "from": {"emailAddress": {"name": "B", "address": "b@x.com"}},
                 "body": {"content": "body 2"}, "hasAttachments": False},
                {"id": "m1", "subject": "First", "receivedDateTime": "2026-10-19T09:00:00Z",
                 "from": {"emailAddress": {"name": "A", "address": "a@x.com"}},
                 "body": {"content": "body 1"}, "hasAttachments": True},
            ]})
        if method == "GET" and url == f"{base}/messages/m1/attachments":
            return ok({"value": [
                {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a1", "name": "tender.pdf",
                 "contentType": "application/pdf", "size": 9, "isInline": False,
                 "contentBytes": base64.b64encode(b"%PDF-fake").decode()},
                {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a2", "name": "photo.jpg",
                 "contentType": "image/jpeg", "size": 900000, "isInline": False},
                {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a3", "name": "logo.png",
                 "contentType": "image/png", "size": 4000, "isInline": True, "contentBytes": "AA=="},
                {"@odata.type": "#microsoft.graph.itemAttachment", "id": "a4", "name": "Fwd: old"},
            ]})
        if method == "GET" and url == f"{base}/messages/m1/attachments/a2/$value":
            return ok(content=b"\xff\xd8jpeg")
        if method == "GET" and url == f"{base}/mailFolders":
            return ok({"value": []})
        if method == "POST" and url == f"{base}/mailFolders":
            return ok({"id": "folder-123"})
        if method in ("PATCH", "POST"):
            return ok({})
        raise AssertionError(f"unexpected call {method} {url}")


def make(monkeypatch):
    monkeypatch.setattr(graph_mod.msal, "ConfidentialClientApplication", FakeMsal)
    monkeypatch.setattr(graph_mod.time, "sleep", lambda s: None)
    session = FakeSession()
    return GraphMailbox(CFG, session=session), session


def test_reads_unread_oldest_first_with_attachments(monkeypatch):
    mb, session = make(monkeypatch)
    emails = list(mb.unread_messages())

    assert [e.subject for e in emails] == ["First", "Second"]
    first = emails[0]
    assert first.sender == "A <a@x.com>"
    assert [(a.filename, a.data) for a in first.attachments] == [
        ("tender.pdf", b"%PDF-fake"), ("photo.jpg", b"\xff\xd8jpeg"),
    ]  # inline logo and forwarded item skipped; large attachment fetched via $value

    list_call = next(c for c in session.calls if c[1].endswith("/mailFolders/inbox/messages") and c[2])
    assert list_call[2]["$filter"] == "isRead eq false"
    assert list_call[4]["Prefer"] == 'outlook.body-content-type="text"'
    assert list_call[4]["Authorization"] == "Bearer tok"


def test_mark_processed_marks_read_and_moves(monkeypatch):
    mb, session = make(monkeypatch)
    mb.mark_processed("m1")
    methods = [(m, u.rsplit("/", 2)[-2:], j) for m, u, _, j, _ in session.calls]
    assert ("PATCH", ["messages", "m1"], {"isRead": True}) in methods
    assert ("POST", ["orders%40example.com", "mailFolders"], {"displayName": "Orders Processed"}) in methods
    assert ("POST", ["m1", "move"], {"destinationId": "folder-123"}) in methods

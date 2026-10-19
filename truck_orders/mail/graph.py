"""Read and update mail in an Exchange Online mailbox through Microsoft Graph.

Uses app-only (client credentials) auth. The Azure app registration needs the
Microsoft Graph *application* permission `Mail.ReadWrite` with admin consent.
Limit it to the orders mailbox with an Exchange application access policy /
RBAC for Applications so the app cannot read every mailbox in the tenant.
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime
from typing import Iterator
from urllib.parse import quote

import msal
import requests

from ..config import Settings
from ..models import Attachment, EmailMessage

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
WELL_KNOWN_FOLDERS = {"inbox", "archive", "drafts", "sentitems", "deleteditems", "junkemail"}


class GraphMailbox:
    def __init__(self, cfg: Settings, session: requests.Session | None = None):
        cfg.require_graph()
        self.cfg = cfg
        self.mailbox = quote(cfg.ms_mailbox)
        self.session = session or requests.Session()
        self._app = msal.ConfidentialClientApplication(
            cfg.ms_client_id,
            authority=f"https://login.microsoftonline.com/{cfg.ms_tenant_id}",
            client_credential=cfg.ms_client_secret,
        )
        self._folder_ids: dict[str, str] = {}

    # -- HTTP helpers -------------------------------------------------------

    def _token(self) -> str:
        result = self._app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise RuntimeError(f"Graph auth failed: {result.get('error')}: {result.get('error_description')}")
        return result["access_token"]

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        if not url.startswith("http"):
            url = f"{GRAPH}{url}"
        headers = kwargs.pop("headers", {})
        for attempt in range(5):
            headers["Authorization"] = f"Bearer {self._token()}"
            resp = self.session.request(method, url, headers=headers, timeout=60, **kwargs)
            if resp.status_code in (429, 503, 504):
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                log.warning("Graph throttled (%s); retrying in %ss", resp.status_code, wait)
                time.sleep(wait)
                continue
            if not resp.ok:
                raise RuntimeError(f"Graph {method} {url} failed: {resp.status_code} {resp.text[:500]}")
            return resp
        raise RuntimeError(f"Graph {method} {url} kept failing after retries")

    # -- Folders ------------------------------------------------------------

    def folder_id(self, name: str, create: bool = False) -> str:
        """Resolve a folder display name (top level) or well-known name to its id."""
        key = name.lower()
        if key in WELL_KNOWN_FOLDERS:
            return key
        if key in self._folder_ids:
            return self._folder_ids[key]
        escaped = name.replace("'", "''")
        resp = self._request(
            "GET",
            f"/users/{self.mailbox}/mailFolders",
            params={"$filter": f"displayName eq '{escaped}'", "includeHiddenFolders": "true"},
        )
        found = resp.json().get("value", [])
        if found:
            fid = found[0]["id"]
        elif create:
            fid = self._request(
                "POST", f"/users/{self.mailbox}/mailFolders", json={"displayName": name}
            ).json()["id"]
            log.info("Created mail folder %r", name)
        else:
            raise RuntimeError(f"Mail folder {name!r} not found in {self.cfg.ms_mailbox}")
        self._folder_ids[key] = fid
        return fid

    # -- Reading ------------------------------------------------------------

    def unread_messages(self, limit: int = 50) -> Iterator[EmailMessage]:
        folder = self.folder_id(self.cfg.ms_source_folder)
        url = f"/users/{self.mailbox}/mailFolders/{folder}/messages"
        params = {
            "$filter": "isRead eq false",
            "$select": "id,internetMessageId,subject,from,receivedDateTime,body,hasAttachments",
            "$top": str(min(limit, 50)),
        }
        # Ask Graph to return the body as plain text instead of HTML.
        headers = {"Prefer": 'outlook.body-content-type="text"'}

        raw: list[dict] = []
        while url and len(raw) < limit:
            data = self._request("GET", url, params=params, headers=dict(headers)).json()
            raw.extend(data.get("value", []))
            url, params = data.get("@odata.nextLink"), None

        raw.sort(key=lambda m: m.get("receivedDateTime", ""))  # oldest first
        for msg in raw[:limit]:
            yield self._to_email(msg)

    def _to_email(self, msg: dict) -> EmailMessage:
        sender = msg.get("from", {}).get("emailAddress", {})
        received = msg.get("receivedDateTime")
        return EmailMessage(
            message_id=msg["id"],
            internet_message_id=msg.get("internetMessageId", ""),
            subject=msg.get("subject") or "",
            sender=f"{sender.get('name', '')} <{sender.get('address', '')}>".strip(),
            received_at=datetime.fromisoformat(received.replace("Z", "+00:00")) if received else None,
            body_text=(msg.get("body") or {}).get("content", ""),
            attachments=self._attachments(msg["id"]) if msg.get("hasAttachments") else [],
            source="exchange",
        )

    def _attachments(self, message_id: str) -> list[Attachment]:
        resp = self._request("GET", f"/users/{self.mailbox}/messages/{message_id}/attachments")
        out: list[Attachment] = []
        for att in resp.json().get("value", []):
            if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                # itemAttachment (forwarded email) / referenceAttachment (OneDrive link)
                log.info("Skipping non-file attachment %r on %s", att.get("name"), message_id)
                continue
            if att.get("isInline") and (att.get("contentType") or "").startswith("image/") and att.get("size", 0) < 20_000:
                continue  # signature logos and similar
            if att.get("contentBytes"):
                data = base64.b64decode(att["contentBytes"])
            else:
                data = self._request(
                    "GET", f"/users/{self.mailbox}/messages/{message_id}/attachments/{att['id']}/$value"
                ).content
            out.append(Attachment(att.get("name") or "attachment", att.get("contentType") or "", data))
        return out

    # -- Updating -----------------------------------------------------------

    def mark_processed(self, message_id: str) -> None:
        self._request("PATCH", f"/users/{self.mailbox}/messages/{message_id}", json={"isRead": True})
        if self.cfg.ms_processed_folder:
            dest = self.folder_id(self.cfg.ms_processed_folder, create=True)
            self._request(
                "POST", f"/users/{self.mailbox}/messages/{message_id}/move", json={"destinationId": dest}
            )

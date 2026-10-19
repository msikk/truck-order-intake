"""Read .eml files from disk - handy for testing without an Exchange connection."""

from __future__ import annotations

import email
import email.policy
import re
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

from ..models import Attachment, EmailMessage


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", html)
    html = re.sub(r"(?i)</td>|</th>", "\t", html)
    text = unescape(re.sub(r"<[^>]+>", "", html))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def read_eml(path: Path) -> EmailMessage:
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)

    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = html_to_text(body)

    attachments = []
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True)
        if data:
            attachments.append(
                Attachment(part.get_filename() or "attachment", part.get_content_type(), data)
            )

    received = None
    if msg["Date"]:
        try:
            received = parsedate_to_datetime(msg["Date"])
        except (TypeError, ValueError):
            pass

    return EmailMessage(
        message_id=f"file:{path.resolve()}",
        internet_message_id=msg["Message-ID"] or "",
        subject=msg["Subject"] or "",
        sender=msg["From"] or "",
        received_at=received,
        body_text=body,
        attachments=attachments,
        source="eml",
    )

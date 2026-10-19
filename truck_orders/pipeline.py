"""Process one email end to end: extract with Claude, save to SQLite."""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import settings
from .extractor import ExtractionError, OrderExtractor, guess_content_type
from .models import EmailMessage, ShippingOrder

log = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    status: str  # order_created | not_an_order | error | skipped
    order_numbers: list[str] = field(default_factory=list)
    detail: str = ""


def review_reasons(order: ShippingOrder, threshold: float) -> list[str]:
    """Why a dispatcher should look at this order before it is booked (empty = looks complete)."""
    reasons = []
    if order.confidence < threshold:
        reasons.append(f"low confidence ({order.confidence:.2f})")
    if not (order.shipper.address.city or order.shipper.address.postal_code):
        reasons.append("pickup location missing")
    if not (order.consignee.address.city or order.consignee.address.postal_code):
        reasons.append("delivery location missing")
    if not order.pickup_date:
        reasons.append("pickup date missing")
    if order.equipment_type.value == "unknown":
        reasons.append("equipment type unknown")
    if order.equipment_type.value == "reefer" and order.temperature_min_f is None and order.temperature_max_f is None:
        reasons.append("reefer without temperature")
    return reasons


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120] or "attachment"


def _save_attachments(conn: sqlite3.Connection, email_id: int, email: EmailMessage, skipped: list[str]) -> None:
    folder = settings.attachment_dir / str(email_id)
    skipped_names = {s.split(" (")[0] for s in skipped}
    for i, att in enumerate(email.attachments, start=1):
        path: Path | None = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{i:02d}_{_safe_name(att.filename)}"
            path.write_bytes(att.data)
        except OSError as exc:
            log.warning("Could not save attachment %s: %s", att.filename, exc)
            path = None
        db.record_attachment(
            conn, email_id, att.filename, guess_content_type(att), len(att.data),
            str(path) if path else None, att.filename not in skipped_names,
        )


def process_email(conn: sqlite3.Connection, extractor: OrderExtractor, email: EmailMessage, force: bool = False) -> ProcessResult:
    if not force and db.email_already_done(conn, email.message_id):
        return ProcessResult("skipped", detail="already processed")

    log.info("Processing %r from %s (%d attachments)", email.subject, email.sender, len(email.attachments))
    try:
        extraction = extractor.extract(email)
    except ExtractionError as exc:
        with conn:
            db.record_email(conn, email, "error", error=str(exc))
        return ProcessResult("error", detail=str(exc))

    result = extraction.result
    usage = dict(model=extraction.model, input_tokens=extraction.input_tokens, output_tokens=extraction.output_tokens)

    with conn:  # one transaction: email row + attachments + orders
        if not result.is_shipping_order or not result.orders:
            email_id = db.record_email(conn, email, "not_an_order", classification=result.classification_reason, **usage)
            _save_attachments(conn, email_id, email, extraction.skipped_attachments)
            return ProcessResult("not_an_order", detail=result.classification_reason)

        email_id = db.record_email(conn, email, "order_created", classification=result.classification_reason, **usage)
        _save_attachments(conn, email_id, email, extraction.skipped_attachments)
        numbers = []
        for order in result.orders:
            reasons = review_reasons(order, settings.review_confidence_threshold)
            status = "needs_review" if reasons else "new"
            numbers.append(db.insert_order(conn, email_id, order, status, reasons))

    return ProcessResult("order_created", numbers, result.classification_reason)

"""SQLite storage for processed emails, shipping orders and freight line items."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import EmailMessage, Party, ShippingOrder

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id          TEXT NOT NULL UNIQUE,
    internet_message_id TEXT,
    source              TEXT NOT NULL,
    subject             TEXT,
    sender              TEXT,
    received_at         TEXT,
    status              TEXT NOT NULL,   -- order_created | not_an_order | error
    classification      TEXT,
    error               TEXT,
    model               TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    processed_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id     INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    content_type TEXT,
    size_bytes   INTEGER,
    stored_path  TEXT,
    sent_to_llm  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS shipping_orders (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number          TEXT NOT NULL UNIQUE,
    email_id              INTEGER NOT NULL REFERENCES emails(id),
    status                TEXT NOT NULL,  -- new | needs_review
    customer_name         TEXT,
    customer_reference    TEXT,
    bill_to_json          TEXT,

    shipper_company       TEXT,
    shipper_contact       TEXT,
    shipper_phone         TEXT,
    shipper_email         TEXT,
    shipper_street        TEXT,
    shipper_city          TEXT,
    shipper_state         TEXT,
    shipper_postal_code   TEXT,
    shipper_country       TEXT,
    shipper_reference     TEXT,

    consignee_company     TEXT,
    consignee_contact     TEXT,
    consignee_phone       TEXT,
    consignee_email       TEXT,
    consignee_street      TEXT,
    consignee_city        TEXT,
    consignee_state       TEXT,
    consignee_postal_code TEXT,
    consignee_country     TEXT,
    consignee_reference   TEXT,

    pickup_date           TEXT,
    pickup_time_window    TEXT,
    delivery_date         TEXT,
    delivery_time_window  TEXT,
    equipment_type        TEXT,
    equipment_length_ft   REAL,
    load_type             TEXT,
    total_weight          REAL,
    total_weight_unit     TEXT,
    total_pieces          REAL,
    temperature_min_f     REAL,
    temperature_max_f     REAL,
    hazmat                INTEGER NOT NULL DEFAULT 0,
    accessorials_json     TEXT,
    special_instructions  TEXT,
    rate_amount           REAL,
    rate_currency         TEXT,
    source_of_data        TEXT,
    missing_fields_json   TEXT,
    confidence            REAL,
    review_reasons_json   TEXT,
    extraction_json       TEXT NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       INTEGER NOT NULL REFERENCES shipping_orders(id) ON DELETE CASCADE,
    line_number    INTEGER NOT NULL,
    description    TEXT,
    quantity       REAL,
    packaging_type TEXT,
    weight         REAL,
    weight_unit    TEXT,
    dimensions     TEXT,
    freight_class  TEXT,
    nmfc_code      TEXT,
    hazmat         INTEGER NOT NULL DEFAULT 0,
    hazmat_details TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON shipping_orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_pickup ON shipping_orders(pickup_date);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def email_already_done(conn: sqlite3.Connection, message_id: str) -> bool:
    """True if this email was processed successfully before (errors are retried)."""
    row = conn.execute("SELECT status FROM emails WHERE message_id = ?", (message_id,)).fetchone()
    return row is not None and row["status"] != "error"


def record_email(
    conn: sqlite3.Connection,
    email: EmailMessage,
    status: str,
    classification: str | None = None,
    error: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> int:
    """Insert or update (on retry) the email row and return its id."""
    values = (
        email.internet_message_id,
        email.source,
        email.subject,
        email.sender,
        email.received_at.isoformat() if email.received_at else None,
        status,
        classification,
        error,
        model,
        input_tokens,
        output_tokens,
        _now(),
    )
    existing = conn.execute("SELECT id FROM emails WHERE message_id = ?", (email.message_id,)).fetchone()
    if existing:
        conn.execute(
            """UPDATE emails SET internet_message_id=?, source=?, subject=?, sender=?, received_at=?,
               status=?, classification=?, error=?, model=?, input_tokens=?, output_tokens=?,
               processed_at=? WHERE id=?""",
            (*values, existing["id"]),
        )
        conn.execute("DELETE FROM email_attachments WHERE email_id = ?", (existing["id"],))
        return existing["id"]
    cur = conn.execute(
        """INSERT INTO emails (internet_message_id, source, subject, sender, received_at, status,
           classification, error, model, input_tokens, output_tokens, processed_at, message_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (*values, email.message_id),
    )
    return cur.lastrowid


def record_attachment(
    conn: sqlite3.Connection,
    email_id: int,
    filename: str,
    content_type: str,
    size: int,
    stored_path: Optional[str],
    sent_to_llm: bool,
) -> None:
    conn.execute(
        """INSERT INTO email_attachments (email_id, filename, content_type, size_bytes, stored_path, sent_to_llm)
           VALUES (?,?,?,?,?,?)""",
        (email_id, filename, content_type, size, stored_path, int(sent_to_llm)),
    )


def _next_order_number(conn: sqlite3.Connection) -> str:
    prefix = f"SO-{datetime.now():%Y%m%d}-"
    row = conn.execute(
        "SELECT order_number FROM shipping_orders WHERE order_number LIKE ? ORDER BY order_number DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    seq = int(row["order_number"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{seq:04d}"


def _party_cols(p: Party) -> tuple:
    a = p.address
    return (
        p.company, p.contact_name, p.phone, p.email,
        a.street, a.city, a.state, a.postal_code, a.country,
        p.reference_number,
    )


def insert_order(
    conn: sqlite3.Connection,
    email_id: int,
    order: ShippingOrder,
    status: str,
    review_reasons: list[str],
) -> str:
    order_number = _next_order_number(conn)
    cols = (
        order_number, email_id, status, order.customer_name, order.customer_reference,
        order.bill_to.model_dump_json() if order.bill_to else None,
        *_party_cols(order.shipper),
        *_party_cols(order.consignee),
        order.pickup_date, order.pickup_time_window, order.delivery_date, order.delivery_time_window,
        order.equipment_type.value, order.equipment_length_ft, order.load_type.value,
        order.total_weight, order.total_weight_unit.value if order.total_weight_unit else None,
        order.total_pieces, order.temperature_min_f, order.temperature_max_f, int(order.hazmat),
        json.dumps(order.accessorials), order.special_instructions,
        order.rate_amount, order.rate_currency, order.source_of_data,
        json.dumps(order.missing_fields), order.confidence, json.dumps(review_reasons),
        order.model_dump_json(), _now(),
    )
    placeholders = ",".join("?" * len(cols))
    cur = conn.execute(
        f"""INSERT INTO shipping_orders (
            order_number, email_id, status, customer_name, customer_reference, bill_to_json,
            shipper_company, shipper_contact, shipper_phone, shipper_email, shipper_street,
            shipper_city, shipper_state, shipper_postal_code, shipper_country, shipper_reference,
            consignee_company, consignee_contact, consignee_phone, consignee_email, consignee_street,
            consignee_city, consignee_state, consignee_postal_code, consignee_country, consignee_reference,
            pickup_date, pickup_time_window, delivery_date, delivery_time_window,
            equipment_type, equipment_length_ft, load_type, total_weight, total_weight_unit,
            total_pieces, temperature_min_f, temperature_max_f, hazmat,
            accessorials_json, special_instructions, rate_amount, rate_currency, source_of_data,
            missing_fields_json, confidence, review_reasons_json, extraction_json, created_at
        ) VALUES ({placeholders})""",
        cols,
    )
    order_id = cur.lastrowid
    for i, item in enumerate(order.items, start=1):
        conn.execute(
            """INSERT INTO order_items (order_id, line_number, description, quantity, packaging_type,
               weight, weight_unit, dimensions, freight_class, nmfc_code, hazmat, hazmat_details)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order_id, i, item.description, item.quantity, item.packaging_type, item.weight,
                item.weight_unit.value if item.weight_unit else None, item.dimensions,
                item.freight_class, item.nmfc_code, int(item.hazmat), item.hazmat_details,
            ),
        )
    return order_number


def list_orders(conn: sqlite3.Connection, status: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    sql = """SELECT o.order_number, o.status, o.customer_name, o.customer_reference,
                    o.shipper_city, o.shipper_state, o.consignee_city, o.consignee_state,
                    o.pickup_date, o.equipment_type, o.total_weight, o.total_weight_unit,
                    o.confidence, e.subject
             FROM shipping_orders o JOIN emails e ON e.id = o.email_id"""
    params: tuple = ()
    if status:
        sql += " WHERE o.status = ?"
        params = (status,)
    sql += " ORDER BY o.id DESC LIMIT ?"
    return conn.execute(sql, (*params, limit)).fetchall()


def get_order(conn: sqlite3.Connection, order_number: str) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    order = conn.execute("SELECT * FROM shipping_orders WHERE order_number = ?", (order_number,)).fetchone()
    if not order:
        return None, []
    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ? ORDER BY line_number", (order["id"],)
    ).fetchall()
    return order, items

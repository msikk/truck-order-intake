"""Offline tests: Claude and Microsoft Graph are replaced with fakes."""

import base64
from dataclasses import replace
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from truck_orders import db
from truck_orders.config import Settings
from truck_orders.extractor import OUTPUT_SCHEMA, OrderExtractor, attachment_to_blocks
from truck_orders.mail.local import read_eml
from truck_orders.models import Attachment
from truck_orders.pipeline import process_email

SAMPLES = Path(__file__).parent.parent / "samples"

ORDER = {
    "customer_name": "Acme Foods Inc.",
    "customer_reference": "AF-558210",
    "bill_to": None,
    "shipper": {
        "company": "Acme Foods DC", "contact_name": "Maria Lopez", "phone": "(559) 555-0142", "email": None,
        "address": {"street": "1200 Industrial Pkwy", "city": "Fresno", "state": "CA", "postal_code": "93725", "country": "US"},
        "reference_number": "PU-88123",
    },
    "consignee": {
        "company": "FreshMart Warehouse", "contact_name": None, "phone": None, "email": None,
        "address": {"street": "77 Commerce Dr", "city": "Phoenix", "state": "AZ", "postal_code": "85043", "country": "US"},
        "reference_number": "APPT-55102",
    },
    "pickup_date": "2026-10-22", "pickup_time_window": "06:00-12:00",
    "delivery_date": "2026-10-23", "delivery_time_window": "08:00",
    "equipment_type": "reefer", "equipment_length_ft": 53, "load_type": "FTL",
    "items": [{
        "description": "Fresh produce (lettuce)", "quantity": 22, "packaging_type": "pallets",
        "weight": 38500, "weight_unit": "lbs", "dimensions": None, "freight_class": None,
        "nmfc_code": None, "hazmat": False, "hazmat_details": None,
    }],
    "total_weight": 38500, "total_weight_unit": "lbs", "total_pieces": 22,
    "temperature_min_f": 34, "temperature_max_f": 36, "hazmat": False,
    "accessorials": [], "special_instructions": "Driver must have pulp thermometer.",
    "rate_amount": 2850.0, "rate_currency": "USD", "source_of_data": "AF-558210_rate_con.pdf",
    "missing_fields": [], "confidence": 0.95,
}


class FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            model=kwargs["model"],
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


def fake_extractor(payload, stop_reason="end_turn"):
    messages = FakeMessages(payload, stop_reason)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return OrderExtractor(client=client, model="claude-opus-5-5"), messages


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("truck_orders.pipeline.settings", replace(Settings(), attachment_dir=tmp_path / "att"))
    return db.connect(tmp_path / "orders.db")


def test_schema_is_strict():
    def check(node):
        if isinstance(node, dict):
            assert "$ref" not in node
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)
    check(OUTPUT_SCHEMA)


def test_pdf_email_creates_order(conn):
    extractor, messages = fake_extractor({"is_shipping_order": True, "classification_reason": "Load tender", "orders": [ORDER]})
    email = read_eml(SAMPLES / "pdf_tender.eml")
    res = process_email(conn, extractor, email)

    assert res.status == "order_created" and len(res.order_numbers) == 1
    order, items = db.get_order(conn, res.order_numbers[0])
    assert order["status"] == "new"
    assert order["shipper_city"] == "Fresno" and order["consignee_state"] == "AZ"
    assert order["equipment_type"] == "reefer" and order["temperature_min_f"] == 34
    assert len(items) == 1 and items[0]["quantity"] == 22

    # Request shape: PDF sent as a document block, structured output + fallbacks on.
    call = messages.calls[0]
    blocks = call["messages"][0]["content"]
    assert any(b["type"] == "document" and b["source"]["media_type"] == "application/pdf" for b in blocks)
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in call["betas"]

    att = conn.execute("SELECT * FROM email_attachments").fetchone()
    assert att["filename"] == "AF-558210_rate_con.pdf" and Path(att["stored_path"]).exists()

    # Second run is skipped (dedupe on message id).
    assert process_email(conn, extractor, email).status == "skipped"
    assert len(messages.calls) == 1


def test_jpeg_sent_as_image_and_incomplete_order_flagged(conn):
    partial = json.loads(json.dumps(ORDER))
    partial["pickup_date"] = None
    partial["confidence"] = 0.5
    extractor, messages = fake_extractor({"is_shipping_order": True, "classification_reason": "Load sheet", "orders": [partial]})
    res = process_email(conn, extractor, read_eml(SAMPLES / "jpeg_load_sheet.eml"))

    blocks = messages.calls[0]["messages"][0]["content"]
    assert any(b["type"] == "image" and b["source"]["media_type"] == "image/jpeg" for b in blocks)
    order, _ = db.get_order(conn, res.order_numbers[0])
    assert order["status"] == "needs_review"
    reasons = json.loads(order["review_reasons_json"])
    assert "pickup date missing" in reasons and any("low confidence" in r for r in reasons)


def test_multiple_orders_in_one_email(conn):
    extractor, _ = fake_extractor({"is_shipping_order": True, "classification_reason": "Two loads", "orders": [ORDER, ORDER]})
    res = process_email(conn, extractor, read_eml(SAMPLES / "body_only.eml"))
    assert len(res.order_numbers) == 2 and res.order_numbers[0] != res.order_numbers[1]


def test_non_order_email(conn):
    extractor, _ = fake_extractor({"is_shipping_order": False, "classification_reason": "Payment remittance", "orders": []})
    res = process_email(conn, extractor, read_eml(SAMPLES / "not_an_order.eml"))
    assert res.status == "not_an_order"
    assert conn.execute("SELECT COUNT(*) FROM shipping_orders").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM emails").fetchone()["status"] == "not_an_order"


def test_refusal_recorded_as_error_and_retried(conn):
    extractor, _ = fake_extractor({}, stop_reason="refusal")
    email = read_eml(SAMPLES / "body_only.eml")
    assert process_email(conn, extractor, email).status == "error"
    # Errors are not "done", so a later run retries the same email.
    extractor2, _ = fake_extractor({"is_shipping_order": True, "classification_reason": "x", "orders": [ORDER]})
    assert process_email(conn, extractor2, email).status == "order_created"
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 1


def test_large_and_tiff_images_are_converted():
    big = Image.new("RGB", (9000, 600), "white")
    buf = io.BytesIO(); big.save(buf, format="PNG")
    blocks = attachment_to_blocks(Attachment("scan.png", "image/png", buf.getvalue()))
    img = Image.open(io.BytesIO(base64.b64decode(blocks[1]["source"]["data"])))
    assert max(img.size) <= 2000 and blocks[1]["source"]["media_type"] == "image/jpeg"

    buf = io.BytesIO(); Image.new("RGB", (800, 600)).save(buf, format="TIFF")
    blocks = attachment_to_blocks(Attachment("fax.tif", "application/octet-stream", buf.getvalue()))
    assert blocks[1]["type"] == "image"

    assert attachment_to_blocks(Attachment("sheet.xlsx", "application/vnd.ms-excel", b"x")) == []

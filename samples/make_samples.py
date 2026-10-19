"""Generate sample .eml files (PDF rate confirmation, JPEG load sheet, non-order) for testing.

    python samples/make_samples.py
"""

from email.message import EmailMessage
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent


def make_pdf() -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    lines = [
        "ACME FOODS INC. - LOAD TENDER / RATE CONFIRMATION",
        "Load #: AF-558210        PO: 4410092",
        "",
        "PICKUP: Acme Foods DC, 1200 Industrial Pkwy, Fresno, CA 93725",
        "  Contact: Maria Lopez (559) 555-0142   Pickup #: PU-88123",
        "  Date: 10/22/2026   Window: 06:00 - 12:00",
        "",
        "DELIVER: FreshMart Warehouse, 77 Commerce Dr, Phoenix, AZ 85043",
        "  Appointment: 10/23/2026 08:00   Ref: APPT-55102",
        "",
        "Equipment: 53' Reefer   Temp: 34-36 F continuous",
        "Commodity: Fresh produce (lettuce), 22 pallets, 38,500 lbs",
        "Rate: $2,850.00 USD all-in",
        "Special: Driver must have pulp thermometer. No double brokering.",
    ]
    y = 740
    for line in lines:
        c.drawString(60, y, line)
        y -= 20
    c.save()
    return buf.getvalue()


def make_jpeg() -> bytes:
    img = Image.new("RGB", (1100, 700), "white")
    d = ImageDraw.Draw(img)
    text = [
        "LOAD SHEET - Midwest Steel Supply",
        "Ship from: Midwest Steel, 900 Mill Rd, Gary IN 46402",
        "Ship to: BuildRight Construction, 15 Ridge Ave, Columbus OH 43215",
        "Pickup Mon 10/26  7am-3pm",
        "Flatbed 48ft, tarps required",
        "12 bundles steel rebar - 44,000 lbs",
        "Contact: Dave 219-555-0199",
    ]
    for i, t in enumerate(text):
        d.text((40, 40 + i * 80), t, fill="black", font_size=32)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def write_eml(name: str, subject: str, sender: str, body: str, attachment=None) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "orders@example-trucking.com"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 19 Oct 2026 09:15:00 -0700"
    msg["Message-ID"] = f"<{name}@example.com>"
    msg.set_content(body)
    if attachment:
        data, maintype, subtype, filename = attachment
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    (OUT / f"{name}.eml").write_bytes(bytes(msg))
    print("wrote", OUT / f"{name}.eml")


if __name__ == "__main__":
    write_eml(
        "pdf_tender",
        "Load tender AF-558210 Fresno -> Phoenix",
        "Maria Lopez <maria.lopez@acmefoods.example>",
        "Hi team,\n\nPlease see the attached tender for Thursday. Confirm ASAP.\n\nThanks,\nMaria",
        (make_pdf(), "application", "pdf", "AF-558210_rate_con.pdf"),
    )
    write_eml(
        "jpeg_load_sheet",
        "Need a flatbed Monday",
        "Dave Kowalski <dave@midweststeel.example>",
        "Photo of the load sheet attached. Can you cover this?",
        (make_jpeg(), "image", "jpeg", "load_sheet.jpg"),
    )
    write_eml(
        "body_only",
        "LTL quote + book: 3 pallets Dallas to Houston",
        "Sam Patel <sam@gadgetco.example>",
        "Please book an LTL pickup:\n"
        "From GadgetCo, 500 Elm St, Dallas TX 75201, pickup 10/21 after 1pm, liftgate needed.\n"
        "To Patel Retail, 2200 Westheimer Rd, Houston TX 77098, inside delivery.\n"
        "3 pallets electronics, 48x40x50 in, 1,350 lbs total, class 92.5.\n"
        "Our PO 7781.",
    )
    write_eml(
        "not_an_order",
        "Remittance advice - invoice 10442",
        "AP Dept <ap@somecustomer.example>",
        "Payment of $3,200.00 for invoice 10442 was sent via ACH today.",
    )

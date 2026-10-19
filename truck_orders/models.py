"""Data shapes shared across the app.

`EmailMessage` / `Attachment` are what the mail sources produce.
`ExtractionResult` is the schema Claude must fill in (sent as a JSON schema
via structured outputs, then validated with Pydantic).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class EmailMessage:
    message_id: str  # source-specific unique id (Graph id, or file path for .eml)
    subject: str
    sender: str
    received_at: Optional[datetime]
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)
    internet_message_id: str = ""
    source: str = "exchange"


# ---------------------------------------------------------------------------
# Extraction schema (what Claude returns)
# ---------------------------------------------------------------------------


class EquipmentType(str, Enum):
    dry_van = "dry_van"
    reefer = "reefer"
    flatbed = "flatbed"
    step_deck = "step_deck"
    lowboy = "lowboy"
    conestoga = "conestoga"
    tanker = "tanker"
    box_truck = "box_truck"
    hotshot = "hotshot"
    power_only = "power_only"
    container = "container"
    other = "other"
    unknown = "unknown"


class LoadType(str, Enum):
    ftl = "FTL"
    ltl = "LTL"
    partial = "partial"
    unknown = "unknown"


class WeightUnit(str, Enum):
    lbs = "lbs"
    kg = "kg"


class Address(BaseModel):
    street: Optional[str] = Field(description="Street address lines, comma separated")
    city: Optional[str]
    state: Optional[str] = Field(description="State/province code, e.g. TX or ON")
    postal_code: Optional[str]
    country: Optional[str] = Field(description="ISO country code, e.g. US, CA, MX")


class Party(BaseModel):
    company: Optional[str]
    contact_name: Optional[str]
    phone: Optional[str]
    email: Optional[str]
    address: Address
    reference_number: Optional[str] = Field(
        description="Pickup/PO/appointment number specific to this location, if any"
    )


class FreightItem(BaseModel):
    description: Optional[str]
    quantity: Optional[float] = Field(description="Number of handling units, e.g. 24")
    packaging_type: Optional[str] = Field(description="pallets, skids, crates, boxes, drums, etc.")
    weight: Optional[float] = Field(description="Total weight of this line")
    weight_unit: Optional[WeightUnit]
    dimensions: Optional[str] = Field(description='Per-unit L x W x H as written, e.g. "48x40x60 in"')
    freight_class: Optional[str] = Field(description="NMFC freight class for LTL, e.g. 70")
    nmfc_code: Optional[str]
    hazmat: bool
    hazmat_details: Optional[str] = Field(description="UN number, hazard class, packing group")


class ShippingOrder(BaseModel):
    customer_name: Optional[str] = Field(description="Company requesting / paying for the shipment")
    customer_reference: Optional[str] = Field(
        description="Customer's PO, load, tender, or BOL number for this shipment"
    )
    bill_to: Optional[Party]
    shipper: Party = Field(description="Pickup / origin location")
    consignee: Party = Field(description="Delivery / destination location")
    pickup_date: Optional[str] = Field(description="YYYY-MM-DD; earliest pickup date")
    pickup_time_window: Optional[str] = Field(description='e.g. "08:00-14:00" or "FCFS"')
    delivery_date: Optional[str] = Field(description="YYYY-MM-DD")
    delivery_time_window: Optional[str]
    equipment_type: EquipmentType
    equipment_length_ft: Optional[float] = Field(description="Trailer length, e.g. 53")
    load_type: LoadType
    items: list[FreightItem]
    total_weight: Optional[float]
    total_weight_unit: Optional[WeightUnit]
    total_pieces: Optional[float]
    temperature_min_f: Optional[float] = Field(description="Reefer min temp in Fahrenheit")
    temperature_max_f: Optional[float] = Field(description="Reefer max temp in Fahrenheit")
    hazmat: bool
    accessorials: list[str] = Field(
        description="e.g. liftgate, inside delivery, appointment, residential, tarps, team"
    )
    special_instructions: Optional[str]
    rate_amount: Optional[float] = Field(description="Quoted or offered rate, if stated")
    rate_currency: Optional[str] = Field(description="ISO currency code, e.g. USD")
    source_of_data: str = Field(
        description='Where the order details were found: "email_body", an attachment filename, or several, comma separated'
    )
    missing_fields: list[str] = Field(
        description="Important fields a dispatcher would need that were not in the email"
    )
    confidence: float = Field(
        description="0.0-1.0: how confident you are this order is complete and correctly read"
    )


class ExtractionResult(BaseModel):
    is_shipping_order: bool = Field(
        description="True only if the email asks for one or more truck shipments to be booked/tendered"
    )
    classification_reason: str = Field(description="One sentence explaining the classification")
    orders: list[ShippingOrder] = Field(
        description="One entry per distinct load/shipment requested. Empty if not a shipping order."
    )

"""Command line entry point.

    python -m truck_orders poll              # process unread Exchange mail once
    python -m truck_orders poll --watch 60   # keep polling every 60 seconds
    python -m truck_orders process-eml samples/*.eml
    python -m truck_orders list [--status needs_review]
    python -m truck_orders show SO-20261019-0001
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import db
from .config import settings
from .extractor import OrderExtractor
from .pipeline import process_email


def _print_result(label: str, res) -> None:
    extra = f" -> {', '.join(res.order_numbers)}" if res.order_numbers else ""
    print(f"[{res.status}] {label}{extra}" + (f"  ({res.detail})" if res.detail and res.status != "order_created" else ""))


def cmd_poll(args) -> int:
    from .mail.graph import GraphMailbox

    mailbox = GraphMailbox(settings)
    extractor = OrderExtractor()
    conn = db.connect(settings.db_path)

    while True:
        count = 0
        for email in mailbox.unread_messages(limit=args.limit):
            count += 1
            try:
                res = process_email(conn, extractor, email)
            except Exception:
                logging.exception("Unexpected failure on %r; leaving it unread for retry", email.subject)
                continue
            _print_result(email.subject, res)
            # Errors stay unread in the inbox so they are retried on the next poll.
            if res.status in ("order_created", "not_an_order", "skipped"):
                mailbox.mark_processed(email.message_id)
        if not args.watch:
            print(f"Done. {count} email(s) checked.")
            return 0
        time.sleep(args.watch)


def cmd_process_eml(args) -> int:
    from .mail.local import read_eml

    extractor = OrderExtractor()
    conn = db.connect(settings.db_path)
    for p in args.files:
        res = process_email(conn, extractor, read_eml(Path(p)), force=args.force)
        _print_result(p, res)
    return 0


def cmd_list(args) -> int:
    conn = db.connect(settings.db_path)
    rows = db.list_orders(conn, args.status, args.limit)
    if not rows:
        print("No orders.")
        return 0
    for r in rows:
        lane = f"{r['shipper_city'] or '?'}, {r['shipper_state'] or ''} -> {r['consignee_city'] or '?'}, {r['consignee_state'] or ''}"
        weight = f"{r['total_weight']:g} {r['total_weight_unit'] or ''}" if r["total_weight"] else "-"
        print(
            f"{r['order_number']}  {r['status']:<12} {r['pickup_date'] or 'no date':<10}  "
            f"{lane:<40} {r['equipment_type']:<10} {weight:<12} {r['customer_name'] or ''}"
        )
    return 0


def cmd_show(args) -> int:
    conn = db.connect(settings.db_path)
    order, items = db.get_order(conn, args.order_number)
    if not order:
        print(f"Order {args.order_number} not found", file=sys.stderr)
        return 1
    data = dict(order)
    data["extraction"] = json.loads(data.pop("extraction_json"))
    data["items"] = [dict(i) for i in items]
    print(json.dumps(data, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="truck_orders", description="Create trucking orders from emails using Claude")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("poll", help="Process unread emails from the Exchange mailbox")
    p.add_argument("--limit", type=int, default=25, help="Max emails per poll")
    p.add_argument("--watch", type=int, metavar="SECONDS", help="Keep polling at this interval")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("process-eml", help="Process local .eml files (for testing)")
    p.add_argument("files", nargs="+")
    p.add_argument("--force", action="store_true", help="Reprocess even if already done")
    p.set_defaults(func=cmd_process_eml)

    p = sub.add_parser("list", help="List orders")
    p.add_argument("--status", choices=["new", "needs_review"])
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="Show one order as JSON")
    p.add_argument("order_number")
    p.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return args.func(args)

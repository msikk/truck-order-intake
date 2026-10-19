# Truck Order Intake

Reads emails from a Microsoft Exchange Online mailbox, uses Claude to read the email
text **and** any PDF or image (JPEG/PNG/TIFF) attachments, and creates trucking
shipping orders in a local SQLite database.

```
Exchange mailbox ──(Microsoft Graph)──> email + attachments
        │
        ▼
Claude (claude-opus-5-5) reads body, PDFs and images directly
        │  returns JSON that matches a strict schema
        ▼
SQLite: emails, email_attachments, shipping_orders, order_items
        │
        ▼
Email marked read and moved to "Orders Processed"
```

## What it does

- **Classifies** each email: real shipping request vs. noise (remittances, tracking updates, etc.).
- **Extracts** shipper, consignee, dates and time windows, equipment, freight items,
  weights, temperatures, hazmat, accessorials, rate and reference numbers.
- **Handles several loads in one email** — each becomes its own order.
- **Reads attachments natively**: PDFs go to Claude as documents, images as images.
  Scanned TIFF/BMP faxes are converted; oversized photos are shrunk to fit API limits.
- **Flags orders for review** (`needs_review`) when confidence is low or key fields
  (pickup/delivery location, pickup date, equipment, reefer temp) are missing.
- **Never processes an email twice.** Failed emails stay unread and are retried on the next poll.
- **Keeps an audit trail**: raw extraction JSON, token usage, and a copy of every attachment
  under `data/attachments/<email id>/`.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill it in
```

### 1. Claude API key
Set `ANTHROPIC_API_KEY` in `.env` (from console.anthropic.com).

### 2. Exchange Online access (Microsoft Graph)
Graph is Microsoft's supported API for Exchange Online (EWS is being retired).

1. **Azure portal → Microsoft Entra ID → App registrations → New registration.** Any name, single tenant.
2. Copy the **Application (client) ID** and **Directory (tenant) ID** into `.env`.
3. **Certificates & secrets → New client secret.** Copy the value into `MS_CLIENT_SECRET`.
4. **API permissions → Add → Microsoft Graph → Application permissions → `Mail.ReadWrite`**,
   then **Grant admin consent**.
5. **Strongly recommended:** restrict the app to just the orders mailbox. Without this,
   `Mail.ReadWrite` lets it read every mailbox in the tenant. In Exchange Online PowerShell:
   ```powershell
   New-ApplicationAccessPolicy -AppId <client-id> -PolicyScopeGroupId orders@yourcompany.com `
     -AccessRight RestrictAccess -Description "Order intake app"
   ```
   (or use Exchange "RBAC for Applications").
6. Set `MS_MAILBOX` to the mailbox address (e.g. `orders@yourcompany.com`).

## Usage

```bash
# Try it without Exchange, using the sample emails
python samples/make_samples.py         # needs: pip install -r requirements-dev.txt
python -m truck_orders process-eml samples/*.eml

# Process unread mail in the Exchange mailbox once
python -m truck_orders poll

# Keep running, checking every 60 seconds
python -m truck_orders poll --watch 60

# Look at results
python -m truck_orders list
python -m truck_orders list --status needs_review
python -m truck_orders show SO-20261019-0001
```

The database is a plain SQLite file (`data/orders.db`), so you can also open it with
`sqlite3` or DB Browser for SQLite.

## Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_MODEL` | `claude-opus-5-5` | Model used for extraction |
| `CLAUDE_EFFORT` | `medium` | `low` is cheaper/faster; `high` for messy scans/handwriting |
| `MS_SOURCE_FOLDER` | `inbox` | Folder to read unread mail from |
| `MS_PROCESSED_FOLDER` | `Orders Processed` | Where handled mail is moved (created if missing). Blank = just mark read |
| `REVIEW_CONFIDENCE_THRESHOLD` | `0.75` | Below this, orders are `needs_review` |
| `DB_PATH` / `ATTACHMENT_DIR` | `data/...` | Storage locations |

## Project layout

```
truck_orders/
  cli.py          commands (poll, process-eml, list, show)
  pipeline.py     email -> Claude -> database, review rules
  extractor.py    Claude call, attachment handling, prompt, JSON schema
  models.py       extraction schema (Pydantic) and email types
  db.py           SQLite schema and queries
  mail/graph.py   Exchange Online via Microsoft Graph
  mail/local.py   .eml files for testing
tests/            offline tests (Claude and Graph are faked)
samples/          sample emails generator
```

Run tests with `pytest`.

## Notes

- **Cost**: roughly one Claude request per email. A 1–2 page PDF typically runs a few
  thousand input tokens. Token usage per email is stored in the `emails` table.
- **Security**: email content is treated as untrusted data — the prompt tells Claude to
  extract information and ignore instructions inside emails. Output is constrained to a
  fixed JSON schema, and nothing is booked automatically.
- **Not yet supported**: Excel/Word attachments (logged as skipped) and forwarded emails
  attached as items. Easy to add if you get these often.

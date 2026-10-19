"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Claude
    claude_model: str = _env("CLAUDE_MODEL", "claude-opus-5-5")
    claude_effort: str = _env("CLAUDE_EFFORT", "medium")

    # Microsoft Graph / Exchange Online
    ms_tenant_id: str = _env("MS_TENANT_ID")
    ms_client_id: str = _env("MS_CLIENT_ID")
    ms_client_secret: str = _env("MS_CLIENT_SECRET")
    ms_mailbox: str = _env("MS_MAILBOX")
    ms_source_folder: str = _env("MS_SOURCE_FOLDER", "inbox")
    ms_processed_folder: str = _env("MS_PROCESSED_FOLDER", "Orders Processed")

    # App
    db_path: Path = Path(_env("DB_PATH", "data/orders.db"))
    attachment_dir: Path = Path(_env("ATTACHMENT_DIR", "data/attachments"))
    review_confidence_threshold: float = float(_env("REVIEW_CONFIDENCE_THRESHOLD", "0.75"))

    def require_graph(self) -> None:
        missing = [
            name
            for name, value in {
                "MS_TENANT_ID": self.ms_tenant_id,
                "MS_CLIENT_ID": self.ms_client_id,
                "MS_CLIENT_SECRET": self.ms_client_secret,
                "MS_MAILBOX": self.ms_mailbox,
            }.items()
            if not value
        ]
        if missing:
            raise SystemExit(f"Missing Exchange settings: {', '.join(missing)} (see .env.example)")


settings = Settings()

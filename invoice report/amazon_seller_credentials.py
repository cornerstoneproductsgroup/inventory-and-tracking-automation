"""Load Amazon Seller Central credentials from invoice report or Inventory Submissions .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).resolve().parent
_ENV_FILE = _SCRIPT_DIR / ".env"
_INVENTORY_ENV = _SCRIPT_DIR.parent / "Inventory Submissions" / ".env"


@dataclass(frozen=True)
class AmazonSellerCredentials:
    email: str
    password: str


def env_file_path() -> Path:
    return _ENV_FILE


def _load_env_files() -> None:
    load_dotenv(_ENV_FILE, override=False)
    if _INVENTORY_ENV.is_file():
        load_dotenv(_INVENTORY_ENV, override=False)


def load_amazon_seller_credentials(*, required: bool = True) -> AmazonSellerCredentials | None:
    _load_env_files()
    email = (
        os.environ.get("AMAZON_SELLER_EMAIL")
        or os.environ.get("AMAZON_SELLER_USERNAME")
        or os.environ.get("AMAZON_USERNAME")
        or os.environ.get("AMAZON_EMAIL")
        or ""
    ).strip()
    password = (
        os.environ.get("AMAZON_SELLER_PASSWORD")
        or os.environ.get("AMAZON_PASSWORD")
        or ""
    ).strip()
    if not email or not password:
        if required:
            raise ValueError(
                "Set Amazon login in .env — any of:\n"
                "  AMAZON_USERNAME + AMAZON_PASSWORD\n"
                "  AMAZON_SELLER_EMAIL + AMAZON_SELLER_PASSWORD\n"
                f"Checked: {_ENV_FILE}\n"
                f"         {_INVENTORY_ENV}\n"
                "Or configure AMAZON_CHROME_USER_DATA_DIR / AMAZON_CHROME_CDP_URL to reuse Chrome login."
            )
        return None
    return AmazonSellerCredentials(email=email, password=password)

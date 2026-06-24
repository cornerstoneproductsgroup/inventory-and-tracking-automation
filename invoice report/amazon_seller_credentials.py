"""Load Amazon Seller Central credentials from invoice report .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).resolve().parent
_ENV_FILE = _SCRIPT_DIR / ".env"


@dataclass(frozen=True)
class AmazonSellerCredentials:
    email: str
    password: str


def env_file_path() -> Path:
    return _ENV_FILE


def load_amazon_seller_credentials(*, required: bool = True) -> AmazonSellerCredentials | None:
    load_dotenv(_ENV_FILE)
    email = (os.environ.get("AMAZON_SELLER_EMAIL") or os.environ.get("AMAZON_SELLER_USERNAME") or "").strip()
    password = (os.environ.get("AMAZON_SELLER_PASSWORD") or "").strip()
    if not email or not password:
        if required:
            raise ValueError(
                "Set AMAZON_SELLER_EMAIL and AMAZON_SELLER_PASSWORD in "
                f"{_ENV_FILE} (copy from .env.example), or configure "
                "AMAZON_CHROME_USER_DATA_DIR / AMAZON_CHROME_CDP_URL to reuse Chrome login."
            )
        return None
    return AmazonSellerCredentials(email=email, password=password)

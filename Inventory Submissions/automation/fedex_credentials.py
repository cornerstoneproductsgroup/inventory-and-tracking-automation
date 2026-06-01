"""FedEx login credentials from Inventory Submissions/.env or fedex_batch.json."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_INVENTORY_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _INVENTORY_ROOT / ".env"


def _load_env() -> None:
    load_dotenv(_ENV_FILE, override=False)


def _expand_env(value: str) -> str:
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def repl(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    return pattern.sub(repl, value or "")


@dataclass(frozen=True)
class FedexCredentials:
    username: str
    password: str


def load_fedex_credentials(cfg: dict[str, Any] | None = None) -> FedexCredentials:
    """
    Resolve FedEx login from (highest priority last):
      - FEDEX_USERNAME / FEDEX_PASSWORD in environment (after .env load)
      - fedex.username / fedex.password in fedex_batch.json (supports ${FEDEX_USERNAME})
    """
    _load_env()

    username = (os.environ.get("FEDEX_USERNAME") or "").strip()
    password = (os.environ.get("FEDEX_PASSWORD") or "").strip()

    if cfg:
        fedex = cfg.get("fedex") if isinstance(cfg.get("fedex"), dict) else {}
        cfg_user = _expand_env(str(fedex.get("username") or "")).strip()
        cfg_pass = _expand_env(str(fedex.get("password") or "")).strip()
        if cfg_user:
            username = cfg_user
        if cfg_pass:
            password = cfg_pass

    if not username:
        raise ValueError(
            f"Missing FedEx username. Add to {_ENV_FILE}:\n"
            "  FEDEX_USERNAME=your-fedex-login@example.com\n"
            "Or set fedex.username in fedex_batch.json."
        )
    if not password:
        raise ValueError(
            f"Missing FedEx password. Add to {_ENV_FILE}:\n"
            "  FEDEX_PASSWORD=your-fedex-password\n"
            "Or set fedex.password in fedex_batch.json."
        )

    return FedexCredentials(username=username, password=password)


def env_file_path() -> Path:
    return _ENV_FILE

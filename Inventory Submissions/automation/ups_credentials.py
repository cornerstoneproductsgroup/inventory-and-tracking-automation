"""UPS.com login credentials from Inventory Submissions/.env or ups_batch.json."""

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
class UpsCredentials:
    username: str
    password: str


def load_ups_credentials(cfg: dict[str, Any] | None = None) -> UpsCredentials:
    _load_env()

    username = (os.environ.get("UPS_USERNAME") or "").strip()
    password = (os.environ.get("UPS_PASSWORD") or "").strip()

    if cfg:
        ups = cfg.get("ups") if isinstance(cfg.get("ups"), dict) else {}
        cfg_user = _expand_env(str(ups.get("username") or "")).strip()
        cfg_pass = _expand_env(str(ups.get("password") or "")).strip()
        if cfg_user:
            username = cfg_user
        if cfg_pass:
            password = cfg_pass

    if not username:
        raise ValueError(
            f"Missing UPS username. Add to {_ENV_FILE}:\n"
            "  UPS_USERNAME=your-ups-login\n"
            "Or set ups.username in ups_batch.json."
        )
    if not password:
        raise ValueError(
            f"Missing UPS password. Add to {_ENV_FILE}:\n"
            "  UPS_PASSWORD=your-ups-password\n"
            "Or set ups.password in ups_batch.json."
        )

    return UpsCredentials(username=username, password=password)


def env_file_path() -> Path:
    return _ENV_FILE

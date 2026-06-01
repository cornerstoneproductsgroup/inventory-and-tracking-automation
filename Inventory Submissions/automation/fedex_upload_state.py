"""Track Lowe's Output.csv files already uploaded to FedEx (avoid re-using old batches)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_STATE_PATH = Path(__file__).resolve().parent.parent / "fedex_upload_state.json"


def _load_raw() -> dict:
    if not _STATE_PATH.is_file():
        return {"used_files": {}}
    try:
        with _STATE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"used_files": {}}
        used = data.get("used_files")
        if not isinstance(used, dict):
            data["used_files"] = {}
        return data
    except Exception:
        return {"used_files": {}}


def was_file_used(filename: str) -> bool:
    name = (filename or "").strip()
    if not name:
        return False
    return name in _load_raw().get("used_files", {})


def mark_file_used(filename: str, *, note: str = "") -> None:
    name = (filename or "").strip()
    if not name:
        return
    data = _load_raw()
    used = data.setdefault("used_files", {})
    used[name] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "note": note,
    }
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def last_used_note(filename: str) -> str | None:
    entry = _load_raw().get("used_files", {}).get(filename)
    if isinstance(entry, dict):
        return str(entry.get("at") or "")
    return None

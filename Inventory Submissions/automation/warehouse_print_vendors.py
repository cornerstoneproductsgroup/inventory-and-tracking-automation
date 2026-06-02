"""Vendors whose FedEx labels print on the warehouse Zebra (same list as Order Splitter)."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

from automation.worldship_label_config import VENDOR_MAP_DIR

_INVENTORY_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_WAREHOUSE_VENDORS_JSON = _INVENTORY_ROOT / "warehouse_vendors.json"
SHARE_WAREHOUSE_VENDORS_JSON = VENDOR_MAP_DIR / "warehouse_vendors.json"

ORDER_SPLITTER_V2_DIR = Path(
    (os.environ.get("ORDER_SPLITTER_V2_DIR") or r"C:\OrderSplitter\Order-Splitter-v2").strip()
)
ORDER_SPLITTER_WATCHER_PY = ORDER_SPLITTER_V2_DIR / "watcher.py"

_cache: frozenset[str] | None = None


def _log(msg: str) -> None:
    print(f"[fedex/warehouse] {msg}", flush=True)


def order_splitter_watcher_path() -> Path:
    override = (os.environ.get("ORDER_SPLITTER_WATCHER_PY") or "").strip()
    if override:
        return Path(override)
    return ORDER_SPLITTER_WATCHER_PY


def bundled_warehouse_vendors_path() -> Path:
    override = (os.environ.get("FEDEX_WAREHOUSE_VENDORS_FILE") or "").strip()
    if override:
        return Path(override)
    return BUNDLED_WAREHOUSE_VENDORS_JSON


def _parse_warehouse_vendors_from_watcher(path: Path) -> frozenset[str]:
    """Read WAREHOUSE_VENDORS = [...] from Order Splitter watcher.py (no import)."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "WAREHOUSE_VENDORS":
                value = ast.literal_eval(node.value)
                if not isinstance(value, list):
                    raise ValueError(f"WAREHOUSE_VENDORS in {path} is not a list.")
                names = [str(v).strip() for v in value if str(v).strip()]
                return frozenset(names)
    raise ValueError(f"WAREHOUSE_VENDORS assignment not found in {path}")


def _parse_warehouse_vendors_from_json(path: Path) -> frozenset[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        names = data
    elif isinstance(data, dict):
        raw = data.get("vendors") or data.get("WAREHOUSE_VENDORS")
        if not isinstance(raw, list):
            raise ValueError(f"{path.name} must contain a 'vendors' array.")
        names = raw
    else:
        raise ValueError(f"Unsupported JSON in {path.name}")
    return frozenset(str(v).strip() for v in names if str(v).strip())


def _load_from_json_candidates() -> tuple[frozenset[str], Path] | None:
    seen: set[Path] = set()
    for path in (
        bundled_warehouse_vendors_path(),
        SHARE_WAREHOUSE_VENDORS_JSON,
    ):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if not path.is_file():
            continue
        try:
            vendors = _parse_warehouse_vendors_from_json(path)
            if vendors:
                return vendors, path
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            _log(f"WARN: Could not read warehouse vendors from {path}: {exc}")
    return None


def load_warehouse_print_vendors(*, reload: bool = False) -> frozenset[str]:
    """
    Warehouse-print vendor names (FedEx → Zebra, not saved to share).

    Load order:
    1. Order Splitter ``watcher.py`` when installed (dev PC).
    2. ``warehouse_vendors.json`` in Inventory Submissions (WorldShip / no Order Splitter).
    3. Same filename on the vendor-maps share (optional shared copy).
    """
    global _cache
    if _cache is not None and not reload:
        return _cache

    watcher = order_splitter_watcher_path()
    if watcher.is_file():
        try:
            vendors = _parse_warehouse_vendors_from_watcher(watcher)
            _cache = vendors
            _log(
                f"Loaded {len(vendors)} warehouse vendor(s) from Order Splitter "
                f"({watcher.parent.name}/watcher.py)"
            )
            return _cache
        except (SyntaxError, ValueError) as exc:
            _log(f"WARN: Could not read WAREHOUSE_VENDORS from {watcher}: {exc}")

    json_result = _load_from_json_candidates()
    if json_result:
        vendors, path = json_result
        _cache = vendors
        _log(f"Loaded {len(vendors)} warehouse vendor(s) from {path}")
        return _cache

    _log(
        "WARN: No warehouse vendor list found. "
        f"Expected Order Splitter at {watcher} or {bundled_warehouse_vendors_path()}"
    )
    _cache = frozenset()
    return _cache


def is_warehouse_print_vendor(vendor_folder: str, *, retailer_key: str = "lowes") -> bool:
    del retailer_key
    name = (vendor_folder or "").strip()
    if not name:
        return False
    vendors = load_warehouse_print_vendors()
    if not vendors:
        return False
    key = name.casefold()
    return any(v.casefold() == key for v in vendors)

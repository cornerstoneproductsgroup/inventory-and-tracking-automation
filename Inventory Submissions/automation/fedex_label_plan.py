"""Build per-label save paths from Lowe's CSV rows and vendor map (grouped by vendor)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from automation.fedex_batch_config import LOWES_LABELS_ROOT, label_filename, warehouse_label_queue_path
from automation.fedex_lowes_csv import LowesOrderRow
from automation.warehouse_print_vendors import is_warehouse_print_vendor
from automation.fedex_reference import vendor_for_sku


def _log(msg: str) -> None:
    print(f"[fedex/plan] {msg}", flush=True)


@dataclass(frozen=True)
class FedexLabelTarget:
    sequence: int
    sku: str
    po: str
    reference: str
    vendor_folder: str
    label_path: Path


@dataclass(frozen=True)
class VendorLabelGroup:
    """Shipments saved under the same vendor folder (printed/saved together)."""

    vendor_folder: str
    save_dir: Path
    targets: tuple[FedexLabelTarget, ...]


def build_label_targets(
    orders: list[LowesOrderRow],
    *,
    labels_root: Path | None = None,
) -> list[FedexLabelTarget]:
    root = labels_root or LOWES_LABELS_ROOT
    targets: list[FedexLabelTarget] = []
    for seq, order in enumerate(orders, start=1):
        try:
            vendor = vendor_for_sku(order.sku)
        except (KeyError, ValueError, FileNotFoundError) as exc:
            _log(f"WARN: line {order.line_number} SKU {order.sku!r}: {exc}; using Unknown folder.")
            vendor = "Unknown"
        save_dir = root / vendor
        save_dir.mkdir(parents=True, exist_ok=True)
        dest = save_dir / label_filename(order.po, order.sku)
        targets.append(
            FedexLabelTarget(
                sequence=seq,
                sku=order.sku,
                po=order.po,
                reference=order.reference,
                vendor_folder=vendor,
                label_path=dest,
            )
        )
    return targets


def group_targets_by_vendor(targets: list[FedexLabelTarget]) -> list[VendorLabelGroup]:
    buckets: dict[str, list[FedexLabelTarget]] = defaultdict(list)
    dirs: dict[str, Path] = {}
    for t in targets:
        buckets[t.vendor_folder].append(t)
        dirs[t.vendor_folder] = t.label_path.parent
    groups: list[VendorLabelGroup] = []
    for vendor in sorted(buckets.keys()):
        items = tuple(sorted(buckets[vendor], key=lambda x: x.sequence))
        groups.append(
            VendorLabelGroup(
                vendor_folder=vendor,
                save_dir=dirs[vendor],
                targets=items,
            )
        )
    return groups


def print_label_plan(groups: list[VendorLabelGroup]) -> None:
    total = sum(len(g.targets) for g in groups)
    _log(f"Label plan: {total} shipment(s) in {len(groups)} vendor group(s)")
    for group in groups:
        skus = ", ".join(t.sku for t in group.targets)
        if is_warehouse_print_vendor(group.vendor_folder):
            dest = str(warehouse_label_queue_path(group.vendor_folder))
        else:
            dest = str(group.save_dir)
        _log(f"  [{group.vendor_folder}] {len(group.targets)} label(s) → {dest}")
        _log(f"      SKUs: {skus}")


def match_reference_to_target(reference: str, targets: list[FedexLabelTarget]) -> FedexLabelTarget | None:
    """Match a FedEx shipment row reference text back to a planned label path."""
    ref = (reference or "").strip()
    if not ref:
        return None
    ref_lower = ref.lower()
    ref_digits = "".join(ch for ch in ref if ch.isdigit())

    for t in targets:
        if ref == t.reference or ref == t.po or ref == t.sku:
            return t
        if t.po and t.po in ref:
            return t
        if t.sku.lower() in ref_lower:
            return t
        po_digits = "".join(ch for ch in t.po if ch.isdigit())
        if po_digits and len(po_digits) >= 5 and po_digits in ref_digits:
            return t
    return None

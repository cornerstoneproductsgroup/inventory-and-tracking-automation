"""Partition CornerstoneMaster rows: all SAVE rows first, then all PRINT rows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from automation.warehouse_print_vendors import is_warehouse_print_vendor

if TYPE_CHECKING:
    from automation.worldship_cornerstone_master import CornerstoneOrderRow
    from automation.worldship_vendor_map import VendorMapRegistry


def _log(msg: str) -> None:
    print(f"[worldship] {msg}", flush=True)


@dataclass(frozen=True)
class SaveLabelItem:
    order: "CornerstoneOrderRow"
    dest: Path
    index: int  # 1-based position in save phase (matches Nth Save dialog)


@dataclass(frozen=True)
class WorldshipLabelWorkPlan:
    save_items: tuple[SaveLabelItem, ...]
    print_orders: tuple["CornerstoneOrderRow", ...]


def partition_worldship_label_rows(
    orders: list["CornerstoneOrderRow"],
    vendor_maps: "VendorMapRegistry",
    *,
    build_destination,
) -> WorldshipLabelWorkPlan:
    """
    Require CSV row order: every SAVE row, then every warehouse PRINT row.

    WorldShip shows Save dialogs in batch row order — mixed rows caused saves to
    drift one label behind. Saves-first keeps dialog index aligned with save_items.
    """
    save_orders: list[CornerstoneOrderRow] = []
    print_orders: list[CornerstoneOrderRow] = []
    in_print_section = False

    for order in orders:
        vendor = vendor_maps.lookup(order.sku, order.retailer_key)
        if is_warehouse_print_vendor(vendor):
            in_print_section = True
            print_orders.append(order)
            continue
        if in_print_section:
            raise ValueError(
                f"CornerstoneMaster row {order.row_number} is a SAVE row but appears "
                f"after warehouse-print rows. Put all rows that save to the share at the "
                f"top of the file, then all warehouse-print rows (SKU {order.sku!r}, "
                f"PO {order.po!r})."
            )
        save_orders.append(order)

    save_items: list[SaveLabelItem] = []
    for i, order in enumerate(save_orders, start=1):
        dest = build_destination(order, vendor_maps)
        save_items.append(SaveLabelItem(order=order, dest=dest, index=i))

    return WorldshipLabelWorkPlan(
        save_items=tuple(save_items),
        print_orders=tuple(print_orders),
    )


def log_worldship_label_work_plan(
    plan: WorldshipLabelWorkPlan,
    vendor_maps: "VendorMapRegistry",
) -> None:
    from automation.worldship_vendor_map import VendorMapRegistry

    if not isinstance(vendor_maps, VendorMapRegistry):
        raise TypeError("vendor_maps must be VendorMapRegistry")

    n_save = len(plan.save_items)
    n_print = len(plan.print_orders)
    _log(
        f"Label work plan: {n_save} SAVE to share (phase 1), "
        f"{n_print} warehouse PRINT (phase 2), "
        f"{n_save + n_print} total row(s)."
    )
    if n_save:
        _log("Phase 1 — SAVE rows (must match WorldShip Save dialog order):")
        for item in plan.save_items:
            vendor = vendor_maps.lookup(item.order.sku, item.order.retailer_key)
            _log(
                f"  save {item.index}/{n_save}: row {item.order.row_number} — "
                f"{vendor!r} → {item.dest.parent.name}\\{item.dest.name}"
            )
    if n_print:
        _log("Phase 2 — warehouse PRINT rows (no Save dialog):")
        for order in plan.print_orders:
            vendor = vendor_maps.lookup(order.sku, order.retailer_key)
            _log(
                f"  print: row {order.row_number} — {vendor!r} "
                f"(SKU {order.sku!r}, PO {order.po!r})"
            )

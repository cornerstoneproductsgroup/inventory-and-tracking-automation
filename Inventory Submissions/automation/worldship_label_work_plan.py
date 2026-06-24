"""Build WorldShip label steps in CornerstoneMaster row order (save / print)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from automation.worldship_cornerstone_master import is_cornerstone_warehouse_print_row
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
    index: int  # 1-based among SAVE rows


@dataclass(frozen=True)
class LabelWorkStep:
    order: "CornerstoneOrderRow"
    action: Literal["save", "print"]
    dest: Path | None
    save_index: int | None  # 1-based when action == "save"
    step_index: int  # 1-based in full batch row order


@dataclass(frozen=True)
class WorldshipLabelWorkPlan:
    steps: tuple[LabelWorkStep, ...]

    @property
    def save_items(self) -> tuple[SaveLabelItem, ...]:
        return tuple(
            SaveLabelItem(order=s.order, dest=s.dest, index=s.save_index)
            for s in self.steps
            if s.action == "save" and s.dest is not None and s.save_index is not None
        )

    @property
    def print_orders(self) -> tuple["CornerstoneOrderRow", ...]:
        return tuple(s.order for s in self.steps if s.action == "print")


def partition_worldship_label_rows(
    orders: list["CornerstoneOrderRow"],
    vendor_maps: "VendorMapRegistry",
    *,
    build_destination,
) -> WorldshipLabelWorkPlan:
    """
    Build label steps in CSV row order (matches WorldShip batch processing order).

    SAVE rows wait for Save Print Output dialogs; PRINT rows wait for WorldShip to
    print and advance without saving. Mixed order is allowed.
    """
    steps: list[LabelWorkStep] = []
    save_n = 0
    saw_print = False

    for order in orders:
        vendor = vendor_maps.lookup(order.sku, order.retailer_key)
        label_action = is_cornerstone_warehouse_print_row(order.label_pr)
        if label_action is None:
            warehouse_print = is_warehouse_print_vendor(vendor)
        else:
            warehouse_print = label_action
            if label_action and not is_warehouse_print_vendor(vendor):
                _log(
                    f"WARN: row {order.row_number} LABEL_PR={order.label_pr!r} is print "
                    f"but vendor {vendor!r} is not in the warehouse vendor list."
                )
            if not label_action and is_warehouse_print_vendor(vendor):
                _log(
                    f"WARN: row {order.row_number} LABEL_PR={order.label_pr!r} is save "
                    f"but SKU maps to warehouse vendor {vendor!r} — using LABEL_PR."
                )

        if warehouse_print:
            saw_print = True
            dest = build_destination(order, vendor_maps)
            steps.append(
                LabelWorkStep(
                    order=order,
                    action="print",
                    dest=dest,
                    save_index=None,
                    step_index=len(steps) + 1,
                )
            )
            continue

        if saw_print:
            _log(
                f"NOTE: row {order.row_number} is SAVE after earlier PRINT row(s) — "
                f"will save when the next Save dialog appears (PO {order.po!r})."
            )

        save_n += 1
        dest = build_destination(order, vendor_maps)
        steps.append(
            LabelWorkStep(
                order=order,
                action="save",
                dest=dest,
                save_index=save_n,
                step_index=len(steps) + 1,
            )
        )

    return WorldshipLabelWorkPlan(steps=tuple(steps))


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
        f"Label work plan: {n_save} SAVE to share, "
        f"{n_print} warehouse PRINT, "
        f"{len(plan.steps)} step(s) in CSV row order."
    )
    for step in plan.steps:
        vendor = vendor_maps.lookup(step.order.sku, step.order.retailer_key)
        if step.action == "save":
            assert step.dest is not None and step.save_index is not None
            _log(
                f"  step {step.step_index}: SAVE {step.save_index}/{n_save} — "
                f"row {step.order.row_number}, {vendor!r} "
                f"(LABEL_PR={step.order.label_pr!r}) → "
                f"{step.dest.parent.name}\\{step.dest.name}"
            )
        else:
            _log(
                f"  step {step.step_index}: PRINT — row {step.order.row_number}, "
                f"{vendor!r} (LABEL_PR={step.order.label_pr!r}, "
                f"SKU {step.order.sku!r}, PO {step.order.po!r})"
            )

"""Parse FedEx REFERENCE column (PO + SKU) and map SKU → vendor folder."""

from __future__ import annotations

import re
from dataclasses import dataclass

from automation.worldship_vendor_map import VendorMapRegistry


def parse_reference(reference: str) -> tuple[str, str]:
    """
    FedEx reference format: '<PO> <SKU>' e.g. '409808835 Medium 25' or '409760327 EZD17'.
    """
    text = (reference or "").strip()
    if not text:
        return "", ""
    m = re.match(r"^(\d{5,})\s+(.+)$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    parts = text.split(None, 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return text, text


@dataclass
class ReferenceOrder:
    reference: str
    po: str
    sku: str
    vendor_folder: str


_registry: VendorMapRegistry | None = None


def _registry_instance() -> VendorMapRegistry:
    global _registry
    if _registry is None:
        _registry = VendorMapRegistry()
    return _registry


def vendor_for_sku(sku: str) -> str:
    try:
        return _registry_instance().lookup(sku, "lowes")
    except Exception:
        return (sku or "Unknown").strip()


def reference_to_order(reference: str) -> ReferenceOrder:
    po, sku = parse_reference(reference)
    vendor = vendor_for_sku(sku)
    return ReferenceOrder(
        reference=(reference or "").strip(),
        po=po,
        sku=sku,
        vendor_folder=vendor,
    )


def group_consecutive_by_vendor(orders: list[ReferenceOrder]) -> list[list[ReferenceOrder]]:
    """Group consecutive rows with the same vendor (FedEx list order)."""
    groups: list[list[ReferenceOrder]] = []
    current_vendor: str | None = None
    bucket: list[ReferenceOrder] = []
    for order in orders:
        if current_vendor is None or order.vendor_folder != current_vendor:
            if bucket:
                groups.append(bucket)
            bucket = [order]
            current_vendor = order.vendor_folder
        else:
            bucket.append(order)
    if bucket:
        groups.append(bucket)
    return groups

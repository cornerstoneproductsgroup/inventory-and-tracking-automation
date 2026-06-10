"""Parse FedEx REFERENCE column (PO + SKU) and map SKU → vendor folder."""

from __future__ import annotations

import re
from dataclasses import dataclass

from automation.worldship_vendor_map import (
    VendorMapRegistry,
    load_vendor_map,
    lookup_vendor_folder_resilient,
)


def _log(msg: str) -> None:
    print(f"[fedex/ref] {msg}", flush=True)


def parse_reference(reference: str) -> tuple[str, str]:
    """
    FedEx reference format: '<PO> <SKU>' e.g. '409808835 Medium 25' or '409760327 EZD17'.
    Multiple SKUs may appear after the PO: '409808835 UPROOTPRO+XTRA UPROOTPRO+YSM'.
    """
    text = (reference or "").strip()
    if not text:
        return "", ""
    m = re.match(r"^(\d{5,})\s*(.+)$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    parts = text.split(None, 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return text, text


def sku_candidates_from_reference(reference: str) -> list[str]:
    """SKU strings to try when resolving vendor (full remainder, then each token)."""
    _po, remainder = parse_reference(reference)
    if not remainder:
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in (remainder, *remainder.split()):
        key = raw.strip()
        norm = key.upper()
        if key and norm not in seen:
            seen.add(norm)
            candidates.append(key)
    return candidates


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


def resolve_vendor_for_reference(
    reference: str,
    *,
    retailer_key: str = "lowes",
) -> tuple[str, str]:
    """
    Map a FedEx REFERENCE cell to a vendor folder using vendor_map_lowes.xlsx.

    Returns (vendor_folder, matched_sku). Never returns the raw SKU as the folder name.
    """
    po, primary_sku = parse_reference(reference)
    reg = _registry_instance()
    if retailer_key not in reg._cache:
        reg._cache[retailer_key] = load_vendor_map(retailer_key=retailer_key)
    mapping = reg._cache[retailer_key]

    tried: list[str] = []
    for candidate in sku_candidates_from_reference(reference):
        tried.append(candidate)
        try:
            vendor = lookup_vendor_folder_resilient(candidate, mapping)
            if candidate != primary_sku:
                _log(
                    f"Vendor {vendor!r} for PO {po!r} via SKU token {candidate!r} "
                    f"(reference {reference!r})"
                )
            return vendor, candidate
        except (KeyError, ValueError):
            continue

    raise KeyError(
        f"No vendor mapping for reference {reference!r} (PO {po!r}). "
        f"Tried SKU(s): {', '.join(tried) or primary_sku!r}. "
        "Check vendor_map_lowes.xlsx on the Cornerstone share."
    )


def vendor_for_sku(sku: str, *, retailer_key: str = "lowes") -> str:
    """Resolve one SKU string to a vendor folder (used by label plan)."""
    reg = _registry_instance()
    if retailer_key not in reg._cache:
        reg._cache[retailer_key] = load_vendor_map(retailer_key=retailer_key)
    return lookup_vendor_folder_resilient(sku, reg._cache[retailer_key])


def reference_to_order(reference: str) -> ReferenceOrder:
    po, sku = parse_reference(reference)
    try:
        vendor, matched_sku = resolve_vendor_for_reference(reference)
    except KeyError as exc:
        _log(f"WARN: {exc}")
        vendor = "Unknown"
        matched_sku = sku
    return ReferenceOrder(
        reference=(reference or "").strip(),
        po=po,
        sku=matched_sku,
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


def group_by_vendor(orders: list[ReferenceOrder]) -> list[list[ReferenceOrder]]:
    """Group all pending orders by vendor (order of first appearance in the list)."""
    buckets: dict[str, list[ReferenceOrder]] = {}
    order: list[str] = []
    for item in orders:
        vendor = item.vendor_folder
        if vendor not in buckets:
            buckets[vendor] = []
            order.append(vendor)
        buckets[vendor].append(item)
    return [buckets[v] for v in order]

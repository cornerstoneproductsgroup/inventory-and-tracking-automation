"""Resize + merge WorldShip label PDFs into z- Daily Vendor Orders (per vendor)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from automation.worldship_label_config import (
    DFC_LEGACY_VENDOR_LABEL_ROOT,
    LABEL_ROOTS,
    daily_vendor_label_pdf_path,
    label_crop_x_pts,
    label_crop_y_from_top_pts,
    label_height_pts,
    label_postprocess_retailer_keys,
    label_width_pts,
)

if TYPE_CHECKING:
    from automation.worldship_label_work_plan import WorldshipLabelWorkPlan
    from automation.worldship_vendor_map import VendorMapRegistry


def _log(msg: str) -> None:
    print(f"[worldship/labels] {msg}", flush=True)


def _pdf_modified_on(path: Path, *, on_date: date) -> bool:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return False
    return mtime == on_date


def _page_is_label_size(page) -> bool:
    try:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
    except Exception:
        return False
    return w <= label_width_pts() * 1.15 and h <= label_height_pts() * 1.15


def _resize_label_page(page):
    """Crop a letter-size WorldShip page to 4x6 and normalize dimensions."""
    from pypdf import PageObject

    target_w = label_width_pts()
    target_h = label_height_pts()

    if _page_is_label_size(page):
        page.scale_to(target_w, target_h)
        return page

    try:
        left = float(page.mediabox.left) + label_crop_x_pts()
        top = float(page.mediabox.top) - label_crop_y_from_top_pts()
        right = left + target_w
        bottom = top - target_h
        cropped = page.crop((left, bottom, right, top))
        cropped.scale_to(target_w, target_h)
        return cropped
    except Exception as exc:
        _log(f"WARN: label crop failed ({exc}) — scaling full page.")
        clone = PageObject.create_blank_page(width=target_w, height=target_h)
        try:
            clone.merge_page(page)
            clone.scale_to(target_w, target_h)
            return clone
        except Exception:
            page.scale_to(target_w, target_h)
            return page


def _write_resized_merged_pdf(sources: list[Path], dest: Path) -> int:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    pages = 0
    for src in sources:
        reader = PdfReader(str(src))
        for page in reader.pages:
            writer.add_page(_resize_label_page(page))
            pages += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        writer.write(fh)
    return pages


def _vendor_scan_dirs(retailer_key: str, vendor_folder: str) -> list[Path]:
    dirs: list[Path] = []
    root = LABEL_ROOTS.get(retailer_key)
    if root is not None:
        dirs.append(root / vendor_folder)
    if retailer_key == "dfc":
        legacy = DFC_LEGACY_VENDOR_LABEL_ROOT / vendor_folder
        if legacy not in dirs:
            dirs.append(legacy)
    return dirs


def _collect_vendor_pdfs_for_date(
    retailer_key: str,
    vendor_folder: str,
    *,
    on_date: date,
    preferred_order: list[Path] | None = None,
) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []

    if preferred_order:
        for path in preferred_order:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved.is_file() and _pdf_modified_on(resolved, on_date=on_date):
                seen.add(resolved)
                ordered.append(resolved)

    for folder in _vendor_scan_dirs(retailer_key, vendor_folder):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.pdf"), key=lambda p: p.name.casefold()):
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen:
                continue
            if not _pdf_modified_on(resolved, on_date=on_date):
                continue
            seen.add(resolved)
            ordered.append(resolved)
    return ordered


def _saved_paths_from_plan(
    plan: "WorldshipLabelWorkPlan",
    vendor_maps: "VendorMapRegistry",
) -> dict[tuple[str, str], list[Path]]:
    groups: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for step in plan.steps:
        if step.action != "save" or step.dest is None:
            continue
        vendor = vendor_maps.lookup(step.order.sku, step.order.retailer_key)
        key = (step.order.retailer_key, vendor)
        groups[key].append(step.dest)
    return groups


def postprocess_worldship_labels(
    plan: "WorldshipLabelWorkPlan",
    vendor_maps: "VendorMapRegistry",
    *,
    order_date: date | None = None,
) -> int:
    """
    For configured retailers (default: dfc), merge today's per-PO PDFs into one
    resized label file per vendor under z- Daily Vendor Orders.
    """
    retailers = label_postprocess_retailer_keys()
    if not retailers:
        return 0

    on_date = order_date or date.today()
    saved_groups = _saved_paths_from_plan(plan, vendor_maps)
    vendors_seen: set[tuple[str, str]] = set(saved_groups)
    for retailer_key in retailers:
        roots: list[Path] = []
        root = LABEL_ROOTS.get(retailer_key)
        if root is not None:
            roots.append(root)
        if retailer_key == "dfc":
            roots.append(DFC_LEGACY_VENDOR_LABEL_ROOT)
        for base in roots:
            try:
                children = list(base.iterdir())
            except OSError:
                continue
            for entry in children:
                if entry.is_dir():
                    vendors_seen.add((retailer_key, entry.name))

    merged_files = 0
    for retailer_key, vendor_folder in sorted(vendors_seen, key=lambda t: (t[0], t[1])):
        if retailer_key not in retailers:
            continue
        preferred = saved_groups.get((retailer_key, vendor_folder), [])
        sources = _collect_vendor_pdfs_for_date(
            retailer_key,
            vendor_folder,
            on_date=on_date,
            preferred_order=preferred,
        )
        if not sources:
            continue

        dest = daily_vendor_label_pdf_path(
            retailer_key, vendor_folder, order_date=on_date
        )
        _log(
            f"Merging {len(sources)} {retailer_key} label PDF(s) for {vendor_folder!r} "
            f"→ {dest.name}"
        )
        for src in sources:
            _log(f"  + {src.name}")
        try:
            pages = _write_resized_merged_pdf(sources, dest)
        except Exception as exc:
            _log(f"ERROR: could not merge labels for {vendor_folder!r}: {exc}")
            continue
        size = dest.stat().st_size if dest.is_file() else 0
        _log(f"Wrote {dest} ({pages} page(s), {size:,} bytes)")
        merged_files += 1

    if merged_files:
        _log(f"Daily vendor label merge complete — {merged_files} vendor file(s).")
    else:
        _log("No label PDFs found for post-process merge today.")
    return merged_files

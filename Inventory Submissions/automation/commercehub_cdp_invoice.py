"""Run CommerceHub retail invoice export on an already-open browser (CDP attach)."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from automation.async_bridge import run_async


def _invoice_report_dir() -> Path:
    root = Path(__file__).resolve().parent.parent.parent
    for name in ("invoice report", "CommerceHub Invoice Report (Depot and Lowe's)"):
        cand = root / name
        if (cand / "commercehub_invoice_export.py").is_file():
            return cand
    env = (os.environ.get("COMMERCEHUB_INVOICE_REPORT_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return root / "invoice report"


def run_retail_invoices_via_cdp(
    cdp_url: str,
    *,
    report_day: date | None = None,
    invoice_report_dir: Path | None = None,
) -> None:
    """
    Depot + Lowe's invoice flows on the browser listening at ``cdp_url``.
    The sync Playwright session must already be logged into CommerceHub on that browser.
    """
    inv_dir = (invoice_report_dir or _invoice_report_dir()).resolve()
    if not (inv_dir / "commercehub_invoice_export.py").is_file():
        raise FileNotFoundError(
            f"commercehub_invoice_export.py not found under {inv_dir}. "
            "Set COMMERCEHUB_INVOICE_REPORT_DIR or add the invoice report folder to the repo."
        )

    async def _run() -> None:
        if str(inv_dir) not in sys.path:
            sys.path.insert(0, str(inv_dir))
        from commercehub_invoice_export import (  # noqa: WPS433
            _prepare_page_for_cdp_invoices,
            _run_depot_invoice_flow,
            _run_lowes_invoice_flow,
            load_project_dotenv,
            previous_business_day,
        )
        from playwright.async_api import async_playwright

        load_project_dotenv()
        os.environ.setdefault("COMMERCEHUB_CHAIN_FAST", "1")
        day = report_day if report_day is not None else previous_business_day()
        download_dir = Path(
            os.environ.get("COMMERCEHUB_DOWNLOAD_DIR", str(inv_dir / "downloads"))
        ).resolve()
        download_dir.mkdir(parents=True, exist_ok=True)

        print(
            f"CommerceHub invoice reports (Depot + Lowe's) via CDP on {cdp_url} "
            f"for {day.isoformat()}…",
            flush=True,
        )
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            if not browser.contexts:
                raise RuntimeError("CDP browser has no contexts after connect.")
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            await _prepare_page_for_cdp_invoices(page)
            try:
                await _run_depot_invoice_flow(page, download_dir, day)
            except Exception as exc:
                print(f"WARN: Depot invoice report failed: {exc}", flush=True)
            try:
                await _run_lowes_invoice_flow(page, download_dir, day)
            except Exception as exc:
                print(f"WARN: Lowe's invoice report failed: {exc}", flush=True)
        print("CommerceHub invoice reports (Depot + Lowe's) finished.", flush=True)

    run_async(_run())

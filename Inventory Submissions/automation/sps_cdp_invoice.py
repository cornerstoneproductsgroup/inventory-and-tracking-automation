"""Run SPS Tractor Supply invoice export on an already-open browser (CDP attach)."""

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


def run_tractor_invoice_via_cdp(
    cdp_url: str,
    *,
    report_day: date | None = None,
    invoice_report_dir: Path | None = None,
) -> None:
    """Tractor Supply invoice flow on the browser at ``cdp_url`` (already signed into SPS)."""
    inv_dir = (invoice_report_dir or _invoice_report_dir()).resolve()
    if not (inv_dir / "commercehub_invoice_export.py").is_file():
        raise FileNotFoundError(
            f"commercehub_invoice_export.py not found under {inv_dir}."
        )

    async def _run() -> None:
        if str(inv_dir) not in sys.path:
            sys.path.insert(0, str(inv_dir))
        from commercehub_invoice_export import (  # noqa: WPS433
            _run_sps_tractor_phase,
            load_project_dotenv,
            previous_business_day,
        )
        from commercehub_invoice_export import NAV_TIMEOUT_MS  # noqa: WPS433
        from playwright.async_api import async_playwright

        load_project_dotenv()
        day = report_day if report_day is not None else previous_business_day()
        download_dir = Path(
            os.environ.get("COMMERCEHUB_DOWNLOAD_DIR", str(inv_dir / "downloads"))
        ).resolve()
        download_dir.mkdir(parents=True, exist_ok=True)

        print(
            f"SPS Tractor Supply invoice report via CDP on {cdp_url} "
            f"for {day.isoformat()}…",
            flush=True,
        )
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            if not browser.contexts:
                raise RuntimeError("CDP browser has no contexts after connect.")
            context = browser.contexts[0]
            await _run_sps_tractor_phase(
                context,
                nav_timeout_ms=NAV_TIMEOUT_MS,
                report_day=day,
                download_dir=download_dir,
            )
        print("SPS Tractor Supply invoice report complete.", flush=True)

    run_async(_run())

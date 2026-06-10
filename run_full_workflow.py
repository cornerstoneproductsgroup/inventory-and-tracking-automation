"""
Orchestrates the full daily workflow.

Two independent lanes run in parallel (default); neither waits on the other:

  **Phase 0** (optional, All Steps) — Outlook vendor emails from z- Daily Vendor Orders (all vendors).

  **Phase 1** — CommerceHub invoice reports (Depot, Lowe's, Tractor) run as a separate subprocess
  (``commercehub_invoice_export.py``), same as menu **R**. This is more reliable than attaching
  to the chain browser via CDP.

  **CommerceHub (Rithum)** — one browser: inventory, Depot tracking/invoicing, Lowe's workflows,
  Depot Special Orders (ack + track + invoice).

  **SPS Commerce** — one browser: Tractor inventory, Tractor + Grainger tracking/invoicing.

Use ``--sequential-lanes`` to run CommerceHub fully, then SPS.

Optional skips: --skip-commercehub, --skip-sps-inventory, --skip-sps-tracking,
--skip-depot, --skip-lowes, --skip-invoice-report.

If any step fails (e.g. no invoices for a closed day), later phases still run. At the end,
a WORKFLOW RUN SUMMARY lists skipped steps (e.g. empty Rithum queues) and all errors.
Use --invoice-report-only to run only phase 0 (combine with --invoice-report-modes).
Use --invoice-report-date YYYY-MM-DD (or MM/DD/YYYY) for a custom invoice report day (Depot, Lowe's, Tractor).
Use --tracking-invoicing-only to skip inventories and run tracking lanes only.
Use --pull-orders-only to run only the morning order pull (CommerceHub PDF/CSV, SPS, warehouse print).
Use --fedex-batch-only to run only FedEx batch shipping (Lowe's CSV upload + labels).
Use --ups-online-batch-only to run only UPS.com batch file shipping (Home Depot CSV + labels).
Use --vendor-emails-only to run only Outlook vendor emails from z- Daily Vendor Orders.
Use --amazon-seller-download-only to download Amazon Deferred Transaction CSV to the Input share.

Each Inventory Submissions step uses Inventory Submissions\\.venv when present.
Invoice report picks the first interpreter that can import dotenv, playwright, and pandas:
``invoice report/.venv``, then Inventory Submissions ``.venv``, then ``sys.executable``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INVENTORY_DIR = ROOT / "Inventory Submissions"
LOWES_DIR = ROOT / "Lowe's Tracking Automation"
_INVOICE_REPORT_FOLDER = "CommerceHub Invoice Report (Depot and Lowe's)"
_INVOICE_REPORT_FOLDER_IN_REPO = "invoice report"
_INVOICE_EXPORT_SCRIPT = "commercehub_invoice_export.py"
_INVOICE_EXPORT_MODES = frozenset({"all", "depot", "lowes", "tractor", "retail"})


def discover_invoice_report_directory(cli_dir: Path | None) -> tuple[Path, list[Path]]:
    """
    Find the folder that contains commercehub_invoice_export.py.

    Tries in order: --invoice-report-dir, COMMERCEHUB_INVOICE_REPORT_DIR,
    ``<repo>/invoice report`` (copy of the invoice app inside this repo),
    ``<repo>/CommerceHub Invoice Report (Depot and Lowe's)`` (nested),
    ``<parent>/CommerceHub Invoice Report (Depot and Lowe's)`` (sibling of repo).
    """
    candidates: list[Path] = []
    if cli_dir is not None:
        candidates.append(cli_dir.expanduser())
    env_raw = (os.environ.get("COMMERCEHUB_INVOICE_REPORT_DIR") or "").strip()
    if env_raw:
        candidates.append(Path(env_raw).expanduser())
    candidates.append(ROOT / _INVOICE_REPORT_FOLDER_IN_REPO)
    candidates.append(ROOT / _INVOICE_REPORT_FOLDER)
    candidates.append(ROOT.parent / _INVOICE_REPORT_FOLDER)

    seen: set[str] = set()
    tried_resolved: list[Path] = []
    for raw in candidates:
        try:
            p = raw.expanduser().resolve()
        except Exception:
            p = raw.expanduser()
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        tried_resolved.append(p)
        try:
            if p.is_dir() and (p / _INVOICE_EXPORT_SCRIPT).is_file():
                return p, tried_resolved
        except OSError:
            continue

    fallback = (
        tried_resolved[0]
        if tried_resolved
        else (ROOT / _INVOICE_REPORT_FOLDER_IN_REPO)
    )
    return fallback, tried_resolved


def _normalize_invoice_report_modes(raw: list[str] | None) -> list[str]:
    """Return exporter argv tokens; collapse overlapping requests to a single subprocess."""
    if not raw:
        return ["all"]
    out: list[str] = []
    for m in raw:
        ml = (m or "").strip().lower()
        if ml not in _INVOICE_EXPORT_MODES:
            raise ValueError(f"Invalid invoice report mode {m!r}; expected one of {sorted(_INVOICE_EXPORT_MODES)}")
        out.append(ml)
    if "all" in out:
        return ["all"]
    keys = frozenset(out)
    if keys == frozenset({"depot", "lowes", "tractor"}):
        return ["all"]
    if keys == frozenset({"depot", "lowes"}):
        return ["retail"]
    return out


def parse_invoice_report_date(raw: str, invoice_dir: Path) -> date:
    """Parse a custom invoice date using the invoice report module."""
    invoice_dir = invoice_dir.resolve()
    inserted = False
    if str(invoice_dir) not in sys.path:
        sys.path.insert(0, str(invoice_dir))
        inserted = True
    try:
        from commercehub_previous_business_day import parse_report_date

        return parse_report_date(raw)
    finally:
        if inserted:
            sys.path.remove(str(invoice_dir))


def _inventory_venv_python() -> Path:
    if sys.platform == "win32":
        return INVENTORY_DIR / ".venv" / "Scripts" / "python.exe"
    return INVENTORY_DIR / ".venv" / "bin" / "python"


def resolve_project_python() -> str:
    candidate = _inventory_venv_python()
    if candidate.is_file():
        return str(candidate.resolve())
    return str(Path(sys.executable).resolve())


def _invoice_report_interpreter_ready(py: Path) -> bool:
    """True if this interpreter has the imports ``commercehub_invoice_export`` needs at startup."""
    try:
        r = subprocess.run(
            [str(py), "-c", "import dotenv, playwright, pandas"],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def resolve_invoice_report_python(invoice_dir: Path) -> tuple[list[str], str | None]:
    """Pick an interpreter that can import dotenv, playwright, and pandas.

    Tries in order: ``invoice_dir/.venv``, Inventory Submissions ``.venv``,
    then ``sys.executable``. De-duplicates by resolved path.

    Returns ``(argv_prefix, None)`` on success, or ``([], error_message)`` if none qualify.
    """
    if sys.platform == "win32":
        invoice_venv_py = invoice_dir / ".venv" / "Scripts" / "python.exe"
    else:
        invoice_venv_py = invoice_dir / ".venv" / "bin" / "python"

    inv_py = _inventory_venv_python()
    sys_py = Path(sys.executable)

    rows: list[tuple[str, Path, Path]] = [
        ("invoice report", invoice_venv_py, invoice_dir / "requirements.txt"),
        ("Inventory Submissions", inv_py, INVENTORY_DIR / "requirements.txt"),
        ("current Python", sys_py, INVENTORY_DIR / "requirements.txt"),
    ]

    seen: set[str] = set()
    for label, py, req_txt in rows:
        if not py.is_file():
            continue
        try:
            key = str(py.resolve()).lower()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)

        if _invoice_report_interpreter_ready(py):
            return [str(py.resolve())], None

        pip_exe = py.parent / ("pip.exe" if sys.platform == "win32" else "pip")
        print(
            f"NOTE: {label} interpreter is missing dotenv/playwright/pandas — trying another.\n"
            f'      Fix: "{pip_exe}" install -r "{req_txt}"',
            flush=True,
        )

    inv_pip = inv_py.parent / ("pip.exe" if sys.platform == "win32" else "pip")
    err = (
        "ERROR: No Python interpreter found with dotenv, playwright, and pandas (required for invoice reports).\n"
        f'Install into Inventory Submissions (recommended):\n  "{inv_pip}" install -r "{INVENTORY_DIR / "requirements.txt"}"'
    )
    if invoice_venv_py.is_file():
        ip = invoice_venv_py.parent / ("pip.exe" if sys.platform == "win32" else "pip")
        err += (
            f'\nOr into invoice report only:\n  "{ip}" install -r "{invoice_dir / "requirements.txt"}"'
        )
    return [], err


def _workflow_report_init() -> None:
    if str(INVENTORY_DIR) not in sys.path:
        sys.path.insert(0, str(INVENTORY_DIR))
    try:
        from automation.workflow_run_report import init_run_report

        init_run_report(ROOT / ".workflow_run_report.jsonl")
        os.environ["WORKFLOW_RUN_REPORT_SUPPRESS_SUMMARY"] = "1"
    except ImportError:
        pass


def _workflow_report_finish(errors: list[str], *, success: bool) -> None:
    if str(INVENTORY_DIR) not in sys.path:
        sys.path.insert(0, str(INVENTORY_DIR))
    try:
        from automation.workflow_run_report import print_final_summary

        print_final_summary(extra_errors=errors, success=success)
    except ImportError:
        if errors:
            print("\nCompleted with errors:")
            for e in errors:
                print(f"  - {e}")
        elif success:
            print("\nAll workflow steps completed successfully.")


def _finish_and_return(code: int, errors: list[str] | None = None) -> int:
    errs = errors or []
    _workflow_report_finish(errs, success=(code == 0 and not errs))
    return code


def run_step(title: str, cmd: list[str], cwd: Path) -> tuple[bool, str]:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")
    if not cwd.is_dir():
        return False, f"Working directory does not exist: {cwd}"
    try:
        result = subprocess.run(cmd, cwd=str(cwd), check=False, env=os.environ.copy())
    except OSError as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, f"exit code {result.returncode}"
    return True, ""


def script_supports_flag(
    python_exe: str, script_name: str, flag: str, cwd: Path, timeout_s: int = 25
) -> bool:
    script_path = cwd / script_name
    if not script_path.is_file():
        return False
    try:
        probe = subprocess.run(
            [python_exe, script_name, "--help"],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return flag in f"{probe.stdout}\n{probe.stderr}"


def _build_chain_cmd(
    python_exe: str,
    *,
    skip_inventory: bool,
    skip_depot: bool,
    skip_lowes: bool,
    lowes_submit: bool,
    with_invoice_reports: bool = False,
    invoice_report_date: date | None = None,
    skip_special_orders: bool = False,
) -> list[str]:
    cmd: list[str] = [
        python_exe,
        "run_commercehub_chain.py",
        "--lowes-config",
        str(LOWES_DIR / "config.example.json"),
    ]
    if lowes_submit:
        cmd.append("--submit")
    if skip_inventory:
        cmd.append("--skip-inventory")
    if skip_depot:
        cmd.append("--skip-depot")
    if skip_lowes:
        cmd.append("--skip-lowes")
    if skip_special_orders:
        cmd.append("--skip-special-orders")
    if with_invoice_reports:
        cmd.append("--with-invoice-reports")
    if invoice_report_date is not None:
        cmd.extend(["--invoice-report-date", invoice_report_date.isoformat()])
    return cmd


def _build_sps_lane_cmd(
    python_exe: str,
    *,
    skip_inventory: bool,
    skip_tracking: bool,
    skip_grainger: bool,
    skip_tractor: bool,
    with_invoice_reports: bool,
    invoice_report_date: date | None,
    submit: bool,
    interactive_login: bool,
) -> list[str]:
    cmd: list[str] = [python_exe, "run_sps_lane.py"]
    if skip_inventory:
        cmd.append("--skip-inventory")
    if skip_tracking:
        cmd.append("--skip-tracking")
    if skip_grainger:
        cmd.append("--skip-grainger")
    if skip_tractor:
        cmd.append("--skip-tractor")
    if with_invoice_reports:
        cmd.append("--with-invoice-reports")
    if invoice_report_date is not None:
        cmd.extend(["--invoice-report-date", invoice_report_date.isoformat()])
    if submit:
        cmd.append("--submit")
    if interactive_login:
        cmd.append("--interactive-login")
    return cmd


def _run_parallel_lane_sequences(
    left_steps: list[tuple[str, list[str], Path]],
    right_steps: list[tuple[str, list[str], Path]],
    *,
    lane_label: str,
    sequential: bool,
) -> list[str]:
    """Run two full lane step lists in parallel (each lane keeps one browser for all its steps)."""
    errs: list[str] = []
    if left_steps and right_steps:
        if sequential:
            print(f"\nNOTE: {lane_label} — sequential (--sequential-lanes).")
            errs.extend(_run_step_sequence(left_steps))
            errs.extend(_run_step_sequence(right_steps))
        else:
            print(
                "\n"
                + "=" * 60
                + f"\n{lane_label} — parallel lanes (independent browsers)\n"
                + "=" * 60
                + "\n  Lane A: CommerceHub (Rithum) — invoice + inventory + tracking in one session\n"
                + "  Lane B: SPS Commerce — invoice + inventory + tracking in one session\n"
                + "\nEach lane runs start-to-finish without waiting on the other.\n"
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_l = pool.submit(_run_step_sequence, left_steps)
                fut_r = pool.submit(_run_step_sequence, right_steps)
                errs.extend(fut_l.result())
                errs.extend(fut_r.result())
    elif left_steps:
        errs.extend(_run_step_sequence(left_steps))
    elif right_steps:
        errs.extend(_run_step_sequence(right_steps))
    return errs


def _run_single(title: str, cmd: list[str], cwd: Path) -> list[str]:
    ok, err_detail = run_step(title, cmd, cwd)
    if ok:
        return []
    msg = f"{title}: {err_detail}"
    print(f"[ERROR] {msg}")
    if str(INVENTORY_DIR) not in sys.path:
        sys.path.insert(0, str(INVENTORY_DIR))
    try:
        from automation.workflow_run_report import record_error

        record_error(title, err_detail)
    except ImportError:
        pass
    return [msg]


def _run_step_sequence(step_list: list[tuple[str, list[str], Path]]) -> list[str]:
    out: list[str] = []
    for title, cmd, cwd in step_list:
        out.extend(_run_single(title, cmd, cwd))
    return out


_INVOICE_MODE_LABELS = {
    "all": "All (parallel: CH Depot+Lowe's + SPS Tractor)",
    "retail": "Depot + Lowe's (CommerceHub, one browser)",
    "depot": "Depot",
    "lowes": "Lowe's",
    "tractor": "Tractor Supply",
}


def _run_vendor_emails_phase(python_exe: str) -> list[str]:
    """Send daily vendor-order emails to all vendors (no interactive menu)."""
    vendor_script = INVENTORY_DIR / "run_vendor_emails.py"
    if not vendor_script.is_file():
        msg = (
            f"Vendor email script not found:\n  {vendor_script}\n"
            "Update/pull Inventory Submissions and retry."
        )
        print(f"\nWARN: {msg}")
        return [msg]

    print(
        "\n"
        + "=" * 60
        + "\nPhase 0 — Vendor emails (ALL vendors, Outlook send)\n"
        + "=" * 60
    )
    return _run_single(
        "Vendor Emails",
        [python_exe, str(vendor_script), "--send", "--no-menu"],
        INVENTORY_DIR,
    )


def _run_invoice_report_phase(
    invoice_modes: list[str],
    invoice_report_dir: Path,
    invoice_search_tried: list[Path],
    invoice_report_date: date | None,
) -> list[str]:
    """Run commercehub_invoice_export.py subprocess(es) — same path as menu R."""
    errors: list[str] = []
    export_script = invoice_report_dir / _INVOICE_EXPORT_SCRIPT
    if not export_script.is_file():
        checked = (
            "\n".join(f"  - {p}" for p in invoice_search_tried) if invoice_search_tried else "  (none)"
        )
        msg = (
            f"Invoice report script not found:\n  {export_script}\n"
            f"Checked locations:\n{checked}\n"
            'Fix: copy the invoice report project into this repo as "invoice report", or use the full folder name:\n'
            f"  {ROOT / _INVOICE_REPORT_FOLDER_IN_REPO}  (recommended)\n"
            f"  {ROOT / _INVOICE_REPORT_FOLDER}\n"
            f"  or next to the repo: {ROOT.parent / _INVOICE_REPORT_FOLDER}\n"
            "  or set environment variable COMMERCEHUB_INVOICE_REPORT_DIR to that folder, "
            "or pass --invoice-report-dir on the command line."
        )
        print(f"\nWARN: {msg}")
        return [msg]

    print(
        "\n"
        + "=" * 60
        + "\nPhase 1 — Invoice reports (Depot, Lowe's, Tractor)\n"
        + "=" * 60
        + f"\n  Modes: {', '.join(invoice_modes)}\n"
        + (
            f"  Report date: {invoice_report_date.isoformat()} (custom)\n"
            if invoice_report_date is not None
            else "  Report date: previous business day (default)\n"
        )
        + "  (Separate browser session — same as menu R.)\n"
    )
    inv_py_parts, invoice_py_err = resolve_invoice_report_python(invoice_report_dir)
    if invoice_py_err:
        print(f"\n{invoice_py_err}")
        return [invoice_py_err]
    if not inv_py_parts:
        msg = "ERROR: Could not resolve an interpreter for invoice reports."
        print(f"\n{msg}")
        return [msg]

    for mode in invoice_modes:
        label = _INVOICE_MODE_LABELS.get(mode, mode)
        cmd = inv_py_parts + [str(export_script), mode]
        if invoice_report_date is not None:
            cmd.extend(["--date", invoice_report_date.isoformat()])
        title = f"Invoice report — {label}"
        errors.extend(_run_single(title, cmd, invoice_report_dir))
    return errors


def _run_parallel_pair(
    left: tuple[str, list[str], Path] | None,
    right_steps: list[tuple[str, list[str], Path]],
    *,
    phase_label: str,
    sequential: bool,
) -> list[str]:
    """Run one optional left subprocess and/or a sequence on the right (same thread if both lanes)."""
    errs: list[str] = []
    if left and right_steps:
        if sequential:
            print(f"\nNOTE: {phase_label} — sequential (--sequential-lanes).")
            errs.extend(_run_single(*left))
            errs.extend(_run_step_sequence(right_steps))
        else:
            print(
                "\n"
                + "=" * 60
                + f"\n{phase_label} — parallel lanes\n"
                + "=" * 60
                + "\n  Lane A: CommerceHub (when present)\n"
                + f"  Lane B: {len(right_steps)} SPS step(s)\n"
                + "\nComplete any logins or prompts in either window as they appear.\n"
            )
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fut_l = pool.submit(_run_single, *left)
                fut_r = pool.submit(_run_step_sequence, right_steps)
                errs.extend(fut_l.result())
                errs.extend(fut_r.result())
    elif left:
        errs.extend(_run_single(*left))
    elif right_steps:
        errs.extend(_run_step_sequence(right_steps))
    return errs


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Full workflow: CommerceHub and SPS Commerce lanes in parallel; each lane uses one "
            "browser for invoice reports (optional), inventory, and tracking/invoicing."
        )
    )
    parser.add_argument(
        "--dry-run-lowes",
        action="store_true",
        help="Omit --submit for Lowe's/Depot steps in the chain (inventory still submits).",
    )
    parser.add_argument(
        "--skip-sps-tracking",
        action="store_true",
        help="Skip SPS Tractor Supply tracking step after SPS inventory.",
    )
    parser.add_argument(
        "--skip-commercehub",
        action="store_true",
        help="Skip CommerceHub chain (Rithum inventory, Depot, Lowe's).",
    )
    parser.add_argument(
        "--skip-depot",
        action="store_true",
        help="Skip Home Depot tracking/invoicing inside the CommerceHub chain.",
    )
    parser.add_argument(
        "--skip-lowes",
        action="store_true",
        help="Skip Lowe's tracking/invoicing inside the CommerceHub chain.",
    )
    parser.add_argument(
        "--skip-special-orders",
        action="store_true",
        help="Skip Home Depot Special Orders (thdso) inside the CommerceHub chain.",
    )
    parser.add_argument(
        "--skip-inventory",
        action="store_true",
        help="Skip Rithum/CommerceHub inventory inside the CommerceHub chain.",
    )
    parser.add_argument(
        "--skip-sps-inventory",
        action="store_true",
        help="Skip SPS Tractor Supply inventory.",
    )
    parser.add_argument(
        "--tracking-invoicing-only",
        action="store_true",
        help=(
            "Skip Rithum/CommerceHub inventory and SPS inventory; run Depot/Lowe's + SPS tracking only. "
            "Cannot be combined with --skip-commercehub."
        ),
    )
    parser.add_argument(
        "--force-sps-interactive-login",
        action="store_true",
        help="Force SPS tracking step to open interactive login and save a fresh session file.",
    )
    parser.add_argument(
        "--run-grainger-all",
        action="store_true",
        help="After SPS Tractor Supply tracking, run SPS tracking flow for Partner=Grainger.",
    )
    parser.add_argument(
        "--grainger-only",
        action="store_true",
        help="Run only SPS Grainger ALL flow.",
    )
    parser.add_argument(
        "--pull-orders-only",
        action="store_true",
        help=(
            "Run only the morning pull-orders workflow (CommerceHub PDF/CSV, SPS Tractor/Grainger, "
            "warehouse print). Not part of All Steps unless you add it there later."
        ),
    )
    parser.add_argument(
        "--ups-online-batch-only",
        action="store_true",
        help=(
            "Run only UPS.com batch file shipping for Home Depot "
            "(login → upload CSV → process → save labels). Not part of All Steps."
        ),
    )
    parser.add_argument(
        "--fedex-batch-only",
        action="store_true",
        help=(
            "Run only FedEx batch shipping: upload Lowe's CSV, finalize shipments, "
            "save labels by SKU/vendor map."
        ),
    )
    parser.add_argument(
        "--amazon-seller-download-only",
        action="store_true",
        help=(
            "Run only Amazon Seller Central download: Deferred Transaction CSV to "
            "Invoice Reports\\Amazon\\Input (previous day filename)."
        ),
    )
    parser.add_argument(
        "--vendor-emails-only",
        action="store_true",
        help=(
            "Run only Outlook vendor emails from z- Daily Vendor Orders "
            "(uses Inventory Submissions/vendor_email_config.json)."
        ),
    )
    parser.add_argument(
        "--with-vendor-emails",
        action="store_true",
        help=(
            "At the start of a full workflow run, send vendor emails to ALL configured vendors "
            "(Outlook, no vendor menu). Used by main menu All Steps."
        ),
    )
    parser.add_argument(
        "--sequential-lanes",
        action="store_true",
        help="Within each phase, run CommerceHub then SPS one after the other instead of parallel.",
    )
    parser.add_argument(
        "--skip-invoice-report",
        action="store_true",
        help="Skip invoice report phase at the start.",
    )
    parser.add_argument(
        "--invoice-report-dir",
        type=Path,
        default=None,
        help=(
            "Folder with commercehub_invoice_export.py. If omitted, searches: "
            "COMMERCEHUB_INVOICE_REPORT_DIR, then <repo>/CommerceHub Invoice Report…, "
            "then <parent>/CommerceHub Invoice Report…"
        ),
    )
    parser.add_argument(
        "--invoice-report-modes",
        nargs="+",
        default=None,
        metavar="MODE",
        help=(
            "Modes for commercehub_invoice_export.py: all, retail, depot, lowes, tractor. "
            "Default when invoices run: all (parallel: CommerceHub Depot+Lowe's, SPS Tractor). "
            "depot+lowes together is normalized to one retail run. depot+lowes+tractor becomes all."
        ),
    )
    parser.add_argument(
        "--invoice-report-only",
        action="store_true",
        help="Run only invoice report phase(s) and exit (no CommerceHub chain, no SPS).",
    )
    parser.add_argument(
        "--invoice-report-date",
        metavar="DATE",
        default=None,
        help=(
            "Custom calendar date for invoice reports (Depot, Lowe's, Tractor Supply). "
            "Formats: YYYY-MM-DD or MM/DD/YYYY. Default is previous business day."
        ),
    )

    args = parser.parse_args()

    if args.tracking_invoicing_only and args.skip_commercehub:
        parser.error("--tracking-invoicing-only cannot be combined with --skip-commercehub")
    if args.invoice_report_only and args.skip_invoice_report:
        parser.error("--invoice-report-only cannot be combined with --skip-invoice-report")
    if args.pull_orders_only and args.invoice_report_only:
        parser.error("--pull-orders-only cannot be combined with --invoice-report-only")
    if args.ups_online_batch_only and args.invoice_report_only:
        parser.error("--ups-online-batch-only cannot be combined with --invoice-report-only")
    if args.pull_orders_only and args.ups_online_batch_only:
        parser.error("--pull-orders-only cannot be combined with --ups-online-batch-only")
    if args.fedex_batch_only and args.invoice_report_only:
        parser.error("--fedex-batch-only cannot be combined with --invoice-report-only")
    if args.fedex_batch_only and args.ups_online_batch_only:
        parser.error("--fedex-batch-only cannot be combined with --ups-online-batch-only")
    if args.fedex_batch_only and args.pull_orders_only:
        parser.error("--fedex-batch-only cannot be combined with --pull-orders-only")
    if args.amazon_seller_download_only and args.invoice_report_only:
        parser.error("--amazon-seller-download-only cannot be combined with --invoice-report-only")
    if args.amazon_seller_download_only and args.ups_online_batch_only:
        parser.error("--amazon-seller-download-only cannot be combined with --ups-online-batch-only")
    if args.amazon_seller_download_only and args.pull_orders_only:
        parser.error("--amazon-seller-download-only cannot be combined with --pull-orders-only")
    if args.amazon_seller_download_only and args.fedex_batch_only:
        parser.error("--amazon-seller-download-only cannot be combined with --fedex-batch-only")
    if args.vendor_emails_only and args.invoice_report_only:
        parser.error("--vendor-emails-only cannot be combined with --invoice-report-only")
    if args.vendor_emails_only and args.ups_online_batch_only:
        parser.error("--vendor-emails-only cannot be combined with --ups-online-batch-only")
    if args.vendor_emails_only and args.pull_orders_only:
        parser.error("--vendor-emails-only cannot be combined with --pull-orders-only")
    if args.vendor_emails_only and args.fedex_batch_only:
        parser.error("--vendor-emails-only cannot be combined with --fedex-batch-only")
    if args.vendor_emails_only and args.amazon_seller_download_only:
        parser.error("--vendor-emails-only cannot be combined with --amazon-seller-download-only")
    if args.with_vendor_emails and args.vendor_emails_only:
        parser.error("Use either --with-vendor-emails or --vendor-emails-only, not both.")

    tracking_invoicing_only = bool(args.tracking_invoicing_only)
    pull_orders_only = bool(args.pull_orders_only)
    ups_online_batch_only = bool(args.ups_online_batch_only)
    fedex_batch_only = bool(args.fedex_batch_only)
    amazon_seller_download_only = bool(args.amazon_seller_download_only)
    vendor_emails_only = bool(args.vendor_emails_only)
    with_vendor_emails = bool(args.with_vendor_emails)
    grainger_only = bool(args.grainger_only)
    invoice_report_only = bool(args.invoice_report_only)
    skip_inventory = bool(args.skip_inventory) or tracking_invoicing_only
    lowes_submit = not args.dry_run_lowes
    run_sps_tracking = not args.skip_sps_tracking
    skip_commercehub = bool(args.skip_commercehub)
    skip_sps_inventory = bool(args.skip_sps_inventory) or tracking_invoicing_only
    skip_depot = bool(args.skip_depot)
    skip_lowes = bool(args.skip_lowes)
    skip_special_orders = bool(args.skip_special_orders)
    run_grainger_all = bool(args.run_grainger_all) or grainger_only
    sequential_lanes = bool(args.sequential_lanes)
    skip_invoice_report = bool(args.skip_invoice_report)
    invoice_report_dir, invoice_search_tried = discover_invoice_report_directory(args.invoice_report_dir)
    invoice_report_date: date | None = None
    if args.invoice_report_date:
        try:
            invoice_report_date = parse_invoice_report_date(
                args.invoice_report_date.strip(), invoice_report_dir
            )
        except ValueError as exc:
            print(f"\nERROR: {exc}")
            return 1

    if grainger_only:
        skip_commercehub = True
        skip_sps_inventory = True
        run_sps_tracking = True
        skip_invoice_report = True

    if invoice_report_only:
        skip_commercehub = True
        skip_sps_inventory = True
        run_sps_tracking = False
        run_grainger_all = False
        grainger_only = False
        skip_invoice_report = False

    try:
        invoice_modes = (
            None
            if skip_invoice_report
            else _normalize_invoice_report_modes(args.invoice_report_modes)
        )
    except ValueError as exc:
        print(f"\nERROR: {exc}")
        return 1

    if tracking_invoicing_only:
        print(
            "Mode: tracking + invoicing only — inventories skipped; each lane uses one browser "
            "(CommerceHub: Depot/Lowe's/Special Orders; SPS: Tractor/Grainger as configured)."
        )

    python_exe = resolve_project_python()
    if not _inventory_venv_python().is_file():
        print(
            "NOTE: No venv at Inventory Submissions\\.venv — using the current Python.\n"
            "      Create it and install deps (recommended, from cmd.exe):\n"
            f'      cd /d "{INVENTORY_DIR}"\n'
            "      python -m venv .venv\n"
            "      .venv\\Scripts\\pip install -r requirements.txt\n"
            "      .venv\\Scripts\\playwright install chromium"
        )
    else:
        print(f"Using project Python: {python_exe}")

    _workflow_report_init()

    if pull_orders_only:
        pull_script = INVENTORY_DIR / "run_pull_orders.py"
        if not pull_script.is_file():
            print(
                f"\nERROR: Missing pull-orders script:\n  {pull_script}\n"
                "Update/pull Inventory Submissions and retry."
            )
            return 1
        print(
            "\n"
            + "=" * 60
            + "\nPull Orders — CommerceHub PDF/CSV, SPS Tractor/Grainger, warehouse print\n"
            + "=" * 60
        )
        pull_cmd = [python_exe, str(pull_script)]
        if args.invoice_report_date:
            pull_cmd.extend(["--date", args.invoice_report_date.strip()])
        pull_errors = _run_single("Pull Orders", pull_cmd, INVENTORY_DIR)
        if pull_errors:
            return _finish_and_return(1, pull_errors)
        print("\nPull orders completed successfully.")
        return _finish_and_return(0)

    if fedex_batch_only:
        fedex_script = INVENTORY_DIR / "run_fedex_batch.py"
        if not fedex_script.is_file():
            print(
                f"\nERROR: Missing FedEx batch script:\n  {fedex_script}\n"
                "Update/pull Inventory Submissions and retry."
            )
            return 1
        print(
            "\n"
            + "=" * 60
            + "\nFedEx Batch — Lowe's CSV upload, finalize, label save by SKU\n"
            + "=" * 60
        )
        fedex_cmd = [python_exe, str(fedex_script)]
        if args.invoice_report_date:
            fedex_cmd.extend(["--date", args.invoice_report_date.strip()])
        errors = _run_single("FedEx Batch", fedex_cmd, INVENTORY_DIR)
        if errors:
            print("\nCompleted with errors:")
            for e in errors:
                print(f"  - {e}")
            return 1
        print("\nFedEx batch completed successfully.")
        return 0

    if amazon_seller_download_only:
        invoice_report_dir, invoice_search_tried = discover_invoice_report_directory(args.invoice_report_dir)
        amazon_script = invoice_report_dir / "run_amazon_seller_download.py"
        if not amazon_script.is_file():
            tried = "\n".join(f"  - {p}" for p in invoice_search_tried) if invoice_search_tried else ""
            print(
                f"\nERROR: Missing Amazon seller download script:\n  {amazon_script}\n"
                f"Searched:\n{tried}\n"
                "Update/pull invoice report folder and retry."
            )
            return 1
        inv_py_parts, invoice_py_err = resolve_invoice_report_python(invoice_report_dir)
        if invoice_py_err:
            print(f"\n{invoice_py_err}")
            return 1
        if not inv_py_parts:
            print("\nERROR: Could not resolve Python for Amazon seller download.")
            return 1
        print(
            "\n"
            + "=" * 60
            + "\nAmazon Seller — Deferred Transaction CSV download\n"
            + "=" * 60
        )
        amazon_cmd = [*inv_py_parts, str(amazon_script)]
        if args.invoice_report_date:
            amazon_cmd.extend(["--date", args.invoice_report_date.strip()])
        errors = _run_single("Amazon Seller download", amazon_cmd, invoice_report_dir)
        if errors:
            print("\nCompleted with errors:")
            for e in errors:
                print(f"  - {e}")
            return 1
        print("\nAmazon seller download step finished (may be on hold — see script output).")
        return 0

    if vendor_emails_only:
        vendor_script = INVENTORY_DIR / "run_vendor_emails.py"
        if not vendor_script.is_file():
            print(
                f"\nERROR: Missing vendor email script:\n  {vendor_script}\n"
                "Update/pull Inventory Submissions and retry."
            )
            return 1
        print(
            "\n"
            + "=" * 60
            + "\nVendor Emails — Outlook send from Daily Vendor Orders\n"
            + "=" * 60
        )
        errors = _run_single(
            "Vendor Emails",
            [python_exe, str(vendor_script), "--send"],
            INVENTORY_DIR,
        )
        if errors:
            print("\nCompleted with errors:")
            for e in errors:
                print(f"  - {e}")
            return 1
        print("\nVendor emails completed successfully.")
        return 0

    if ups_online_batch_only:
        ups_script = INVENTORY_DIR / "run_ups_online_batch.py"
        if not ups_script.is_file():
            print(
                f"\nERROR: Missing UPS online batch script:\n  {ups_script}\n"
                "Update/pull Inventory Submissions and retry."
            )
            return 1
        print(
            "\n"
            + "=" * 60
            + "\nUPS.com Batch Shipping — Home Depot\n"
            + "=" * 60
        )
        errors = _run_single(
            "UPS Online Batch (Home Depot)",
            [python_exe, str(ups_script)],
            INVENTORY_DIR,
        )
        if errors:
            print("\nCompleted with errors:")
            for e in errors:
                print(f"  - {e}")
            return 1
        print("\nUPS online batch step completed successfully.")
        return 0

    required_chain_flags: list[str] = []
    if skip_inventory:
        required_chain_flags.append("--skip-inventory")
    if skip_depot:
        required_chain_flags.append("--skip-depot")
    if skip_lowes:
        required_chain_flags.append("--skip-lowes")
    if skip_special_orders:
        required_chain_flags.append("--skip-special-orders")

    for required_flag in required_chain_flags:
        if script_supports_flag(python_exe, "run_commercehub_chain.py", required_flag, INVENTORY_DIR):
            continue
        print(
            "\nERROR: this mode requires an updated Inventory Submissions script:\n"
            f"  - run_commercehub_chain.py must support {required_flag}\n"
            "\nOn this machine, that flag is not available. Update/pull Inventory Submissions and retry.\n"
            "Quick check after update:\n"
            f'  cd /d "{INVENTORY_DIR}"\n'
            "  python run_commercehub_chain.py --help\n"
            f"  (should list {required_flag})\n"
        )
        return 1

    if tracking_invoicing_only and not invoice_report_only and not (INVENTORY_DIR / "run_sps_tracking.py").is_file():
        print(
            "\nERROR: tracking + invoicing only requires SPS tracking script:\n"
            f"  - Missing: {INVENTORY_DIR / 'run_sps_tracking.py'}\n"
            "\nUpdate/pull Inventory Submissions on this machine and retry."
        )
        return 1

    sps_lane_script = INVENTORY_DIR / "run_sps_lane.py"
    sps_storage_json = INVENTORY_DIR / "sps_playwright_storage.json"
    tracking_script = INVENTORY_DIR / "run_sps_tracking.py"

    ch_lane_steps: list[tuple[str, list[str], Path]] = []
    sps_lane_steps: list[tuple[str, list[str], Path]] = []

    wants_ch_work = not skip_commercehub and (
        not skip_inventory
        or not skip_depot
        or not skip_lowes
        or not skip_special_orders
    )
    wants_sps_work = not skip_sps_inventory or run_sps_tracking or run_grainger_all

    if wants_ch_work:
        ch_cmd = _build_chain_cmd(
            python_exe,
            skip_inventory=skip_inventory,
            skip_depot=skip_depot,
            skip_lowes=skip_lowes,
            lowes_submit=lowes_submit,
            with_invoice_reports=False,
            invoice_report_date=invoice_report_date,
            skip_special_orders=skip_special_orders,
        )
        ch_lane_steps.append(
            (
                "CommerceHub lane (invoice + inventory + Depot/Lowe's + Special Orders)",
                ch_cmd,
                INVENTORY_DIR,
            )
        )
    elif not skip_commercehub:
        print("NOTE: CommerceHub selected but no CommerceHub actions enabled; skipping that step.")

    if wants_sps_work:
        if not sps_lane_script.is_file():
            print(
                "NOTE: SPS lane script not found:\n"
                f"      {sps_lane_script}\n"
                "      Update/pull Inventory Submissions (run_sps_lane.py) to enable the unified SPS session."
            )
        else:
            sps_interactive = args.force_sps_interactive_login or (
                not sps_storage_json.is_file()
                and os.environ.get("SPS_TRACKING_NON_INTERACTIVE", "").strip().lower()
                not in ("1", "true", "yes", "y", "on")
            )
            if sps_interactive and not args.force_sps_interactive_login:
                print(
                    "\nNOTE: No sps_playwright_storage.json — SPS lane will pause for sign-in if needed.\n"
                    "      Set SPS_TRACKING_NON_INTERACTIVE=1 to skip interactive login prompts.\n"
                )
            sps_cmd = _build_sps_lane_cmd(
                python_exe,
                skip_inventory=skip_sps_inventory,
                skip_tracking=not run_sps_tracking,
                skip_grainger=not run_grainger_all,
                skip_tractor=grainger_only,
                with_invoice_reports=False,
                invoice_report_date=invoice_report_date,
                submit=True,
                interactive_login=sps_interactive,
            )
            sps_lane_steps.append(
                (
                    "SPS Commerce lane (invoice + inventory + Tractor/Grainger tracking)",
                    sps_cmd,
                    INVENTORY_DIR,
                )
            )

    has_work = bool(ch_lane_steps or sps_lane_steps)
    if invoice_report_only:
        has_work = has_work or bool(invoice_modes)
    if not has_work:
        print(
            "ERROR: No steps to run (everything skipped). "
            "Omit some --skip-* flags or run without skipping all steps."
        )
        return 1

    required_names: list[str] = []
    if not skip_commercehub:
        required_names.append("run_commercehub_chain.py")
    if wants_sps_work:
        required_names.append("run_sps_lane.py")
    if run_sps_tracking and tracking_script.is_file() and not sps_lane_script.is_file():
        required_names.append("run_sps_tracking.py")

    missing_scripts = [str(INVENTORY_DIR / n) for n in required_names if not (INVENTORY_DIR / n).is_file()]
    if missing_scripts:
        print("\nERROR: Missing script(s) under Inventory Submissions:")
        for path in missing_scripts:
            print(f"  - {path}")
        print(
            "\nThis folder must contain the same Python files as your main PC (including run_commercehub_chain.py).\n"
            f'  cd /d "{ROOT}"\n'
            "  git pull origin main\n"
        )
        return 1

    errors: list[str] = []

    if with_vendor_emails:
        errors.extend(_run_vendor_emails_phase(python_exe))

    if invoice_modes:
        errors.extend(
            _run_invoice_report_phase(
                invoice_modes,
                invoice_report_dir,
                invoice_search_tried,
                invoice_report_date,
            )
        )
        if errors and invoice_report_only:
            print(
                "\n"
                + "=" * 60
                + "\nWARN: Invoice report step(s) had issues.\n"
                + "=" * 60
            )

    if not invoice_report_only and (ch_lane_steps or sps_lane_steps):
        errors.extend(
            _run_parallel_lane_sequences(
                ch_lane_steps,
                sps_lane_steps,
                lane_label="Daily workflow",
                sequential=sequential_lanes,
            )
        )

    return _finish_and_return(1 if errors else 0, errors)


if __name__ == "__main__":
    sys.exit(main())

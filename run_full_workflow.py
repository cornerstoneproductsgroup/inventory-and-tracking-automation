"""
Orchestrates the full daily workflow.

Phases (default):
  0 — CommerceHub invoice reports (sibling project), first. Default mode is ``all`` (Depot + Lowe's +
      Tractor Supply in one exporter run). Override with ``--invoice-report-modes depot lowes`` etc.
  1 — Inventories in parallel when both sides run: Rithum inventory (CommerceHub) and
      Tractor Supply inventory (SPS), each in its own browser.
  2 — Tracking / invoicing in parallel when both sides run: Depot + Lowe's (CommerceHub) and
      SPS Tractor Supply tracking (+ optional Grainger), each lane in its own browser.

CommerceHub is split into two subprocesses when you need both Rithum inventory and Depot/Lowe's
and any SPS step is enabled (two separate logins to CommerceHub).

Optional skips: --skip-commercehub, --skip-sps-inventory, --skip-sps-tracking,
--skip-depot, --skip-lowes, --skip-invoice-report.
Use --invoice-report-only to run only phase 0 (combine with --invoice-report-modes).
Use --tracking-invoicing-only to skip inventories and run tracking lanes only.
Use --sequential-lanes to run each phase's two sides one after the other instead of parallel.

Each Inventory Submissions step uses Inventory Submissions\\.venv when present.
Invoice report uses that project's .venv if present, else py -3.13 on Windows, else the same venv.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INVENTORY_DIR = ROOT / "Inventory Submissions"
LOWES_DIR = ROOT / "Lowe's Tracking Automation"
DEFAULT_INVOICE_REPORT_DIR = ROOT.parent / "CommerceHub Invoice Report (Depot and Lowe's)"
_INVOICE_EXPORT_MODES = frozenset({"all", "depot", "lowes", "tractor"})


def _normalize_invoice_report_modes(raw: list[str] | None) -> list[str]:
    """Return exporter argv tokens; if ``all`` appears, run a single ``all`` pass only."""
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
    return out


def resolve_project_python() -> str:
    if sys.platform == "win32":
        candidate = INVENTORY_DIR / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = INVENTORY_DIR / ".venv" / "bin" / "python"
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def resolve_invoice_report_python(invoice_dir: Path) -> list[str]:
    """Argv prefix to run commercehub_invoice_export.py (venv, py launcher, or project Python)."""
    if sys.platform == "win32":
        venv_py = invoice_dir / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = invoice_dir / ".venv" / "bin" / "python"
    if venv_py.is_file():
        return [str(venv_py)]
    if sys.platform == "win32" and shutil.which("py"):
        return ["py", "-3.13"]
    return [resolve_project_python()]


def run_step(title: str, cmd: list[str], cwd: Path) -> tuple[bool, str]:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")
    if not cwd.is_dir():
        return False, f"Working directory does not exist: {cwd}"
    try:
        result = subprocess.run(cmd, cwd=str(cwd), check=False)
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
    return cmd


def _run_single(title: str, cmd: list[str], cwd: Path) -> list[str]:
    ok, err_detail = run_step(title, cmd, cwd)
    if ok:
        return []
    msg = f"{title}: {err_detail}"
    print(f"[ERROR] {msg}")
    return [msg]


def _run_step_sequence(step_list: list[tuple[str, list[str], Path]]) -> list[str]:
    out: list[str] = []
    for title, cmd, cwd in step_list:
        out.extend(_run_single(title, cmd, cwd))
    return out


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
            "Full workflow: invoice reports (optional), then inventory lanes in parallel, "
            "then tracking lanes in parallel (CommerceHub vs SPS)."
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
        help=f"Folder with commercehub_invoice_export.py (default: {DEFAULT_INVOICE_REPORT_DIR}).",
    )
    parser.add_argument(
        "--invoice-report-modes",
        nargs="+",
        default=None,
        metavar="MODE",
        help=(
            "Modes for commercehub_invoice_export.py: all, depot, lowes, tractor. "
            "Default when invoices run: all (one run: Depot + Lowe's + Tractor Supply). "
            "Example: --invoice-report-modes depot lowes (two runs)."
        ),
    )
    parser.add_argument(
        "--invoice-report-only",
        action="store_true",
        help="Run only invoice report phase(s) and exit (no CommerceHub chain, no SPS).",
    )

    args = parser.parse_args()

    if args.tracking_invoicing_only and args.skip_commercehub:
        parser.error("--tracking-invoicing-only cannot be combined with --skip-commercehub")
    if args.invoice_report_only and args.skip_invoice_report:
        parser.error("--invoice-report-only cannot be combined with --skip-invoice-report")

    tracking_invoicing_only = bool(args.tracking_invoicing_only)
    grainger_only = bool(args.grainger_only)
    invoice_report_only = bool(args.invoice_report_only)
    skip_inventory = bool(args.skip_inventory) or tracking_invoicing_only
    lowes_submit = not args.dry_run_lowes
    run_sps_tracking = not args.skip_sps_tracking
    skip_commercehub = bool(args.skip_commercehub)
    skip_sps_inventory = bool(args.skip_sps_inventory) or tracking_invoicing_only
    skip_depot = bool(args.skip_depot)
    skip_lowes = bool(args.skip_lowes)
    run_grainger_all = bool(args.run_grainger_all) or grainger_only
    sequential_lanes = bool(args.sequential_lanes)
    skip_invoice_report = bool(args.skip_invoice_report)
    invoice_report_dir = args.invoice_report_dir or Path(
        os.environ.get("COMMERCEHUB_INVOICE_REPORT_DIR", str(DEFAULT_INVOICE_REPORT_DIR))
    )

    if grainger_only:
        skip_commercehub = True
        skip_sps_inventory = True
        run_sps_tracking = False
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
            "Mode: tracking + invoicing only — inventories skipped; CommerceHub runs with "
            "--skip-inventory (Depot/Lowe's), then SPS tracking."
        )

    python_exe = resolve_project_python()
    if python_exe == sys.executable:
        print(
            "NOTE: No venv at Inventory Submissions\\.venv — using the current Python.\n"
            "      Create it and install deps (recommended, from cmd.exe):\n"
            f'      cd /d "{INVENTORY_DIR}"\n'
            "      python -m venv .venv\n"
            r"      .venv\Scripts\pip install -r requirements.txt" "\n"
            r"      .venv\Scripts\playwright install chromium"
        )
    else:
        print(f"Using project Python: {python_exe}")

    required_chain_flags: list[str] = []
    if skip_inventory:
        required_chain_flags.append("--skip-inventory")
    if skip_depot:
        required_chain_flags.append("--skip-depot")
    if skip_lowes:
        required_chain_flags.append("--skip-lowes")

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

    tracking_script = INVENTORY_DIR / "run_sps_tracking.py"
    sps_storage_json = INVENTORY_DIR / "sps_playwright_storage.json"

    sps_inventory_entry: tuple[str, list[str], Path] | None = None
    sps_tracking_steps: list[tuple[str, list[str], Path]] = []

    if not skip_sps_inventory:
        sps_inventory_entry = (
            "SPS Commerce — Tractor Supply inventory",
            [python_exe, "run_sps.py"],
            INVENTORY_DIR,
        )

    if run_sps_tracking and tracking_script.is_file():
        tracking_cmd = [python_exe, "run_sps_tracking.py", "--submit"]
        need_interactive = args.force_sps_interactive_login or (
            not sps_storage_json.is_file()
            and os.environ.get("SPS_TRACKING_NON_INTERACTIVE", "").strip().lower()
            not in ("1", "true", "yes", "y", "on")
        )
        if need_interactive:
            tracking_cmd.append("--interactive-login")
            if args.force_sps_interactive_login:
                print(
                    "\nNOTE: Forcing interactive SPS login before tracking to refresh saved session.\n"
                    f"      Session file target: {sps_storage_json}\n"
                )
            else:
                print(
                    "\nNOTE: No sps_playwright_storage.json — tracking will pause once for SPS login in the browser, "
                    "then save that session for future runs.\n"
                    "      Set SPS_TRACKING_NON_INTERACTIVE=1 to skip this (automation must supply the file).\n"
                )
        sps_tracking_steps.append(
            (
                "SPS Commerce — Tractor Supply tracking",
                tracking_cmd,
                INVENTORY_DIR,
            )
        )
    elif run_sps_tracking:
        print(
            "NOTE: SPS tracking step requested, but script not found:\n"
            f"      {tracking_script}\n"
            "      Add run_sps_tracking.py under Inventory Submissions to enable this step."
        )

    if run_grainger_all and tracking_script.is_file():
        grainger_cmd = [python_exe, "run_sps_tracking.py", "--submit", "--partner", "Grainger"]
        need_interactive = args.force_sps_interactive_login or (
            not sps_storage_json.is_file()
            and os.environ.get("SPS_TRACKING_NON_INTERACTIVE", "").strip().lower() not in ("1", "true", "yes", "y", "on")
        )
        if need_interactive:
            grainger_cmd.append("--interactive-login")
        sps_tracking_steps.append(
            (
                "SPS Commerce — Grainger ALL",
                grainger_cmd,
                INVENTORY_DIR,
            )
        )
    elif run_grainger_all:
        print(
            "NOTE: SPS Grainger step requested, but script not found:\n"
            f"      {tracking_script}\n"
            "      Add run_sps_tracking.py under Inventory Submissions to enable this step."
        )

    wants_ch_inv = not skip_commercehub and not skip_inventory
    wants_ch_dlv = not skip_commercehub and (not skip_depot or not skip_lowes)
    has_any_sps = sps_inventory_entry is not None or bool(sps_tracking_steps)

    split_ch = False
    ch_inventory_entry: tuple[str, list[str], Path] | None = None
    ch_tracking_entry: tuple[str, list[str], Path] | None = None
    ch_single_entry: tuple[str, list[str], Path] | None = None

    if not skip_commercehub:
        split_ch = bool(wants_ch_inv and wants_ch_dlv and has_any_sps)
        if split_ch:
            cmd_inv = _build_chain_cmd(
                python_exe,
                skip_inventory=False,
                skip_depot=True,
                skip_lowes=True,
                lowes_submit=lowes_submit,
            )
            ch_inventory_entry = ("CommerceHub — Rithum inventory only (Lowe's + Home Depot IBL)", cmd_inv, INVENTORY_DIR)
            cmd_tr = _build_chain_cmd(
                python_exe,
                skip_inventory=True,
                skip_depot=skip_depot,
                skip_lowes=skip_lowes,
                lowes_submit=lowes_submit,
            )
            ch_tracking_entry = ("CommerceHub — Depot + Lowe's tracking & invoicing", cmd_tr, INVENTORY_DIR)
        elif wants_ch_inv or wants_ch_dlv:
            scope_parts: list[str] = []
            if wants_ch_inv:
                scope_parts.append("inventory")
            if not skip_depot:
                scope_parts.append("Depot tracking/invoicing")
            if not skip_lowes:
                scope_parts.append("Lowe's tracking/invoicing")
            scope_text = ", ".join(scope_parts) if scope_parts else "chain"
            cmd_full = _build_chain_cmd(
                python_exe,
                skip_inventory=skip_inventory,
                skip_depot=skip_depot,
                skip_lowes=skip_lowes,
                lowes_submit=lowes_submit,
            )
            ch_single_entry = (f"CommerceHub — one login: {scope_text}", cmd_full, INVENTORY_DIR)
        else:
            print("NOTE: CommerceHub selected but no CommerceHub actions enabled; skipping that step.")

    phase1_left: tuple[str, list[str], Path] | None = None
    phase1_right: list[tuple[str, list[str], Path]] = []
    phase2_left: tuple[str, list[str], Path] | None = None

    if split_ch:
        phase1_left = ch_inventory_entry
        if sps_inventory_entry:
            phase1_right.append(sps_inventory_entry)
        phase2_left = ch_tracking_entry
    elif ch_single_entry:
        dlv_only_single = wants_ch_dlv and not wants_ch_inv
        if sps_inventory_entry and dlv_only_single:
            phase1_right.append(sps_inventory_entry)
            phase2_left = ch_single_entry
        else:
            phase1_left = ch_single_entry
            if sps_inventory_entry:
                phase1_right.append(sps_inventory_entry)
    elif sps_inventory_entry:
        phase1_right.append(sps_inventory_entry)

    has_work = bool(
        phase1_left
        or phase1_right
        or phase2_left
        or sps_tracking_steps
        or (invoice_modes is not None and len(invoice_modes) > 0)
    )
    if not has_work:
        print(
            "ERROR: No steps to run (everything skipped). "
            "Omit some --skip-* flags or run without skipping all steps."
        )
        return 1

    required_names: list[str] = []
    if not skip_commercehub:
        required_names.append("run_commercehub_chain.py")
    if not skip_sps_inventory:
        required_names.append("run_sps.py")
    if run_sps_tracking and tracking_script.is_file():
        required_names.append("run_sps_tracking.py")
    if run_grainger_all and tracking_script.is_file():
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

    if invoice_modes:
        export_script = invoice_report_dir / "commercehub_invoice_export.py"
        if not export_script.is_file():
            msg = (
                f"Invoice report script not found ({export_script}); "
                "skipping phase 0. Use --invoice-report-dir or set COMMERCEHUB_INVOICE_REPORT_DIR."
            )
            print(f"\nWARN: {msg}")
            errors.append(msg)
        else:
            mode_labels = {
                "all": "All (Depot + Lowe's + Tractor Supply)",
                "depot": "Depot",
                "lowes": "Lowe's",
                "tractor": "Tractor Supply",
            }
            print(
                "\n"
                + "=" * 60
                + "\nPhase 0 — CommerceHub invoice reports\n"
                + "=" * 60
                + f"\n  Modes: {', '.join(invoice_modes)}\n"
            )
            inv_py_parts = resolve_invoice_report_python(invoice_report_dir)
            for mode in invoice_modes:
                label = mode_labels.get(mode, mode)
                cmd = inv_py_parts + [str(export_script), mode]
                title = f"CommerceHub invoice report — {label}"
                errors.extend(_run_single(title, cmd, invoice_report_dir))

    if phase1_left or phase1_right:
        errors.extend(
            _run_parallel_pair(
                phase1_left,
                phase1_right,
                phase_label="Phase 1 — Inventories",
                sequential=sequential_lanes,
            )
        )

    if phase2_left or sps_tracking_steps:
        errors.extend(
            _run_parallel_pair(
                phase2_left,
                sps_tracking_steps,
                phase_label="Phase 2 — Tracking / invoicing",
                sequential=sequential_lanes,
            )
        )

    if errors:
        print("\nCompleted with errors:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("\nAll workflow steps completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

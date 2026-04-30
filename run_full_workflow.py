"""

Orchestrates the full daily workflow:



1. CommerceHub (one Playwright login): Rithum inventory, Home Depot tracking/invoicing,

   Lowe's ship-to-store, ship-to-customer, invoicing — see Inventory Submissions\\run_commercehub_chain.py

2. SPS Commerce — Tractor Supply inventory (separate site; own login)
3. SPS Commerce — Tractor Supply tracking (runs right after SPS inventory)

Optional skips: --skip-commercehub, --skip-sps-inventory, --skip-sps-tracking.
Use --tracking-invoicing-only to skip Rithum/CommerceHub inventory and SPS inventory while still running
Depot/Lowe's tracking and invoicing in the CommerceHub chain, then SPS Tractor Supply tracking.
The chain script must accept --skip-inventory (see Inventory Submissions\\run_commercehub_chain.py).
Or run Run Full Workflow.bat with no arguments for a numbered menu.



Each step uses the Inventory Submissions\\.venv Python when present.

"""

from __future__ import annotations



import argparse

import os

import subprocess

import sys

from pathlib import Path



ROOT = Path(__file__).resolve().parent

INVENTORY_DIR = ROOT / "Inventory Submissions"

LOWES_DIR = ROOT / "Lowe's Tracking Automation"





def resolve_project_python() -> str:

    if sys.platform == "win32":

        candidate = INVENTORY_DIR / ".venv" / "Scripts" / "python.exe"

    else:

        candidate = INVENTORY_DIR / ".venv" / "bin" / "python"

    if candidate.is_file():

        return str(candidate)

    return sys.executable





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





def main() -> int:

    parser = argparse.ArgumentParser(

        description="Run CommerceHub chain (one login), then SPS (Tractor Supply)."

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
        "--skip-sps-inventory",
        action="store_true",
        help="Skip SPS Tractor Supply inventory (run SPS tracking only if CommerceHub is also skipped, or after CommerceHub).",
    )
    parser.add_argument(
        "--tracking-invoicing-only",
        action="store_true",
        help=(
            "Skip Rithum/CommerceHub inventory and SPS Tractor Supply inventory; run CommerceHub chain "
            "with --skip-inventory (Depot/Lowe's tracking + invoicing), then SPS tracking. "
            "Cannot be combined with --skip-commercehub."
        ),
    )

    args = parser.parse_args()

    if args.tracking_invoicing_only and args.skip_commercehub:
        parser.error("--tracking-invoicing-only cannot be combined with --skip-commercehub")

    tracking_invoicing_only = bool(args.tracking_invoicing_only)
    lowes_submit = not args.dry_run_lowes
    run_sps_tracking = not args.skip_sps_tracking
    skip_commercehub = bool(args.skip_commercehub)
    skip_sps_inventory = bool(args.skip_sps_inventory) or tracking_invoicing_only

    if tracking_invoicing_only:
        print(
            "Mode: tracking + invoicing only — SPS inventory skipped; CommerceHub chain runs with "
            "--skip-inventory (your run_commercehub_chain.py must honor that flag for Rithum inventory)."
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



    chain_cmd: list[str] = [

        python_exe,

        "run_commercehub_chain.py",

        "--lowes-config",

        str(LOWES_DIR / "config.example.json"),

    ]

    if lowes_submit:

        chain_cmd.append("--submit")

    if tracking_invoicing_only:
        chain_cmd.append("--skip-inventory")



    steps: list[tuple[str, list[str], Path]] = []

    if not skip_commercehub:
        commercehub_title = (
            "CommerceHub — one login: Depot, Lowe's tracking & invoicing (Rithum inventory skipped)"
            if tracking_invoicing_only
            else "CommerceHub — one login: inventory, Depot, Lowe's"
        )
        steps.append(
            (
                commercehub_title,
                chain_cmd,
                INVENTORY_DIR,
            )
        )

    if not skip_sps_inventory:
        steps.append(
            (
                "SPS Commerce — Tractor Supply inventory",
                [python_exe, "run_sps.py"],
                INVENTORY_DIR,
            )
        )

    tracking_script = INVENTORY_DIR / "run_sps_tracking.py"
    sps_storage_json = INVENTORY_DIR / "sps_playwright_storage.json"
    if run_sps_tracking and tracking_script.is_file():
        tracking_cmd: list[str] = [python_exe, "run_sps_tracking.py", "--submit"]
        need_interactive = not sps_storage_json.is_file() and os.environ.get(
            "SPS_TRACKING_NON_INTERACTIVE", ""
        ).strip().lower() not in ("1", "true", "yes", "y", "on")
        if need_interactive:
            tracking_cmd.append("--interactive-login")
            print(
                "\nNOTE: No sps_playwright_storage.json — tracking will pause once for SPS login in the browser, "
                "then save that session for future runs.\n"
                "      Set SPS_TRACKING_NON_INTERACTIVE=1 to skip this (automation must supply the file).\n"
            )
        steps.append(
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

    if not steps:
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

    missing_scripts = [str(INVENTORY_DIR / n) for n in required_names if not (INVENTORY_DIR / n).is_file()]

    if missing_scripts:

        print("\nERROR: Missing script(s) under Inventory Submissions:")

        for path in missing_scripts:

            print(f"  - {path}")

        print(

            "\nThis folder must contain the same Python files as your main PC (including run_commercehub_chain.py).\n"

            "Typical fixes from cmd.exe:\n"

            f'  cd /d "{ROOT}"\n'

            "  git submodule update --init --recursive\n"

            "\nIf you do not use submodules, either copy the full \"Inventory Submissions\" folder from your main PC,\n"

            "or clone your Inventory-Automation repo into:\n"

            f'  "{INVENTORY_DIR}"\n'

            "then run git pull there."

        )

        return 1



    errors: list[str] = []

    for title, cmd, cwd in steps:

        ok, err_detail = run_step(title, cmd, cwd)

        if not ok:

            msg = f"{title}: {err_detail}"

            print(f"[ERROR] {msg}")

            errors.append(msg)



    if errors:

        print("\nCompleted with errors:")

        for e in errors:

            print(f"  - {e}")

        return 1

    print("\nAll workflow steps completed successfully.")

    return 0





if __name__ == "__main__":

    sys.exit(main())



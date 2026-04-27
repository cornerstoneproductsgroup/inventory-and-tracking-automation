"""

Orchestrates the full daily workflow:



1. CommerceHub (one Playwright login): Rithum inventory, Home Depot tracking/invoicing,

   Lowe's ship-to-store, ship-to-customer, invoicing — see Inventory Submissions\\run_commercehub_chain.py

2. SPS Commerce — Tractor Supply inventory (separate site; own login)



Each step uses the Inventory Submissions\\.venv Python when present.

"""

from __future__ import annotations



import argparse

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

    args = parser.parse_args()

    lowes_submit = not args.dry_run_lowes



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



    steps: list[tuple[str, list[str], Path]] = [

        (

            "CommerceHub — one login: inventory, Depot, Lowe's",

            chain_cmd,

            INVENTORY_DIR,

        ),

        (

            "SPS Commerce — Tractor Supply inventory",

            [python_exe, "run_sps.py"],

            INVENTORY_DIR,

        ),

    ]



    required_scripts = ("run_commercehub_chain.py", "run_sps.py")

    missing_scripts = [str(INVENTORY_DIR / n) for n in required_scripts if not (INVENTORY_DIR / n).is_file()]

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



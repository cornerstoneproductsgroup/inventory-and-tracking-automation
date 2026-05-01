"""CLI entry for automation.commercehub_chain (cwd must be Inventory Submissions)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


if __name__ == "__main__":
    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    from automation.commercehub_chain import main

    raise SystemExit(main())

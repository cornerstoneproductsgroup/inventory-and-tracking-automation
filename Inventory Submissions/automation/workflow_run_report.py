"""
Append-only run report (skips, errors, warnings) for full-workflow and lane scripts.

Set ``WORKFLOW_RUN_REPORT_FILE`` to a JSONL path before starting a run; child processes
inherit the variable and append to the same file.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_KINDS = frozenset({"skip", "error", "warn"})


def report_file_path() -> Path | None:
    raw = (os.environ.get("WORKFLOW_RUN_REPORT_FILE") or "").strip()
    if not raw:
        return None
    return Path(raw)


def init_run_report(path: Path | str | None = None) -> Path | None:
    """Create/clear the report file and set ``WORKFLOW_RUN_REPORT_FILE``."""
    p = Path(path) if path is not None else report_file_path()
    if p is None:
        return None
    p = p.expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")
    os.environ["WORKFLOW_RUN_REPORT_FILE"] = str(p)
    return p


def _append(kind: str, step: str, detail: str) -> None:
    if kind not in _KINDS:
        raise ValueError(f"invalid report kind: {kind!r}")
    path = report_file_path()
    if path is None:
        return
    step = (step or "").strip() or "Unknown step"
    detail = (detail or "").strip() or "(no detail)"
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "step": step,
        "detail": detail,
    }
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)


def record_skip(step: str, reason: str) -> None:
    _append("skip", step, reason)


def record_error(step: str, message: str) -> None:
    _append("error", step, message)


def record_warn(step: str, message: str) -> None:
    _append("warn", step, message)


def log_and_record_skip(step: str, reason: str) -> None:
    record_skip(step, reason)
    print(f"{step}: Skipped — {reason}", flush=True)


def read_report_entries() -> list[dict[str, Any]]:
    path = report_file_path()
    if path is None or not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (
            str(row.get("kind") or ""),
            str(row.get("step") or ""),
            str(row.get("detail") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def print_final_summary(
    *,
    extra_errors: list[str] | None = None,
    extra_skips: list[tuple[str, str]] | None = None,
    extra_warnings: list[str] | None = None,
    success: bool | None = None,
) -> None:
    """Print end-of-run summary (skipped steps, errors, warnings)."""
    skips: list[tuple[str, str]] = []
    errors: list[str] = []
    warnings: list[str] = []

    for row in _dedupe_rows(read_report_entries()):
        kind = row.get("kind")
        step = str(row.get("step") or "Unknown step")
        detail = str(row.get("detail") or "")
        if kind == "skip":
            skips.append((step, detail))
        elif kind == "error":
            errors.append(f"{step}: {detail}" if detail else step)
        elif kind == "warn":
            warnings.append(f"{step}: {detail}" if detail else step)

    if extra_skips:
        for step, reason in extra_skips:
            skips.append((step.strip(), reason.strip()))

    if extra_errors:
        for msg in extra_errors:
            msg = (msg or "").strip()
            if msg and msg not in errors:
                errors.append(msg)

    if extra_warnings:
        for msg in extra_warnings:
            msg = (msg or "").strip()
            if msg and msg not in warnings:
                warnings.append(msg)

    # Dedupe display lists
    skip_seen: set[tuple[str, str]] = set()
    skip_lines: list[str] = []
    for step, reason in skips:
        key = (step, reason)
        if key in skip_seen:
            continue
        skip_seen.add(key)
        skip_lines.append(f"  {step} — {reason}")

    err_seen: set[str] = set()
    err_lines: list[str] = []
    for e in errors:
        if e in err_seen:
            continue
        err_seen.add(e)
        err_lines.append(f"  {e}")

    warn_seen: set[str] = set()
    warn_lines: list[str] = []
    for w in warnings:
        if w in warn_seen:
            continue
        warn_seen.add(w)
        warn_lines.append(f"  {w}")

    bar = "=" * 60
    print(f"\n{bar}\nWORKFLOW RUN SUMMARY\n{bar}")

    print("\nSkipped steps:")
    if skip_lines:
        print("\n".join(skip_lines))
    else:
        print("  (none)")

    print("\nErrors:")
    if err_lines:
        print("\n".join(err_lines))
    else:
        print("  (none)")

    print("\nWarnings:")
    if warn_lines:
        print("\n".join(warn_lines))
    else:
        print("  (none)")

    if success is True and not err_lines:
        print("\nOverall: completed successfully.")
    elif success is False or err_lines:
        print("\nOverall: completed with errors.")
    else:
        print("\nOverall: finished (see details above).")

    print(bar)

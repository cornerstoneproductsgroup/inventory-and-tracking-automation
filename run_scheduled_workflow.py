"""
Run the morning (or custom) scheduled workflow from scheduled_workflow.json.

Each step is a subprocess (usually run_full_workflow.py with different flags).
Add or reorder steps in scheduled_workflow.json; set enabled=false to skip one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "scheduled_workflow.json"


def _log(msg: str, *, log_fp) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    if log_fp is not None:
        log_fp.write(line + "\n")
        log_fp.flush()


def _load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a JSON object: {path}")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"Config must include a non-empty steps[] array: {path}")
    return data


def _resolve_log_path(cfg: dict, cli_log: Path | None) -> Path:
    if cli_log is not None:
        return cli_log.expanduser().resolve()
    settings = cfg.get("settings") if isinstance(cfg.get("settings"), dict) else {}
    log_dir = (settings.get("log_dir") or "logs").strip() or "logs"
    basename = (settings.get("log_basename") or "scheduled_workflow.log").strip() or "scheduled_workflow.log"
    return (ROOT / log_dir / basename).resolve()


def _schedule_time(cfg: dict) -> str:
    env_raw = (os.environ.get("SCHEDULED_WORKFLOW_TIME") or "").strip()
    if env_raw:
        return env_raw
    schedule = cfg.get("schedule") if isinstance(cfg.get("schedule"), dict) else {}
    return (schedule.get("time_local") or "05:00").strip() or "05:00"


def _task_name(cfg: dict) -> str:
    schedule = cfg.get("schedule") if isinstance(cfg.get("schedule"), dict) else {}
    return (schedule.get("task_name") or "Cornerstone Morning Automation").strip()


def _enabled_steps(cfg: dict, only_ids: set[str] | None) -> list[dict]:
    out: list[dict] = []
    for raw in cfg.get("steps", []):
        if not isinstance(raw, dict):
            continue
        step_id = (raw.get("id") or "").strip()
        if not step_id:
            continue
        if only_ids is not None and step_id not in only_ids:
            continue
        if not raw.get("enabled", True):
            continue
        out.append(raw)
    return out


def _build_command(step: dict, python_exe: str) -> tuple[list[str], Path]:
    step_type = (step.get("type") or "workflow").strip().lower()
    args = step.get("args") or []
    if not isinstance(args, list):
        raise ValueError(f"Step {step.get('id')!r}: args must be a list")

    if step_type == "workflow":
        script = ROOT / "run_full_workflow.py"
        if not script.is_file():
            raise FileNotFoundError(f"Missing workflow runner: {script}")
        return [python_exe, str(script), *[str(a) for a in args]], ROOT

    if step_type == "script":
        rel = (step.get("script") or "").strip()
        if not rel:
            raise ValueError(f"Step {step.get('id')!r}: script path is required for type=script")
        script = (ROOT / rel).resolve()
        if not script.is_file():
            raise FileNotFoundError(f"Missing script for step {step.get('id')!r}: {script}")
        cwd_raw = (step.get("cwd") or rel).strip()
        cwd = (ROOT / cwd_raw).resolve() if cwd_raw else script.parent
        return [python_exe, str(script), *[str(a) for a in args]], cwd

    raise ValueError(f"Step {step.get('id')!r}: unknown type {step_type!r} (use workflow or script)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run scheduled workflow steps from scheduled_workflow.json (extensible step list)."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("SCHEDULED_WORKFLOW_CONFIG") or DEFAULT_CONFIG),
        help=f"Step list JSON (default: {DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Append run output to this log file (default: logs/scheduled_workflow.log)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print enabled steps and exit without running.",
    )
    parser.add_argument(
        "--only",
        metavar="ID",
        nargs="+",
        default=None,
        help="Run only these step id values from the config (e.g. pull_orders fedex_batch).",
    )
    parser.add_argument(
        "--show-schedule",
        action="store_true",
        help="Print configured task time/name and exit.",
    )
    args = parser.parse_args()

    config_path = args.config.expanduser()
    if not config_path.is_file():
        print(f"ERROR: Config not found: {config_path}", file=sys.stderr)
        return 1

    try:
        cfg = _load_config(config_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.show_schedule:
        print(f"Task name: {_task_name(cfg)}")
        print(f"Time (local): {_schedule_time(cfg)}")
        print(f"Config: {config_path.resolve()}")
        return 0

    only_ids = {s.strip() for s in args.only} if args.only else None
    steps = _enabled_steps(cfg, only_ids)
    if not steps:
        print("ERROR: No enabled steps to run.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"Config: {config_path.resolve()}")
        print(f"Scheduled time (local): {_schedule_time(cfg)}")
        print("Enabled steps:")
        for i, step in enumerate(steps, 1):
            cont = "continue on error" if step.get("continue_on_error", False) else "stop on error"
            print(f"  {i}. [{step.get('id')}] {step.get('label') or step.get('id')} ({cont})")
            try:
                cmd, cwd = _build_command(step, "python")
                print(f"       cwd={cwd}")
                print(f"       cmd={' '.join(cmd)}")
            except (ValueError, FileNotFoundError) as exc:
                print(f"       ERROR building command: {exc}")
        return 0

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from run_full_workflow import resolve_project_python, run_step

    log_path = _resolve_log_path(cfg, args.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    python_exe = resolve_project_python()
    errors: list[str] = []

    with log_path.open("a", encoding="utf-8") as log_fp:
        _log("=" * 60, log_fp=log_fp)
        _log(f"Scheduled workflow start — config {config_path.name}", log_fp=log_fp)
        _log(f"Python: {python_exe}", log_fp=log_fp)
        _log(f"Steps: {len(steps)}", log_fp=log_fp)

        for index, step in enumerate(steps, 1):
            step_id = step.get("id") or f"step_{index}"
            label = (step.get("label") or step_id).strip()
            continue_on_error = bool(step.get("continue_on_error", False))
            _log(f"Step {index}/{len(steps)} — {label}", log_fp=log_fp)

            try:
                cmd, cwd = _build_command(step, python_exe)
            except (ValueError, FileNotFoundError) as exc:
                msg = f"{step_id}: {exc}"
                errors.append(msg)
                _log(f"ERROR: {msg}", log_fp=log_fp)
                if not continue_on_error:
                    _log("Stopping — continue_on_error is false.", log_fp=log_fp)
                    break
                continue

            ok, err_detail = run_step(label, cmd, cwd)
            if ok:
                _log(f"OK: {label}", log_fp=log_fp)
                continue

            msg = f"{label}: {err_detail}"
            errors.append(msg)
            _log(f"ERROR: {msg}", log_fp=log_fp)
            if not continue_on_error:
                _log("Stopping — continue_on_error is false.", log_fp=log_fp)
                break

        if errors:
            _log(f"Finished with {len(errors)} error(s).", log_fp=log_fp)
            for e in errors:
                _log(f"  - {e}", log_fp=log_fp)
            _log("=" * 60, log_fp=log_fp)
            return 1

        _log("All scheduled steps completed successfully.", log_fp=log_fp)
        _log("=" * 60, log_fp=log_fp)
        return 0


if __name__ == "__main__":
    sys.exit(main())

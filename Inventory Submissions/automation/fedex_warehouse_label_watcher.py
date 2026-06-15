"""Watch a folder for new FedEx warehouse label PDFs and print them on the Zebra."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from automation.fedex_batch_config import resolve_fedex_warehouse_label_printer
from automation.pull_orders_warehouse_print import print_pdf_windows


def _log(msg: str) -> None:
    print(f"[fedex/warehouse-watch] {msg}", flush=True)


def _poll_interval_s() -> float:
    raw = (os.environ.get("FEDEX_WAREHOUSE_WATCH_POLL_S") or "2").strip()
    try:
        return max(0.5, float(raw))
    except ValueError:
        return 2.0


def _file_stable(path: Path, *, settle_s: float = 1.5) -> bool:
    if not path.is_file():
        return False
    try:
        size_a = path.stat().st_size
        if size_a < 200:
            return False
        time.sleep(settle_s)
        size_b = path.stat().st_size
        return size_a == size_b
    except OSError:
        return False


def _printed_subdir(queue_dir: Path) -> Path:
    return queue_dir / "_printed"


class FedexWarehouseLabelWatcher:
    """
    Background thread: when a new PDF appears in ``queue_dir``, print it and move
    to ``_printed/`` so FedEx label saving and Zebra printing are separate steps.
    """

    def __init__(self, queue_dir: Path, printer: str, *, printer_source: str = "") -> None:
        self.queue_dir = queue_dir.resolve()
        self.printer = printer
        self.printer_source = printer_source or "configured"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._in_progress: set[str] = set()
        self._printed_count = 0
        self._errors: list[str] = []

    @classmethod
    def for_queue_dir(cls, queue_dir: Path) -> FedexWarehouseLabelWatcher:
        """Start a watcher that prints queue PDFs on the Zebra from .env."""
        printer, source = resolve_fedex_warehouse_label_printer()
        return cls(queue_dir, printer, printer_source=source)

    def start(self) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        _printed_subdir(self.queue_dir).mkdir(parents=True, exist_ok=True)
        _log(
            f"Watching {self.queue_dir} → Zebra {self.printer!r} "
            f"(from {self.printer_source})"
        )
        self._thread = threading.Thread(target=self._run, name="fedex-warehouse-label-watcher", daemon=True)
        self._thread.start()

    def _pending_pdfs(self) -> list[Path]:
        printed_root = _printed_subdir(self.queue_dir)
        out: list[Path] = []
        if not self.queue_dir.is_dir():
            return out
        for path in sorted(self.queue_dir.rglob("*.pdf")):
            try:
                if printed_root in path.parents:
                    continue
            except ValueError:
                pass
            if path.name.startswith("."):
                continue
            out.append(path)
        return out

    def _archive_printed(self, path: Path) -> None:
        rel = path.relative_to(self.queue_dir)
        dest = _printed_subdir(self.queue_dir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        shutil.move(str(path), str(dest))

    def _print_one(self, path: Path) -> None:
        key = str(path.resolve())
        with self._lock:
            if key in self._in_progress:
                return
            self._in_progress.add(key)
        try:
            if not _file_stable(path):
                return
            _log(
                f"Printing {path.name} on Zebra {self.printer!r} "
                f"({self.printer_source})…"
            )
            print_pdf_windows(path, self.printer)
            self._archive_printed(path)
            with self._lock:
                self._printed_count += 1
            _log(f"Sent {path.name} to Zebra {self.printer!r}.")
        except Exception as exc:
            msg = f"{path.name}: {exc}"
            with self._lock:
                self._errors.append(msg)
            _log(f"ERROR: warehouse print failed — {msg}")
        finally:
            with self._lock:
                self._in_progress.discard(key)

    def _run(self) -> None:
        poll = _poll_interval_s()
        while not self._stop.is_set():
            for pdf in self._pending_pdfs():
                if self._stop.is_set():
                    break
                self._print_one(pdf)
            self._stop.wait(poll)

    def stop_and_drain(self, *, timeout_s: float) -> int:
        """Stop the watcher thread and print any PDFs still in the queue."""
        _log(f"Draining warehouse print queue (up to {timeout_s:.0f}s)…")
        deadline = time.monotonic() + max(30.0, timeout_s)
        while time.monotonic() < deadline:
            pending = self._pending_pdfs()
            if pending:
                for pdf in pending:
                    self._print_one(pdf)
            else:
                with self._lock:
                    busy = bool(self._in_progress)
                if not busy:
                    break
            time.sleep(_poll_interval_s())
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15.0)

        remaining = self._pending_pdfs()
        if remaining:
            names = ", ".join(p.name for p in remaining[:8])
            _log(f"WARN: {len(remaining)} label(s) still in queue after drain: {names}")

        with self._lock:
            count = self._printed_count
            errs = list(self._errors)
        if errs:
            _log(f"Warehouse watcher had {len(errs)} print error(s).")
        _log(f"Warehouse watcher finished — printed {count} label PDF(s).")
        return count


def warehouse_label_print_mode() -> str:
    from automation.fedex_batch_config import warehouse_label_print_mode as _mode

    return _mode()


def drain_timeout_s() -> float:
    raw = (os.environ.get("FEDEX_WAREHOUSE_WATCH_DRAIN_TIMEOUT_S") or "600").strip()
    try:
        return max(60.0, float(raw))
    except ValueError:
        return 600.0

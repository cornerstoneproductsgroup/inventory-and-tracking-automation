"""Run async coroutines from sync Playwright chains (which already own an event loop)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """
    Complete *coro* even when the caller thread already has a running loop.

    sync_playwright() keeps an asyncio loop active on the main thread, so
    asyncio.run() inside commercehub_chain / run_sps_lane fails with
    "cannot be called from a running event loop". Option R works because it
    runs commercehub_invoice_export.py in a separate process.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    err: list[BaseException] = []
    result: list[T] = []
    done = threading.Event()

    def _worker() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:
            err.append(exc)
        finally:
            done.set()

    threading.Thread(target=_worker, name="async-bridge", daemon=True).start()
    done.wait()
    if err:
        raise err[0]
    return result[0]

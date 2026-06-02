"""
Detect CommerceHub / Rithum empty order queues across Depot, Lowe's, and Special Orders.

Authoritative signal: Transaction Request Notification widget
  div.fw_widget_windowtag_body — "No order(s) found that match the supplied criteria."

Do not treat summary/dashboard pages (no form fields yet) as empty — those are not empty queues.
"""

from __future__ import annotations

import re
import time
from typing import Literal

from playwright.sync_api import Frame, Page

NO_ORDERS_CRITERIA_RE = re.compile(
    r"no\s+orders?\s*\(?s?\)?\s*found.*(?:supplied\s+criteria|criteria)",
    re.I,
)


def rithum_no_orders_criteria(root: Page | Frame) -> bool:
    """Transaction Request Notification widget on empty quickship/quickinvoice/quickack pages."""
    try:
        widget = root.locator("div.fw_widget_windowtag_body")
        if widget.count() > 0:
            txt = (widget.first.inner_text(timeout=800) or "").strip().lower()
            if txt and "no order" in txt and "found" in txt and "criteria" in txt:
                return True
    except Exception:
        pass
    try:
        hit = root.get_by_text(NO_ORDERS_CRITERIA_RE)
        if hit.count() > 0 and hit.first.is_visible():
            return True
    except Exception:
        pass
    return False


def rithum_criteria_empty(page: Page) -> bool:
    """True only when Rithum shows the explicit no-orders criteria notification."""
    for root in (page, *page.frames):
        try:
            if hasattr(root, "is_detached") and root.is_detached():
                continue
        except Exception:
            pass
        if rithum_no_orders_criteria(root):
            return True

    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    if not body:
        return False
    if "no order(s) found" in body and "supplied criteria" in body:
        return True
    return bool(NO_ORDERS_CRITERIA_RE.search(body))


def rithum_empty_queue(page: Page) -> bool:
    """Alias for criteria-only empty detection (avoids false skips on summary pages)."""
    return rithum_criteria_empty(page)


def log_rithum_empty_skip(step: str) -> None:
    reason = "No orders found"
    try:
        from automation.workflow_run_report import log_and_record_skip

        log_and_record_skip(step, reason)
    except ImportError:
        print(f"{step}: Skipped — {reason}", flush=True)


def skip_if_rithum_empty(page: Page, step: str) -> bool:
    """Return True when the step should be skipped because the queue is empty."""
    if rithum_criteria_empty(page):
        log_rithum_empty_skip(step)
        return True
    return False


WaitOutcome = Literal["ready", "empty", "timeout"]


def wait_for_selector_or_empty(
    page: Page,
    selector: str,
    *,
    step: str,
    timeout_ms: int,
    poll_ms: int = 400,
) -> WaitOutcome:
    """
    Poll until ``selector`` matches at least one element, Rithum shows no orders, or timeout.
    """
    deadline = time.monotonic() + max(500, timeout_ms) / 1000.0
    loc = page.locator(selector)
    while time.monotonic() < deadline:
        if rithum_criteria_empty(page):
            log_rithum_empty_skip(step)
            return "empty"
        try:
            if loc.count() > 0:
                return "ready"
        except Exception:
            pass
        page.wait_for_timeout(poll_ms)
    if rithum_criteria_empty(page):
        log_rithum_empty_skip(step)
        return "empty"
    return "timeout"

"""Shared CommerceHub / Rithum wait limits — poll until ready, with higher ceilings for slow days."""

from __future__ import annotations

import os


def chain_fast() -> bool:
    return os.environ.get("COMMERCEHUB_CHAIN_FAST") == "1"


def ms(env_key: str, default: int) -> int:
    raw = (os.environ.get(env_key) or "").strip()
    if raw:
        try:
            return max(1000, int(raw))
        except ValueError:
            pass
    return default


def default_page_timeout_ms() -> int:
    """Playwright default timeout for CommerceHub chain browser context."""
    return ms("COMMERCEHUB_DEFAULT_TIMEOUT_MS", 120_000)


def navigation_timeout_ms() -> int:
    """page.goto / domcontentloaded for DSM order realms."""
    return ms("COMMERCEHUB_NAVIGATION_TIMEOUT_MS", 180_000)


def depot_ship_list_timeout_ms() -> int:
    """Poll until tracking / special-order ship list is ready (exits early when loaded)."""
    default = 45_000 if chain_fast() else 300_000
    return ms("DEPOT_SHIP_LIST_TIMEOUT_MS", default)


def depot_queue_probe_timeout_ms() -> int:
    """First poll on quickship/quickinvoice before summary fallback (still exits early when ready)."""
    default = 12_000 if chain_fast() else 60_000
    return ms("DEPOT_QUEUE_PROBE_TIMEOUT_MS", default)

def depot_invoice_ready_timeout_ms() -> int:
    """Poll until quickinvoice form is ready (exits early when loaded)."""
    return ms("DEPOT_INVOICE_READY_TIMEOUT_MS", 240_000)


def rithum_ibl_timeout_ms() -> int:
    """Inventory update (IBL) form controls."""
    return ms("COMMERCEHUB_IBL_TIMEOUT_MS", 90_000)


def rithum_profile_timeout_ms() -> int:
    """Profile chooser after login."""
    return ms("COMMERCEHUB_PROFILE_TIMEOUT_MS", 30_000)


def commercehub_login_probe_timeout_ms() -> int:
    """Each selector try while confirming post-login shell."""
    return ms("COMMERCEHUB_LOGIN_PROBE_TIMEOUT_MS", 20_000)


def lowes_order_links_timeout_ms() -> int:
    return ms("LOWES_ORDER_LINKS_TIMEOUT_MS", 60_000)


def lowes_po_row_timeout_ms() -> int:
    return ms("LOWES_PO_ROW_TIMEOUT_MS", 45_000)


def lowes_invoice_select_timeout_ms() -> int:
    return ms("LOWES_INVOICE_SELECT_TIMEOUT_MS", 90_000)


def poll_interval_ms() -> int:
    """Sleep between readiness polls (short — does not add to max wait if UI is ready)."""
    return ms("COMMERCEHUB_POLL_INTERVAL_MS", 400 if chain_fast() else 600)

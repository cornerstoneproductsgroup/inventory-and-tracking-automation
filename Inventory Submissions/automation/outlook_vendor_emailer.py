"""Send daily vendor-order emails from z- Daily Vendor Orders via Outlook."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any


def _log(msg: str) -> None:
    print(f"[vendor-email] {msg}", flush=True)


class VendorEmailError(Exception):
    pass


@dataclass(frozen=True)
class VendorEmailEntry:
    vendor_folder: str
    to: str
    cc: str
    subject: str
    body: str


@dataclass(frozen=True)
class VendorEmailConfig:
    daily_vendor_root: Path
    skip_root_entries: tuple[str, ...]
    skip_file_prefixes: tuple[str, ...]
    skip_subfolders_inside_vendor: tuple[str, ...]
    append_run_date_to_subject: bool
    outlook_signature_name: str
    signature_image_path: Path | None
    vendors: tuple[VendorEmailEntry, ...]


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _normalize_recipient_field(value: Any) -> str:
    """Accept a string or list of emails/names; return one Outlook-style field."""
    if isinstance(value, list):
        parts = [_as_str(x) for x in value if _as_str(x)]
        return ", ".join(parts)
    return _as_str(value)


def load_vendor_email_config(config_path: Path) -> VendorEmailConfig:
    if not config_path.is_file():
        raise VendorEmailError(f"Config not found: {config_path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VendorEmailError(f"Invalid JSON in {config_path}: {exc}") from exc

    root = Path(_as_str(raw.get("daily_vendor_root")))
    if not root:
        raise VendorEmailError("vendor_email_config.json is missing daily_vendor_root.")

    skip_root = tuple(_as_str(x) for x in (raw.get("skip_root_entries") or []) if _as_str(x))
    skip_prefixes = tuple(_as_str(x) for x in (raw.get("skip_file_prefixes") or []) if _as_str(x))
    skip_subfolders = tuple(
        _as_str(x) for x in (raw.get("skip_subfolders_inside_vendor") or []) if _as_str(x)
    )
    append_run_date_to_subject = bool(raw.get("append_run_date_to_subject", True))
    signature_name = _as_str(raw.get("outlook_signature_name"))
    sig_raw = _as_str(raw.get("signature_image_path"))
    signature_image_path = Path(sig_raw) if sig_raw else None

    vendors_raw = raw.get("vendors")
    if not isinstance(vendors_raw, list) or not vendors_raw:
        raise VendorEmailError("vendor_email_config.json must contain a non-empty vendors list.")

    vendors: list[VendorEmailEntry] = []
    for i, item in enumerate(vendors_raw, start=1):
        if not isinstance(item, dict):
            raise VendorEmailError(f"vendors[{i}] is not an object.")
        vendor = _as_str(item.get("vendor_folder"))
        to = _normalize_recipient_field(item.get("to"))
        cc = _normalize_recipient_field(item.get("cc"))
        subject = _as_str(item.get("subject"))
        body = str(item.get("body") or "")
        if not vendor:
            raise VendorEmailError(f"vendors[{i}] missing vendor_folder.")
        vendors.append(VendorEmailEntry(vendor_folder=vendor, to=to, cc=cc, subject=subject, body=body))

    return VendorEmailConfig(
        daily_vendor_root=root,
        skip_root_entries=skip_root,
        skip_file_prefixes=skip_prefixes,
        skip_subfolders_inside_vendor=skip_subfolders,
        append_run_date_to_subject=append_run_date_to_subject,
        outlook_signature_name=signature_name,
        signature_image_path=signature_image_path,
        vendors=tuple(vendors),
    )


def _ensure_outlook_app():
    try:
        import win32com.client  # type: ignore[import-not-found]
    except Exception as exc:
        raise VendorEmailError(
            "pywin32 is required for Outlook automation. Install dependencies on this PC."
        ) from exc

    app = None
    try:
        app = win32com.client.GetActiveObject("Outlook.Application")
        _log("Outlook is already open; reusing existing session.")
    except Exception:
        pass

    if app is None:
        app = win32com.client.Dispatch("Outlook.Application")
        _log("Outlook was not open; launched Outlook session.")

    try:
        ns = app.GetNamespace("MAPI")
        ns.Logon("", "", False, False)
    except Exception:
        pass
    return app


# Outlook OlMailRecipientType
_OL_TO = 1
_OL_CC = 2


def _split_recipient_field(value: str) -> list[str]:
    """Split To/CC field on ; or , (same as Outlook)."""
    out: list[str] = []
    for chunk in re.split(r"[;,]", value or ""):
        name = chunk.strip()
        if name:
            out.append(name)
    return out


def _looks_like_smtp_address(value: str) -> bool:
    return "@" in value and "." in value.split("@", 1)[-1]


def _name_matches(a: str, b: str) -> bool:
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def _search_address_entries(entries, label: str, *, depth: int = 0) -> object | None:
    """Search an AddressEntries collection (and nested containers) for a display name."""
    if depth > 6:
        return None
    target = label.casefold()
    try:
        count = int(entries.Count)
    except Exception:
        return None
    for i in range(1, count + 1):
        try:
            entry = entries.Item(i)
            entry_name = str(getattr(entry, "Name", "") or "").strip()
            if entry_name.casefold() == target:
                return entry
            nested = getattr(entry, "AddressEntries", None)
            if nested is not None:
                try:
                    if int(nested.Count) > 0:
                        hit = _search_address_entries(nested, label, depth=depth + 1)
                        if hit is not None:
                            return hit
                except Exception:
                    pass
        except Exception:
            continue
    return None


def _find_in_contacts_folder(namespace, label: str) -> object | None:
    """Find a contact or contact group in the default Contacts folder (My Contacts)."""
    try:
        folder = namespace.GetDefaultFolder(10)  # olFolderContacts
        items = folder.Items
    except Exception:
        return None

    safe = label.replace("'", "''")
    found = None
    try:
        found = items.Find(f"[FullName] = '{safe}'")
        if found is not None and not _name_matches(
            str(getattr(found, "FullName", "") or ""), label
        ):
            found = None
    except Exception:
        found = None

    if found is None:
        try:
            found = items.Find(f"[FileAs] = '{safe}'")
            if found is not None and not _name_matches(
                str(getattr(found, "FileAs", "") or getattr(found, "FullName", "") or ""),
                label,
            ):
                found = None
        except Exception:
            found = None

    if found is None:
        try:
            for item in items:
                try:
                    full = str(
                        getattr(item, "FullName", "") or getattr(item, "FileAs", "") or ""
                    ).strip()
                    if _name_matches(full, label):
                        return item
                except Exception:
                    continue
        except Exception:
            pass
        return None

    return found


def _find_address_entry(namespace, name: str) -> object | None:
    """
    Find Ez Pole-style entries in any Outlook address source:
    GAL, other address lists, and My Contacts contact groups.
    """
    label = (name or "").strip()
    if not label:
        return None
    if _looks_like_smtp_address(label):
        return None

    try:
        recip = namespace.CreateRecipient(label)
        if recip.Resolve():
            try:
                return recip.AddressEntry
            except Exception:
                pass
    except Exception:
        pass

    try:
        lists = namespace.AddressLists
        for li in range(1, int(lists.Count) + 1):
            try:
                addr_list = lists.Item(li)
                hit = _search_address_entries(addr_list.AddressEntries, label)
                if hit is not None:
                    return hit
            except Exception:
                continue
    except Exception:
        pass

    try:
        gal_list = namespace.GetGlobalAddressList()
        if gal_list is not None:
            hit = _search_address_entries(gal_list.AddressEntries, label)
            if hit is not None:
                return hit
    except Exception:
        pass

    contact = _find_in_contacts_folder(namespace, label)
    if contact is not None:
        try:
            return contact.AddressEntry
        except Exception:
            return contact
    return None


def _gal_resolves(namespace, name: str) -> bool:
    """True when the name exists in any Outlook address list or Contacts folder."""
    label = (name or "").strip()
    if not label:
        return False
    if _looks_like_smtp_address(label):
        return True
    return _find_address_entry(namespace, label) is not None


def _find_canonical_gal_name(namespace, name: str) -> str | None:
    """Return the display name Outlook uses for this contact / group."""
    label = (name or "").strip()
    if not label:
        return None
    hit = _find_address_entry(namespace, label)
    if hit is None:
        return None
    try:
        canon = str(getattr(hit, "Name", "") or "").strip()
        return canon or label
    except Exception:
        return label


def _add_recipient_token(
    mail, namespace, token: str, rtype: int
) -> tuple[bool, str | None]:
    """
    Add one To/Cc entry. Returns (ok, source) where source is 'entry', 'smtp', or None.
    """
    label = token.strip()
    if not label:
        return True, None

    if _looks_like_smtp_address(label):
        recip = mail.Recipients.Add(label)
        recip.Type = rtype
        try:
            recip.Resolve()
        except Exception:
            pass
        return _recipient_is_resolved(recip, namespace), "smtp"

    hit = _find_address_entry(namespace, label)
    if hit is not None:
        try:
            recip = mail.Recipients.Add(hit)
            recip.Type = rtype
            try:
                recip.Resolve()
            except Exception:
                pass
            return True, "entry"
        except Exception as exc:
            _log(f"  WARN: address book entry found for {label!r} but Add failed: {exc}")

    recip = mail.Recipients.Add(label)
    recip.Type = rtype
    try:
        recip.Resolve()
    except Exception:
        pass
    if _recipient_is_resolved(recip, namespace):
        return True, "string"
    return False, None


def _recipient_is_resolved(recip, namespace=None) -> bool:
    try:
        if bool(getattr(recip, "Resolved", False)):
            return True
    except Exception:
        pass
    try:
        addr = str(getattr(recip, "Address", "") or "").strip()
        if addr and (
            _looks_like_smtp_address(addr)
            or addr.startswith("/o=")
            or addr.startswith("EX")
        ):
            return True
    except Exception:
        pass
    if namespace is not None:
        name = str(getattr(recip, "Name", "") or "").strip()
        if name and _gal_resolves(namespace, name):
            return True
    return False


def _unresolved_recipient_names(mail, namespace) -> list[str]:
    bad: list[str] = []
    try:
        for recip in mail.Recipients:
            name = str(getattr(recip, "Name", "") or "").strip()
            if _recipient_is_resolved(recip, namespace):
                continue
            try:
                recip.Resolve()
            except Exception:
                pass
            if _recipient_is_resolved(recip, namespace):
                continue
            bad.append(name or "?")
    except Exception:
        pass
    return bad


def _dedupe_mail_recipients(mail) -> int:
    """Remove duplicate To/Cc entries (same name + type). Returns count removed."""
    removed = 0
    seen: set[tuple[str, int]] = set()
    try:
        count = int(mail.Recipients.Count)
    except Exception:
        return 0
    for idx in range(count, 0, -1):
        try:
            recip = mail.Recipients.Item(idx)
            key = (str(getattr(recip, "Name", "") or "").casefold(), int(recip.Type))
            if key in seen:
                mail.Recipients.Remove(idx)
                removed += 1
            else:
                seen.add(key)
        except Exception:
            continue
    if removed:
        _log(f"Removed {removed} duplicate recipient(s).")
    return removed


def _apply_mail_recipients(
    mail,
    namespace,
    *,
    to: str,
    cc: str,
) -> list[str]:
    """
    Add To/Cc from SMTP, GAL, other address lists, or My Contacts contact groups.

    Contact groups (e.g. Ez Pole) often live under Contacts, not the Global Address List.
    """
    unresolved: list[str] = []

    for token in _split_recipient_field(to):
        ok, source = _add_recipient_token(mail, namespace, token, _OL_TO)
        if ok:
            if source == "entry":
                _log(f"  To: added {token!r} from address book / Contacts")
            elif source == "smtp":
                _log(f"  To: added {token!r} (email)")
            continue
        if _gal_resolves(namespace, token):
            if _rebuild_named_recipient(mail, namespace, token, _OL_TO):
                _log(f"  To: re-linked {token!r} from Contacts")
                continue
        _log(f"  WARN: not found in address book: {token!r}")
        unresolved.append(token)

    for token in _split_recipient_field(cc):
        ok, source = _add_recipient_token(mail, namespace, token, _OL_CC)
        if ok:
            if source == "entry":
                _log(f"  Cc: added {token!r} from address book / Contacts")
            elif source == "smtp":
                _log(f"  Cc: added {token!r} (email)")
            continue
        if _gal_resolves(namespace, token):
            if _rebuild_named_recipient(mail, namespace, token, _OL_CC):
                _log(f"  Cc: re-linked {token!r} from Contacts")
                continue
        _log(f"  WARN: not found in address book: {token!r}")
        unresolved.append(token)

    for _ in range(3):
        try:
            mail.Recipients.ResolveAll()
        except Exception:
            pass
        time.sleep(0.3)

    _dedupe_mail_recipients(mail)
    return unresolved


def _recipient_ready_to_send(recip, namespace) -> bool:
    """
    True when Outlook has linked this recipient to the address book (safe to send).

    Plain display text without Resolved, AddressEntry, or SMTP is NOT ready.
    """
    try:
        if bool(getattr(recip, "Resolved", False)):
            return True
    except Exception:
        pass
    try:
        if recip.AddressEntry is not None:
            return True
    except Exception:
        pass
    addr = str(getattr(recip, "Address", "") or "").strip()
    if addr and _looks_like_smtp_address(addr):
        return True
    if addr and (addr.startswith("/o=") or addr.startswith("EX")):
        return True
    return False


def _recipients_not_ready(mail, namespace) -> list[str]:
    bad: list[str] = []
    try:
        for recip in mail.Recipients:
            if _recipient_ready_to_send(recip, namespace):
                continue
            bad.append(str(getattr(recip, "Name", "") or "?"))
    except Exception:
        pass
    return bad


def _rebuild_named_recipient(mail, namespace, name: str, rtype: int) -> bool:
    """Remove and re-add one recipient from Contacts / address book."""
    target = name.casefold()
    try:
        count = int(mail.Recipients.Count)
    except Exception:
        count = 0
    for idx in range(count, 0, -1):
        try:
            recip = mail.Recipients.Item(idx)
            if str(getattr(recip, "Name", "") or "").casefold() == target:
                mail.Recipients.Remove(idx)
        except Exception:
            continue
    hit = _find_address_entry(namespace, name)
    if hit is None:
        return False
    try:
        recip = mail.Recipients.Add(hit)
        recip.Type = rtype
        try:
            recip.Resolve()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _open_compose_inspector(mail, *, visible: bool) -> None:
    try:
        insp = mail.GetInspector()
    except Exception:
        mail.Display(False)
        insp = mail.GetInspector()
    try:
        insp.Visible = visible
    except Exception:
        if visible:
            mail.Display(False)


def _finalize_recipients_before_send(
    mail,
    namespace,
    *,
    vendor: str,
    for_send: bool,
) -> None:
    """
    Open the compose window (hidden for send), re-add groups from Contacts, and wait
    until Outlook links each recipient — avoids sending to plain text.
    """
    timeout_s = _recipient_resolve_timeout_s()
    deadline = time.monotonic() + timeout_s
    _open_compose_inspector(mail, visible=not for_send)
    time.sleep(0.5)

    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            mail.Recipients.ResolveAll()
        except Exception:
            pass

        bad = _recipients_not_ready(mail, namespace)
        if not bad:
            _log(
                f"{vendor}: recipients confirmed"
                + (" — sending." if for_send else " (preview).")
            )
            if for_send:
                try:
                    mail.GetInspector().Visible = False
                except Exception:
                    pass
            return

        for name in list(bad):
            if _looks_like_smtp_address(name):
                continue
            rtype = _OL_TO
            try:
                for idx in range(1, int(mail.Recipients.Count) + 1):
                    recip = mail.Recipients.Item(idx)
                    if _name_matches(str(getattr(recip, "Name", "") or ""), name):
                        rtype = int(recip.Type)
                        break
            except Exception:
                pass
            if _rebuild_named_recipient(mail, namespace, name, rtype):
                _log(f"{vendor}: re-linked {name!r} from Contacts")

        if attempt == 1 or attempt % 4 == 0:
            _log(f"{vendor}: waiting for Outlook to link: {', '.join(bad)}")
        time.sleep(0.75)

    bad = _recipients_not_ready(mail, namespace)
    _dedupe_mail_recipients(mail)
    if bad and for_send:
        _log_recipient_resolution(
            mail, to="", cc="", unresolved=bad, namespace=namespace
        )
        raise VendorEmailError(
            f"{vendor}: will not send — To/CC not confirmed: {', '.join(bad)}. "
            "Run with --preview and check the group shows with a + chip. "
            "Use the exact name from Outlook Contacts."
        )
    if bad:
        _log(
            f"{vendor}: WARN preview — still plain text for: {', '.join(bad)}. "
            "Do not send until the + chip appears."
        )


def _recipient_resolve_timeout_s() -> float:
    raw = (os.environ.get("VENDOR_EMAIL_RESOLVE_TIMEOUT_S") or "45").strip()
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 45.0


def _log_recipient_resolution(
    mail, *, to: str, cc: str, unresolved: list[str], namespace=None
) -> None:
    _log(f"  TO={to!r} CC={cc!r}")
    try:
        for recip in mail.Recipients:
            name = str(getattr(recip, "Name", "") or "")
            addr = str(getattr(recip, "Address", "") or "")
            rtype = int(getattr(recip, "Type", 0))
            kind = "To" if rtype == _OL_TO else "Cc" if rtype == _OL_CC else f"type={rtype}"
            resolved = _recipient_is_resolved(recip, namespace)
            ready = _recipient_ready_to_send(recip, namespace)
            in_book = "yes" if (name and namespace and _gal_resolves(namespace, name)) else "no"
            _log(
                f"  recipient [{kind}]: {name!r} ready={ready} resolved={resolved} "
                f"in_book={in_book} address={addr!r}"
            )
    except Exception as exc:
        _log(f"  (could not list recipients: {exc})")
    if unresolved:
        _log(f"  WARN: not in address book: {', '.join(unresolved)}")


def _collect_vendor_attachments(cfg: VendorEmailConfig, vendor_folder: str) -> list[Path]:
    vdir = cfg.daily_vendor_root / vendor_folder
    if not vdir.is_dir():
        return []

    skip_subfolders = {name.casefold() for name in cfg.skip_subfolders_inside_vendor}
    skip_prefixes = tuple(prefix.casefold() for prefix in cfg.skip_file_prefixes)

    out: list[Path] = []
    for item in sorted(vdir.iterdir(), key=lambda p: p.name.casefold()):
        name_cf = item.name.casefold()
        if item.is_dir():
            if name_cf in skip_subfolders:
                continue
            continue
        if skip_prefixes and any(name_cf.startswith(prefix) for prefix in skip_prefixes):
            continue
        out.append(item)
    return out


def _run_date_stamp(d: date | None = None) -> str:
    x = d or date.today()
    return f"{x.month}-{x.day}-{x.year}"


def _subject_with_optional_date(subject: str, *, append_date: bool) -> str:
    base = (subject or "").strip()
    if not append_date:
        return base
    stamp = _run_date_stamp()
    return f"{base} {stamp}".strip()


def _body_text_to_html(body: str) -> str:
    safe = escape(body or "")
    return safe.replace("\r\n", "\n").replace("\n", "<br>")


def _inline_signature_image_paths(sig_html: str, sig_file: Path) -> str:
    """Rewrite relative <img src> in signature .htm to absolute file paths."""
    sig_dir = sig_file.parent

    def fix_src(match: re.Match[str]) -> str:
        quote = match.group(1)
        src = (match.group(2) or "").strip()
        if not src or src.startswith(("http:", "https:", "cid:", "file:", "data:")):
            return match.group(0)
        asset = (sig_dir / src.replace("/", os.sep)).resolve()
        if asset.is_file():
            return f"src={quote}{asset.as_uri()}{quote}"
        return match.group(0)

    return re.sub(r'src=(["\'])([^"\']+)\1', fix_src, sig_html, flags=re.IGNORECASE)


def _load_outlook_signature_html(signature_name: str) -> str | None:
    name = (signature_name or "").strip()
    if not name:
        return None
    appdata = Path.home() / "AppData" / "Roaming"
    sig_file = appdata / "Microsoft" / "Signatures" / f"{name}.htm"
    try:
        if sig_file.is_file():
            html = sig_file.read_text(encoding="utf-8", errors="ignore")
            return _inline_signature_image_paths(html, sig_file)
    except OSError:
        return None
    return None


def _set_mail_body_with_optional_signature(
    mail,
    body: str,
    *,
    signature_name: str,
    signature_image_path: Path | None,
    show_window: bool = True,
) -> bool:
    """
    Set HTML body + signature. Returns True if a compose window was opened.
    Outlook embeds signature images via Display + HTMLBody — raw .htm paste breaks images.
    """
    opened = False
    if signature_name:
        try:
            mail.Display(False)
            opened = True
            if not show_window:
                try:
                    mail.GetInspector().Visible = False
                except Exception:
                    pass
            time.sleep(0.6)
        except Exception:
            pass
        try:
            existing_html = str(getattr(mail, "HTMLBody", "") or "").strip()
        except Exception:
            existing_html = ""
        if existing_html:
            try:
                mail.HTMLBody = f"{_body_text_to_html(body)}<br><br>{existing_html}"
                return opened
            except Exception:
                pass

    sig_html = _load_outlook_signature_html(signature_name)
    if sig_html:
        try:
            mail.HTMLBody = f"{_body_text_to_html(body)}<br><br>{sig_html}"
            return opened
        except Exception:
            pass

    if signature_image_path and signature_image_path.is_file():
        try:
            attachment = mail.Attachments.Add(str(signature_image_path))
            cid = "cornerstone_signature_logo"
            attachment.PropertyAccessor.SetProperty(
                "http://schemas.microsoft.com/mapi/proptag/0x3712001F",
                cid,
            )
            mail.HTMLBody = (
                "<html><body style='font-family:Calibri,Arial,sans-serif;font-size:11pt;'>"
                f"{_body_text_to_html(body)}"
                "<br><br>"
                f"<img src='cid:{cid}' alt='Cornerstone Products Group logo'>"
                "</body></html>"
            )
            return opened
        except Exception:
            pass
    mail.Body = body
    return opened


def send_vendor_emails(
    *,
    config_path: Path,
    dry_run: bool = True,
    preview: bool = False,
    vendor_filter: str | None = None,
    preview_pause: bool = True,
    send_delay_s: float = 0.5,
) -> int:
    cfg = load_vendor_email_config(config_path)
    if not cfg.daily_vendor_root.is_dir():
        raise VendorEmailError(f"Daily vendor folder not found: {cfg.daily_vendor_root}")

    if preview and dry_run:
        dry_run = False

    filter_cf = (vendor_filter or "").strip().casefold()
    if filter_cf:
        _log(f"Vendor filter: {vendor_filter!r}")

    _log(f"Daily vendor root: {cfg.daily_vendor_root}")
    if preview:
        _log("Mode: PREVIEW (Outlook compose windows open; nothing is sent)")
    elif dry_run:
        _log("Mode: DRY RUN (console preview only; Outlook is not opened)")
    else:
        _log("Mode: SEND")
    _log(
        f"Subject date suffix: {'enabled' if cfg.append_run_date_to_subject else 'disabled'}"
    )
    if cfg.outlook_signature_name:
        sig = _load_outlook_signature_html(cfg.outlook_signature_name)
        if sig:
            _log(f"Outlook signature: {cfg.outlook_signature_name!r}")
        else:
            _log(f"WARN: Outlook signature not found: {cfg.outlook_signature_name!r}")
    if cfg.signature_image_path:
        if cfg.signature_image_path.is_file():
            _log(f"Signature image: {cfg.signature_image_path}")
        else:
            _log(f"WARN: signature image not found (sending text-only body): {cfg.signature_image_path}")

    app = None if dry_run else _ensure_outlook_app()
    namespace = None
    if app is not None:
        try:
            namespace = app.GetNamespace("MAPI")
        except Exception as exc:
            raise VendorEmailError(f"Could not open Outlook address book: {exc}") from exc
    sent = 0
    skipped = 0

    for entry in cfg.vendors:
        vendor = entry.vendor_folder
        if filter_cf and vendor.casefold() != filter_cf:
            continue
        root_entry = cfg.daily_vendor_root / vendor
        if not root_entry.exists():
            _log(f"Skip {vendor!r}: folder not present today.")
            skipped += 1
            continue
        if not root_entry.is_dir():
            _log(f"Skip {vendor!r}: not a directory.")
            skipped += 1
            continue

        attachments = _collect_vendor_attachments(cfg, vendor)
        if not attachments:
            _log(f"Skip {vendor!r}: no files to attach.")
            skipped += 1
            continue

        if not entry.to:
            _log(f"Skip {vendor!r}: TO is empty in config.")
            skipped += 1
            continue
        if not entry.subject:
            _log(f"Skip {vendor!r}: subject is empty in config.")
            skipped += 1
            continue
        if not entry.body.strip():
            _log(f"Skip {vendor!r}: body is empty in config.")
            skipped += 1
            continue

        final_subject = _subject_with_optional_date(
            entry.subject, append_date=cfg.append_run_date_to_subject
        )
        _log(f"{vendor}: {len(attachments)} attachment(s)")
        if dry_run:
            _log(f"  TO={entry.to!r} CC={entry.cc!r}")
            _log(f"  Subject={final_subject!r}")
            if cfg.outlook_signature_name:
                _log(f"  outlook_signature={cfg.outlook_signature_name!r}")
            if cfg.signature_image_path and cfg.signature_image_path.is_file():
                _log(f"  signature: {cfg.signature_image_path.name}")
            for path in attachments:
                _log(f"  attach: {path.name}")
            sent += 1
            continue

        if preview:
            mail = app.CreateItem(0)  # olMailItem
            mail.Subject = final_subject
            unresolved = _apply_mail_recipients(
                mail, namespace, to=entry.to, cc=entry.cc
            )
            if unresolved:
                _log(f"  WARN: initial add could not resolve: {', '.join(unresolved)}")
            for path in attachments:
                try:
                    mail.Attachments.Add(str(path))
                except Exception as exc:
                    raise VendorEmailError(
                        f"{vendor}: failed adding attachment {path.name!r}: {exc}"
                    ) from exc
            try:
                opened = _set_mail_body_with_optional_signature(
                    mail,
                    entry.body,
                    signature_name=cfg.outlook_signature_name,
                    signature_image_path=cfg.signature_image_path,
                    show_window=False,
                )
            except Exception as exc:
                raise VendorEmailError(
                    f"{vendor}: failed while setting body/signature: {exc}"
                ) from exc
            _dedupe_mail_recipients(mail)
            _finalize_recipients_before_send(
                mail, namespace, vendor=vendor, for_send=False
            )
            _log_recipient_resolution(
                mail,
                to=entry.to,
                cc=entry.cc,
                unresolved=unresolved,
                namespace=namespace,
            )
            _log(f"  Subject={final_subject!r}")
            if not opened:
                _open_compose_inspector(mail, visible=True)
            else:
                try:
                    mail.GetInspector().Visible = True
                except Exception:
                    mail.Display(False)
            _log("  Review To/CC — each group should show with a + chip before you send.")
            if preview_pause:
                try:
                    input("  Close the message when done, then press Enter for the next vendor… ")
                except EOFError:
                    time.sleep(max(1.0, send_delay_s))
            sent += 1
            continue

        mail = app.CreateItem(0)  # olMailItem
        mail.Subject = final_subject
        unresolved = _apply_mail_recipients(mail, namespace, to=entry.to, cc=entry.cc)
        if unresolved:
            _log(f"  WARN: initial add could not resolve: {', '.join(unresolved)}")
        for path in attachments:
            try:
                mail.Attachments.Add(str(path))
            except Exception as exc:
                raise VendorEmailError(
                    f"{vendor}: failed adding attachment {path.name!r}: {exc}"
                ) from exc
        try:
            _set_mail_body_with_optional_signature(
                mail,
                entry.body,
                signature_name=cfg.outlook_signature_name,
                signature_image_path=cfg.signature_image_path,
                show_window=False,
            )
        except Exception as exc:
            raise VendorEmailError(
                f"{vendor}: failed while setting body/signature: {exc}"
            ) from exc
        _dedupe_mail_recipients(mail)
        _finalize_recipients_before_send(
            mail, namespace, vendor=vendor, for_send=True
        )
        _log_recipient_resolution(
            mail, to=entry.to, cc=entry.cc, unresolved=[], namespace=namespace
        )
        try:
            mail.Send()
        except Exception as exc:
            raise VendorEmailError(f"{vendor}: failed on Send(): {exc}") from exc
        sent += 1
        _log(f"Sent {vendor!r}")
        time.sleep(max(0.0, send_delay_s))

    label = "Prepared"
    if preview:
        label = "Previewed"
    elif not dry_run:
        label = "Sent"
    _log(f"Done. {label} {sent}; skipped {skipped}.")
    if filter_cf and sent == 0 and skipped == 0:
        _log(f"No vendor matched filter {vendor_filter!r}. Check vendor_folder in JSON.")
    return 0

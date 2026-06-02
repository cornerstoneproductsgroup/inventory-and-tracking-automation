"""Send daily vendor-order emails from z- Daily Vendor Orders via Outlook."""

from __future__ import annotations

import json
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
        to = _as_str(item.get("to"))
        cc = _as_str(item.get("cc"))
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


def _apply_mail_recipients(
    mail,
    namespace,
    *,
    to: str,
    cc: str,
) -> list[str]:
    """
    Add To/CC through Recipients and resolve (distribution lists, GAL display names).

    Setting mail.To = "Group Name" often leaves plain text in the compose window.
    Recipients.Add + Resolve() matches typing the name and pressing Tab/Enter.
    """
    unresolved: list[str] = []

    def add_one(display_name: str, rtype: int) -> None:
        name = display_name.strip()
        if not name:
            return

        recip = mail.Recipients.Add(name)
        recip.Type = rtype
        if recip.Resolve():
            return

        gal = None
        try:
            gal = namespace.CreateRecipient(name)
            if not gal.Resolve():
                gal = None
        except Exception:
            gal = None

        if gal is not None:
            try:
                mail.Recipients.Remove(recip.Index)
            except Exception:
                pass
            resolved_name = str(getattr(gal, "Name", "") or name).strip()
            entry = mail.Recipients.Add(resolved_name)
            entry.Type = rtype
            if entry.Resolve():
                return
            try:
                addr = str(getattr(gal, "Address", "") or "").strip()
                if addr:
                    mail.Recipients.Remove(entry.Index)
                    entry = mail.Recipients.Add(addr)
                    entry.Type = rtype
                    if entry.Resolve():
                        return
            except Exception:
                pass

        unresolved.append(name)

    for name in _split_recipient_field(to):
        add_one(name, _OL_TO)
    for name in _split_recipient_field(cc):
        add_one(name, _OL_CC)

    try:
        mail.Recipients.ResolveAll()
    except Exception:
        pass

    return unresolved


def _log_recipient_resolution(mail, *, to: str, cc: str, unresolved: list[str]) -> None:
    _log(f"  TO={to!r} CC={cc!r}")
    try:
        for recip in mail.Recipients:
            name = str(getattr(recip, "Name", "") or "")
            addr = str(getattr(recip, "Address", "") or "")
            rtype = int(getattr(recip, "Type", 0))
            kind = "To" if rtype == _OL_TO else "Cc" if rtype == _OL_CC else f"type={rtype}"
            resolved = bool(getattr(recip, "Resolved", False))
            _log(f"  recipient [{kind}]: {name!r} resolved={resolved} address={addr!r}")
    except Exception as exc:
        _log(f"  (could not list recipients: {exc})")
    if unresolved:
        _log(f"  WARN: unresolved name(s): {', '.join(unresolved)}")


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


def _load_outlook_signature_html(signature_name: str) -> str | None:
    name = (signature_name or "").strip()
    if not name:
        return None
    appdata = (Path.home() / "AppData" / "Roaming")
    sig_file = appdata / "Microsoft" / "Signatures" / f"{name}.htm"
    try:
        if sig_file.is_file():
            return sig_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    return None


def _set_mail_body_with_optional_signature(
    mail,
    body: str,
    *,
    signature_name: str,
    signature_image_path: Path | None,
) -> None:
    # Prefer Outlook's native default signature (already configured in profile),
    # because it carries embedded resources the same way as manual New Email.
    if signature_name:
        try:
            mail.Display(False)
        except Exception:
            pass
        try:
            existing_html = str(getattr(mail, "HTMLBody", "") or "").strip()
        except Exception:
            existing_html = ""
        if existing_html:
            try:
                mail.HTMLBody = f"{_body_text_to_html(body)}<br><br>{existing_html}"
                return
            except Exception:
                pass

    sig_html = _load_outlook_signature_html(signature_name)
    if sig_html:
        try:
            mail.HTMLBody = f"{_body_text_to_html(body)}<br><br>{sig_html}"
            return
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
            return
        except Exception:
            pass
    mail.Body = body


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
            _log_recipient_resolution(mail, to=entry.to, cc=entry.cc, unresolved=unresolved)
            try:
                _set_mail_body_with_optional_signature(
                    mail,
                    entry.body,
                    signature_name=cfg.outlook_signature_name,
                    signature_image_path=cfg.signature_image_path,
                )
            except Exception as exc:
                raise VendorEmailError(
                    f"{vendor}: failed while setting body/signature: {exc}"
                ) from exc
            for path in attachments:
                try:
                    mail.Attachments.Add(str(path))
                except Exception as exc:
                    raise VendorEmailError(
                        f"{vendor}: failed adding attachment {path.name!r}: {exc}"
                    ) from exc
            _log(f"  Subject={final_subject!r}")
            _log("  Opening message in Outlook — verify To/CC show as resolved groups.")
            try:
                mail.Display(False)
            except Exception as exc:
                raise VendorEmailError(f"{vendor}: failed opening preview: {exc}") from exc
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
            raise VendorEmailError(
                f"{vendor}: could not resolve recipient(s): {', '.join(unresolved)}. "
                "Use the exact Outlook display name from the address book."
            )
        try:
            _set_mail_body_with_optional_signature(
                mail,
                entry.body,
                signature_name=cfg.outlook_signature_name,
                signature_image_path=cfg.signature_image_path,
            )
        except Exception as exc:
            raise VendorEmailError(
                f"{vendor}: failed while setting body/signature: {exc}"
            ) from exc
        for path in attachments:
            try:
                mail.Attachments.Add(str(path))
            except Exception as exc:
                raise VendorEmailError(
                    f"{vendor}: failed adding attachment {path.name!r}: {exc}"
                ) from exc
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

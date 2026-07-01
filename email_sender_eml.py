"""
email_sender_eml.py
-------------------
Generates .eml draft files that you open in Outlook to review and send.

Zero external dependencies — pure Python stdlib. Works without pywin32, without
Microsoft Graph credentials, and behind any corporate firewall/proxy.

Workflow:
    pipeline runs -> .eml created in outbox/ -> Outlook opens it as a draft ->
    you review/edit -> click Send -> message lands in your Sent Items folder.

Modes:
    preview  (default) -> print payload, write nothing.
    save               -> write .eml to outbox/, return path. No window pops up.
    open               -> write .eml AND launch it via os.startfile (opens in
                          your default mail handler, usually Outlook).

Defaulting to 'preview' means a misconfiguration can never accidentally
write or open anything.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Optional


DEFAULT_OUTBOX = Path(__file__).resolve().parent / "outbox"
VALID_MODES = ("preview", "save", "open")
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024


class EmailSenderError(Exception):
    pass


@dataclass(frozen=True)
class EmailPayload:
    to: str
    subject: str
    body_text: str
    cc: tuple[str, ...] = ()
    attachment_path: Optional[Path] = None


@dataclass(frozen=True)
class SendResult:
    ok: bool
    mode: str
    eml_path: Optional[Path] = None
    error: Optional[str] = None
    payload: Optional[dict] = None


# ---------------------------------------------------------------------------
# Build the .eml
# ---------------------------------------------------------------------------

def _validate_payload(p: EmailPayload) -> None:
    if not p.to or "@" not in p.to:
        raise EmailSenderError(f"Invalid 'to' address: {p.to!r}")
    if p.attachment_path is not None:
        ap = Path(p.attachment_path)
        if not ap.exists():
            raise EmailSenderError(f"Attachment not found: {ap}")
        size = ap.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise EmailSenderError(
                f"Attachment {ap.name} is {size / 1024 / 1024:.1f} MB; "
                f"cap is {MAX_ATTACHMENT_BYTES / 1024 / 1024:.0f} MB."
            )


def _payload_to_dict(p: EmailPayload) -> dict:
    return {
        "to": p.to,
        "cc": list(p.cc),
        "subject": p.subject,
        "body": p.body_text,
        "attachment": str(p.attachment_path) if p.attachment_path else None,
    }


def _build_eml_message(p: EmailPayload) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = p.to
    if p.cc:
        msg["Cc"] = ", ".join(p.cc)
    msg["Subject"] = p.subject
    # X-Unsent: 1 makes Outlook open the .eml as a draft for editing/sending
    # rather than as a received message.
    msg["X-Unsent"] = "1"
    msg.set_content(p.body_text)

    if p.attachment_path:
        path = Path(p.attachment_path)
        ctype, _ = mimetypes.guess_type(path.name)
        if ctype is None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    return msg


def _make_filename(p: EmailPayload) -> str:
    """Build a safe, informative filename for the .eml."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", p.subject)[:60].strip("_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}__{safe}.eml" if safe else f"{ts}.eml"


def _write_eml(p: EmailPayload, outbox: Path) -> Path:
    outbox.mkdir(parents=True, exist_ok=True)
    msg = _build_eml_message(p)
    path = outbox / _make_filename(p)
    path.write_bytes(bytes(msg))
    return path


def _open_in_default_app(path: Path) -> None:
    if hasattr(os, "startfile"):
        os.startfile(str(path))  # Windows
    else:
        # Non-Windows fallback for testing only
        raise EmailSenderError(
            "os.startfile not available on this platform; "
            f"open {path} manually."
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def send_email(
    payload: EmailPayload,
    mode: str = "preview",
    outbox: Optional[Path] = None,
) -> SendResult:
    """
    Returns SendResult; does not raise on normal failures.
    mode: 'preview' (default) | 'save' | 'open'
    """
    if mode not in VALID_MODES:
        return SendResult(ok=False, mode=mode,
                          error=f"Invalid mode {mode!r}; expected {VALID_MODES}")

    try:
        _validate_payload(payload)
    except EmailSenderError as e:
        return SendResult(ok=False, mode=mode, error=str(e))

    fields = _payload_to_dict(payload)

    if mode == "preview":
        return SendResult(ok=True, mode="preview", payload=fields)

    target_outbox = outbox or DEFAULT_OUTBOX
    try:
        eml_path = _write_eml(payload, target_outbox)
    except OSError as e:
        return SendResult(ok=False, mode=mode, payload=fields,
                          error=f"Failed to write .eml: {e}")

    if mode == "save":
        return SendResult(ok=True, mode="save", eml_path=eml_path, payload=fields)

    # mode == "open"
    try:
        _open_in_default_app(eml_path)
    except EmailSenderError as e:
        return SendResult(ok=False, mode=mode, eml_path=eml_path,
                          payload=fields, error=str(e))
    return SendResult(ok=True, mode="open", eml_path=eml_path, payload=fields)


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Build / open an .eml email file.")
    ap.add_argument("--to", required=True)
    ap.add_argument("--cc", default="")
    ap.add_argument("--subject", default="[TEST] email_sender_eml.py wiring")
    ap.add_argument("--body", default="Wiring test from email_sender_eml.py.")
    ap.add_argument("--attachment", default=None)
    ap.add_argument("--mode", default="preview", choices=VALID_MODES)
    args = ap.parse_args(argv[1:])

    payload = EmailPayload(
        to=args.to,
        subject=args.subject,
        body_text=args.body,
        cc=tuple(c.strip() for c in args.cc.split(",") if c.strip()),
        attachment_path=Path(args.attachment) if args.attachment else None,
    )
    result = send_email(payload, mode=args.mode)

    if result.mode == "preview":
        print("=== PREVIEW (no file written) ===")
        print(json.dumps(result.payload, indent=2))
        return 0 if result.ok else 1

    if result.ok:
        if result.mode == "save":
            print(f"OK: saved {result.eml_path}")
        else:
            print(f"OK: saved and opened {result.eml_path}")
        return 0
    print(f"FAIL: {result.error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
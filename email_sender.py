"""
email_sender.py
---------------
Sends emails via Microsoft Graph using Entra ID client-credentials.
Designed to slot beside cortex_client.py — same tenant, same auth shape.

Env vars (load via .env in project root; never commit):
    GRAPH_CLIENT_ID
    GRAPH_CLIENT_SECRET
    GRAPH_TENANT_ID        (Lilly tenant, same as Cortex)
    GRAPH_SENDER           (UPN of mailbox to send from, e.g. lawrence@lilly.com)

The Entra app registration needs Microsoft Graph -> Mail.Send (application)
permission with admin consent granted. Talk to IT/security before requesting.

Two modes:
    dry_run=True  (DEFAULT) -> builds payload, returns it, no network call
    dry_run=False           -> acquires token, POSTs to Graph /sendMail

Defaulting to dry-run means a misconfiguration can never accidentally send.
Callers must explicitly opt in to live sending.

Usage from another module:
    from email_sender import send_email, EmailPayload
    result = send_email(
        EmailPayload(
            to="me@lilly.com",
            cc=(),
            subject="Test",
            body_text="Hello",
            attachment_path=None,
        ),
        dry_run=True,
    )
    print(result.ok, result.error)

Standalone:
    python email_sender.py --to me@lilly.com                 # dry-run
    python email_sender.py --to me@lilly.com --send          # live send
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

REQUIRED_ENV = ("GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_TENANT_ID", "GRAPH_SENDER")

# Graph sendMail inline attachment cap is ~4 MB. Anything bigger needs an
# upload session, which we don't implement here.
MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024


class EmailSenderError(Exception):
    """Raised on auth, payload, or Graph errors."""


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
    dry_run: bool
    http_status: Optional[int] = None
    error: Optional[str] = None
    payload: Optional[dict] = None  # the Graph JSON that was (or would be) sent


# ---------------------------------------------------------------------------
# .env loader — minimal, defensive
# ---------------------------------------------------------------------------

def _load_env_file(env_path: Path) -> None:
    """Read KEY=VALUE lines. Only sets vars not already in os.environ."""
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _ensure_env_loaded() -> None:
    _load_env_file(Path(__file__).resolve().parent / ".env")


# ---------------------------------------------------------------------------
# Token + payload
# ---------------------------------------------------------------------------

def _get_token(client_id: str, client_secret: str, tenant_id: str) -> str:
    url = TOKEN_URL_TMPL.format(tenant=tenant_id)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": GRAPH_SCOPE,
        "grant_type": "client_credentials",
    }
    r = requests.post(url, data=data, timeout=30)
    if r.status_code != 200:
        raise EmailSenderError(
            f"Token request failed (HTTP {r.status_code}): {r.text[:500]}"
        )
    body = r.json()
    if "access_token" not in body:
        raise EmailSenderError(f"Token response missing access_token: {body}")
    return body["access_token"]


def _build_attachment(path: Path) -> dict:
    if not path.exists():
        raise EmailSenderError(f"Attachment not found: {path}")
    content = path.read_bytes()
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise EmailSenderError(
            f"Attachment {path.name} is {len(content) / 1024 / 1024:.1f} MB; "
            f"Graph inline cap is ~4 MB. Use a SharePoint link or upload session."
        )
    if path.suffix.lower() == ".pptx":
        ct = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    elif path.suffix.lower() == ".pdf":
        ct = "application/pdf"
    else:
        ct = "application/octet-stream"
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": path.name,
        "contentType": ct,
        "contentBytes": base64.b64encode(content).decode("ascii"),
    }


def _build_payload(p: EmailPayload) -> dict:
    if not p.to or "@" not in p.to:
        raise EmailSenderError(f"Invalid 'to' address: {p.to!r}")
    msg: dict = {
        "subject": p.subject,
        "body": {"contentType": "Text", "content": p.body_text},
        "toRecipients": [{"emailAddress": {"address": p.to}}],
    }
    if p.cc:
        msg["ccRecipients"] = [{"emailAddress": {"address": a}} for a in p.cc]
    if p.attachment_path:
        msg["attachments"] = [_build_attachment(Path(p.attachment_path))]
    return {"message": msg, "saveToSentItems": True}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def send_email(payload: EmailPayload, dry_run: bool = True) -> SendResult:
    """
    Send via Graph. dry_run=True (default) returns the payload without sending.

    Returns SendResult; never raises for normal failures (auth, HTTP errors,
    bad payload). Caller inspects .ok / .error.
    """
    try:
        body = _build_payload(payload)
    except EmailSenderError as e:
        return SendResult(ok=False, dry_run=dry_run, error=str(e))

    if dry_run:
        return SendResult(ok=True, dry_run=True, payload=body)

    _ensure_env_loaded()
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        return SendResult(
            ok=False, dry_run=False,
            error=f"Missing env vars: {missing}. Set them in .env (project root).",
        )

    try:
        token = _get_token(
            os.environ["GRAPH_CLIENT_ID"],
            os.environ["GRAPH_CLIENT_SECRET"],
            os.environ["GRAPH_TENANT_ID"],
        )
    except EmailSenderError as e:
        return SendResult(ok=False, dry_run=False, error=str(e))

    sender = os.environ["GRAPH_SENDER"]
    url = f"{GRAPH_BASE}/users/{sender}/sendMail"
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=60,
        )
    except requests.RequestException as e:
        return SendResult(ok=False, dry_run=False, error=f"Network error: {e}")

    if r.status_code == 202:
        # Graph sendMail returns 202 Accepted with no body on success.
        return SendResult(ok=True, dry_run=False, http_status=202, payload=body)
    return SendResult(
        ok=False, dry_run=False, http_status=r.status_code,
        error=f"Graph sendMail HTTP {r.status_code}: {r.text[:500]}",
        payload=body,
    )


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Send a test email via Microsoft Graph.")
    ap.add_argument("--to", required=True, help="Recipient address")
    ap.add_argument("--cc", default="", help="Comma-separated CC list")
    ap.add_argument("--subject", default="[TEST] email_sender.py wiring")
    ap.add_argument("--body", default="This is a wiring test from email_sender.py.")
    ap.add_argument("--attachment", default=None, help="Path to a file to attach")
    ap.add_argument("--send", action="store_true",
                    help="Actually send (default is dry-run)")
    args = ap.parse_args(argv[1:])

    payload = EmailPayload(
        to=args.to,
        subject=args.subject,
        body_text=args.body,
        cc=tuple(c.strip() for c in args.cc.split(",") if c.strip()),
        attachment_path=Path(args.attachment) if args.attachment else None,
    )
    result = send_email(payload, dry_run=not args.send)

    if result.dry_run:
        print("=== DRY RUN (no network call) ===")
        print(json.dumps(result.payload, indent=2))
        return 0 if result.ok else 1

    if result.ok:
        print(f"OK: sent to {payload.to} (HTTP {result.http_status})")
        return 0
    print(f"FAIL: {result.error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
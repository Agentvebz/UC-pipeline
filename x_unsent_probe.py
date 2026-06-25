#!/usr/bin/env python3
"""
x_unsent_probe.py -- one-shot diagnostic for the .eml -> Outlook draft flow.

Purpose: confirm that an .eml carrying `X-Unsent: 1`, opened via os.startfile(),
lands in Outlook as an EDITABLE DRAFT (Send button present; To/Subject/body
editable) rather than a read-only received message. This gates the whole
Prepare Email flow -- if it opens read-only, the open path needs to change
before any other hardening matters.

Stdlib only. Run on the Windows box with Outlook desktop installed:

    python x_unsent_probe.py

Nothing is transmitted. A draft just opens for visual inspection.
Close it WITHOUT sending.
"""

from __future__ import annotations

import os
import tempfile
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import SMTP, default as default_policy

# send-to-self, so even an accidental Send is harmless
TO_ADDR = "lawrence.mondal@lilly.com"
SUBJECT = "[PROBE] X-Unsent draft test -- do not send"
BODY = (
    "If you are reading this inside an Outlook compose window with a Send "
    "button, and you can edit this text plus the To/Subject fields, the draft "
    "path works.\n\n"
    "If this opened read-only (no Send button, cannot edit the body), then "
    "X-Unsent is not honored on this Outlook build and the Prepare Email flow "
    "needs a different open path.\n"
)
ATTACH = True  # set False to test the no-attachment case


def build_eml() -> bytes:
    msg = EmailMessage(policy=SMTP)  # SMTP policy -> CRLF line endings
    msg["To"] = TO_ADDR
    msg["Subject"] = SUBJECT
    msg["X-Unsent"] = "1"  # the header under test
    msg.set_content(BODY)
    if ATTACH:
        msg.add_attachment(
            b"dummy attachment payload -- stands in for the deck\n",
            maintype="text",
            subtype="plain",
            filename="probe_attachment.txt",
        )
    return msg.as_bytes()


def verify_roundtrip(raw: bytes) -> None:
    parsed = BytesParser(policy=default_policy).parsebytes(raw)
    assert str(parsed["X-Unsent"]) == "1", "X-Unsent missing after round-trip"
    assert str(parsed["To"]).strip() == TO_ADDR, "To mismatch after round-trip"
    atts = [p.get_filename() for p in parsed.iter_attachments()]
    print(f"  parsed OK | X-Unsent={parsed['X-Unsent']} | To={parsed['To']} | attachments={atts}")


def main() -> int:
    raw = build_eml()
    print(f"[1/3] built .eml ({len(raw)} bytes)")
    verify_roundtrip(raw)
    print("[2/3] round-trip parse passed")

    path = os.path.join(tempfile.gettempdir(), "x_unsent_probe.eml")
    with open(path, "wb") as f:
        f.write(raw)
    print(f"[3/3] wrote {path}")

    if not hasattr(os, "startfile"):
        print("\nos.startfile unavailable (not Windows). Run this on the Lilly box.")
        print(f"Or open manually: {path}")
        return 0

    print("\nLaunching default handler (should be Outlook). Inspect the window:")
    print("  PASS -> compose window, Send button present, To/Subject/body editable")
    print("  FAIL -> read-only message, no Send button, cannot edit")
    print("Close WITHOUT sending.\n")
    os.startfile(path)  # type: ignore[attr-defined]  # Windows-only
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
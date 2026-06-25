"""
send_test.py
------------
End-to-end smoke test using the .eml backend (zero deps, works behind any
corporate firewall).

  1. Look up the owner via email_router (routing CSV)
  2. Build a plain-text test email
  3. Preview / save / open it in your default mail handler (Outlook)

Usage:
    python send_test.py IT                  # preview only (no file written)
    python send_test.py IT --mode save      # write .eml to outbox/ silently
    python send_test.py IT --mode open      # write .eml AND open in Outlook
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from email_router import lookup_owner, RoutingError
from email_sender_eml import EmailPayload, send_email, VALID_MODES


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("country", help="ISO-2 country code (e.g. IT, GB)")
    ap.add_argument("--mode", default="preview", choices=VALID_MODES,
                    help="preview (default) | save | open")
    args = ap.parse_args(argv[1:])

    try:
        owner = lookup_owner(args.country)
    except RoutingError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not owner.is_resolved:
        print(f"ERROR: {owner.country_code} has placeholder email <TBD> "
              f"in routing CSV.", file=sys.stderr)
        print(f"       Edit config/country_owners.csv first.", file=sys.stderr)
        return 2

    body = (
        f"Hello {owner.name},\n\n"
        f"This is an end-to-end wiring test for the IBU NBA/E UC performance "
        f"reporting pipeline.\n\n"
        f"If you received this, the routing CSV lookup and .eml integration "
        f"are working correctly.\n\n"
        f"Country:   {owner.country_code}\n"
        f"Role:      {owner.role}\n"
        f"Timestamp: {datetime.now().isoformat(timespec='seconds')}\n\n"
        f"No action required - this is a test message.\n"
    )
    payload = EmailPayload(
        to=owner.email,
        cc=owner.cc,
        subject=f"[TEST] IBU NBA/E pipeline wiring - {owner.country_code}",
        body_text=body,
    )
    result = send_email(payload, mode=args.mode)

    if result.mode == "preview":
        print("=== PREVIEW (no file written) ===")
        print(f"To:      {owner.email}")
        if owner.cc:
            print(f"CC:      {'; '.join(owner.cc)}")
        print(f"Subject: {payload.subject}")
        print()
        print("--- Body ---")
        print(payload.body_text)
        print("--- Payload dict ---")
        print(json.dumps(result.payload, indent=2))
        return 0 if result.ok else 1

    if result.ok:
        if result.mode == "save":
            print(f"OK: saved {result.eml_path}")
            print("    Double-click the file in Explorer to open it in Outlook.")
        else:
            print(f"OK: saved and opened {result.eml_path}")
            print("    The email should now be open in Outlook for review.")
            print("    Edit if needed, then click Send.")
        return 0
    print(f"FAIL: {result.error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
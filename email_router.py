"""
email_router.py
---------------
Maps an IBU country code to the owner (NBE Lead / DSM / Ops) who should
receive the UC performance report email.

Routing file: config/country_owners.csv
Columns:
    country_code   ISO-2 (GB, DE, FR, ES, IT, PL, JP, CA, SA, AE, CN)
    owner_name     Display name (used in greeting / preview)
    owner_email    Primary recipient (To:)
    cc_emails      Semicolon- or comma-separated CC list (may be blank or <TBD>)
    role           Free-text role label (NBE Lead, DSM, Ops, etc.)

Usage from other modules:
    from email_router import lookup_owner, RoutingError
    owner = lookup_owner("IT")
    if not owner.is_resolved:
        ...                         # placeholder still in CSV
    print(owner.email, owner.cc)

Standalone sanity-check:
    python email_router.py          # list all rows, exit 2 if any unresolved
    python email_router.py IT       # look up one country
"""
from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# Default location relative to project root (this file sits in the root).
DEFAULT_ROUTING_CSV = Path(__file__).resolve().parent / "config" / "country_owners.csv"

# Basic email shape check — not RFC-perfect, just catches obvious typos / TBDs.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Sentinel used in the CSV for un-filled placeholders.
TBD = "<TBD>"

# Required CSV columns.
_REQUIRED_COLS = ("country_code", "owner_name", "owner_email", "cc_emails", "role")


class RoutingError(Exception):
    """Raised when the routing CSV is missing, malformed, or lacks the requested country."""


@dataclass(frozen=True)
class Owner:
    country_code: str
    name: str
    email: str
    cc: tuple[str, ...] = field(default_factory=tuple)
    role: str = ""

    @property
    def is_resolved(self) -> bool:
        """True if the primary email is a real address (not <TBD> / blank)."""
        return self.email not in ("", TBD) and bool(_EMAIL_RE.match(self.email))


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _split_cc(raw: str) -> tuple[str, ...]:
    raw = _clean(raw)
    if not raw or raw == TBD:
        return ()
    parts = [p.strip() for p in re.split(r"[;,]", raw) if p.strip()]
    return tuple(parts)


def _load_routing(csv_path: Path) -> dict[str, Owner]:
    if not csv_path.exists():
        raise RoutingError(f"Routing CSV not found: {csv_path}")

    owners: dict[str, Owner] = {}
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing = [c for c in _REQUIRED_COLS if c not in fieldnames]
        if missing:
            raise RoutingError(
                f"Routing CSV {csv_path} missing required columns: {missing}"
            )

        for line_no, row in enumerate(reader, start=2):  # row 1 = header
            code = _clean(row.get("country_code")).upper()
            if not code:
                continue  # skip blank rows silently
            if code in owners:
                raise RoutingError(
                    f"Duplicate country_code '{code}' in {csv_path} (line {line_no})"
                )
            owners[code] = Owner(
                country_code=code,
                name=_clean(row.get("owner_name")),
                email=_clean(row.get("owner_email")),
                cc=_split_cc(row.get("cc_emails") or ""),
                role=_clean(row.get("role")),
            )

    if not owners:
        raise RoutingError(f"Routing CSV {csv_path} contains no country rows")
    return owners


def lookup_owner(country_code: str, csv_path: Path | str | None = None) -> Owner:
    """
    Look up the Owner for a country code.

    Raises RoutingError if the CSV is missing/malformed or the country has no row.
    Does NOT raise on TBD placeholders — callers should check `owner.is_resolved`
    so they can show a clear "fill in the routing file" message instead of a stack trace.
    """
    path = Path(csv_path) if csv_path else DEFAULT_ROUTING_CSV
    owners = _load_routing(path)
    code = _clean(country_code).upper()
    if code not in owners:
        raise RoutingError(
            f"No routing entry for country '{code}' in {path}. "
            f"Known countries: {sorted(owners.keys())}"
        )
    return owners[code]


def list_owners(csv_path: Path | str | None = None) -> list[Owner]:
    """Return all owner rows, sorted by country_code. Useful for audits / startup checks."""
    path = Path(csv_path) if csv_path else DEFAULT_ROUTING_CSV
    return sorted(_load_routing(path).values(), key=lambda o: o.country_code)


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def _print_owner(o: Owner) -> None:
    status = "OK " if o.is_resolved else "TBD"
    print(f"[{status}] {o.country_code}  {o.role:<12}  {o.email}")
    if o.name and o.name != TBD:
        print(f"        Name: {o.name}")
    if o.cc:
        print(f"        CC:   {'; '.join(o.cc)}")


def _main(argv: list[str]) -> int:
    try:
        if len(argv) > 1:
            owner = lookup_owner(argv[1])
            _print_owner(owner)
            if not owner.is_resolved:
                print(f"\nWARNING: {owner.country_code} has placeholder email; "
                      f"fill in {DEFAULT_ROUTING_CSV} before sending.")
                return 2
            return 0

        rows = list_owners()
        print(f"Routing file: {DEFAULT_ROUTING_CSV}")
        print(f"Countries:    {len(rows)}\n")
        for o in rows:
            _print_owner(o)
        unresolved = [o.country_code for o in rows if not o.is_resolved]
        if unresolved:
            print(f"\nUnresolved ({len(unresolved)}/{len(rows)}): "
                  f"{', '.join(unresolved)}")
            return 2
        print("\nAll countries resolved.")
        return 0

    except RoutingError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
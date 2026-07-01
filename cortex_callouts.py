"""
cortex_callouts.py — report-enhancement layer on top of the team's verified
cortex_client.py. This module owns the *prompting / KPI* logic only; all
transport, auth and config live in cortex_client (do not duplicate them here).

Public functions:
  generate_callout_reasons(brand, rows) -> list[str]
      One concise "potential reason" per underperforming use-case row, aligned
      to input order. Used by report_generator to fill the Key Callouts column.
  generate_key_callouts(country, kpis) -> str
      Slide-level plain-text narrative (optional; e.g. an executive-summary slide).

cortex_client.chat() returns errors as a "⚠️ ..." string rather than raising;
for a leadership report that's dangerous, so we convert it to a CortexError.
"""
from __future__ import annotations

import json
from typing import Any

try:
    from cortex_client import chat
except ImportError:                       # if the cortex_*.py files live under src/
    from src.cortex_client import chat


class CortexError(RuntimeError):
    """Raised when Cortex returns an error sentinel or an unparseable response."""


def _guard(out: str) -> str:
    if out is None or out.strip().startswith("\u26a0"):  # ⚠️
        raise CortexError(out or "empty response from Cortex")
    return out.strip()


def _parse_json_array(text: str, expected: int) -> list[str]:
    """Extract a JSON array of strings from the model output, tolerant of any
    prose or code fences around it. Raises if shape/length is wrong."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise CortexError(f"no JSON array in response: {text[:200]!r}")
    try:
        arr = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise CortexError(f"could not parse reasons JSON: {e}")
    if not isinstance(arr, list) or len(arr) != expected:
        got = len(arr) if isinstance(arr, list) else type(arr).__name__
        raise CortexError(f"expected {expected} reasons, got {got}")
    return [str(x).strip() for x in arr]


# ---------------------------------------------------------------------------
# Per-use-case "Potential Reasons" (fills the Key Callouts table column)
# ---------------------------------------------------------------------------
CALLOUT_REASONS_SYSTEM_PROMPT = (
    "You are a senior omnichannel analytics lead at a global pharmaceutical company, "
    "reviewing Next Best Action/Engagement (NBA/E) use-case performance for field reps.\n\n"
    "For each underperforming use case provided, write ONE concise potential reason — a "
    "hypothesis a leader could investigate — for why it is below threshold.\n\n"
    "Rules:\n"
    "- Max ~15 words per reason. Plain business prose. No markdown, no bold, no bullet symbols.\n"
    "- Do NOT restate the percentage; it is already on the slide. Explain the likely driver.\n"
    "- Frame as a plausible hypothesis to investigate, not asserted fact.\n"
    "- Ground reasons in realistic NBA/E drivers: targeting/eligibility logic, content relevance "
    "or VAE content gaps, channel or consent constraints, execution-window timing, rep workload "
    "or territory conflicts, low go-live maturity, or model precision for AI/ML use cases.\n"
    "- Never invent numbers or name specific customers.\n"
    "- Return ONLY a JSON array of strings, one per use case, in the same order as given."
)


def generate_callout_reasons(
    brand: str,
    rows: list[dict[str, Any]],
    max_tokens: int = 500,
    session_id: str = "ibu-uc-report",
) -> list[str]:
    """Return one potential-reason string per underperforming use-case row,
    aligned to the input order. Raises CortexError on failure (caller decides
    whether to fail the build or leave the cells blank)."""
    if not rows:
        return []
    payload = {"brand": brand, "underperforming_use_cases": rows}
    user_prompt = (
        f"{json.dumps(payload, indent=2)}\n\n"
        "Return a JSON array of potential-reason strings, one per use case, same order."
    )
    out = chat(user_prompt, system=CALLOUT_REASONS_SYSTEM_PROMPT,
               max_tokens=max_tokens, temperature=0.3, session_id=session_id)
    return _parse_json_array(_guard(out), expected=len(rows))


# ---------------------------------------------------------------------------
# Slide-level narrative (optional; plain text, PPTX-ready)
# ---------------------------------------------------------------------------
KEY_CALLOUTS_SYSTEM_PROMPT = (
    "You are a senior omnichannel analytics lead at a global pharmaceutical company. "
    "You write a concise 'Key Callouts' narrative for a leadership slide reviewing Next "
    "Best Action/Engagement (NBA/E) use-case performance.\n\n"
    "Rules:\n"
    "- Return ONLY plain text: no markdown, no bold, no headers, no title line, no bullets.\n"
    "- 2 to 4 short business-prose sentences.\n"
    "- Be specific about what performs well and what needs attention; imply scale/refine/remove "
    "without stating it as a label.\n"
    "- Only reference figures present in the data. Never invent numbers.\n"
    "- Treat high acceptance on low volume or very new use cases as not-yet-reliable; say so."
)


def generate_key_callouts(
    country: str,
    kpis: dict[str, Any],
    max_tokens: int = 600,
    session_id: str = "ibu-uc-report",
) -> str:
    user_prompt = (
        f"Country: {country}\n\n"
        f"Use-case KPI summary (JSON):\n{json.dumps(kpis, indent=2)}\n\n"
        "Write the Key Callouts."
    )
    return _guard(chat(user_prompt, system=KEY_CALLOUTS_SYSTEM_PROMPT,
                       max_tokens=max_tokens, temperature=0.3, session_id=session_id))


# ---------------------------------------------------------------------------
# Standalone smoke test
# ---------------------------------------------------------------------------
SAMPLE_ROWS = [
    {"use_case": "Omni ML - VAE", "metric": "dismissal rate", "acceptance_rate": 0.34,
     "dismissal_rate": 0.41, "no_action_rate": 0.25, "total_suggestions": 9100},
    {"use_case": "MSL Activity", "metric": "no action rate", "acceptance_rate": 0.39,
     "dismissal_rate": 0.15, "no_action_rate": 0.46, "total_suggestions": 4200},
]

if __name__ == "__main__":
    try:
        reasons = generate_callout_reasons("OMVOH", SAMPLE_ROWS)
        for r, reason in zip(SAMPLE_ROWS, reasons):
            print(f"- {r['use_case']} ({r['metric']}): {reason}")
    except CortexError as e:
        print(f"Cortex failed: {e}")
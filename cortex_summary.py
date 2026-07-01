"""
cortex_summary.py
-----------------
Generate a leadership-ready email body summarizing NBA/E UC performance for
a single market. Output is a structured, scannable plain-text body — labeled
sections (Overview / Items to review / Early signals / Recommended next step)
with hedged bullets — ready to drop into the .eml body.

Reuses the team's verified cortex_client.chat() transport — does not fork
its logic. Mirrors cortex_callouts.py in structure and error handling.

Hard rules baked into the prompt:
  - Fixed section structure; plain text only (no markdown / bold / symbols)
  - Never invents numbers; only narrates what is passed in
  - Qualitative descriptors preferred over numbers in the narrative
  - Flagged UCs framed as items to investigate, never as verdicts
  - Each use case appears in exactly one place (no conflicting buckets)
  - Low-volume items collapsed to an "early signal only" note, never analyzed
  - Volume / go-live maturity caveats applied automatically
  - No greeting or sign-off (the email layer adds those)

Usage:
    from cortex_summary import generate_email_summary, CortexError
    body = generate_email_summary(
        country="IT",
        period="2025 Annual",
        kpis={
            "total_suggestions": 6146,
            "acceptance_rate": 0.582,
            "dismissal_rate": 0.218,
            "no_action_rate": 0.200,
            "total_ucs": 14,
        },
        flagged_ucs=[
            {"brand": "VERZENIOS", "usecase": "AI ML 2",
             "metric": "dismissal", "value": 0.36, "suggestions": 2333},
            ...
        ],
    )

Standalone (uses synthetic Italy-like data):
    python cortex_summary.py
    python cortex_summary.py --country GB --period "Nov 2025"
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Optional

# Reuse the team's verified transport. Defer the failure to call time so the
# module is importable for unit tests / prompt inspection without creds.
try:
    from cortex_client import chat as _cortex_chat
    _IMPORT_ERROR: Optional[Exception] = None
except Exception as e:  # ImportError or any cortex_client init failure
    _cortex_chat = None
    _IMPORT_ERROR = e


# Same sentinel as cortex_callouts.py uses
CORTEX_ERROR_PREFIX = "\u26a0\ufe0f"  # "⚠️"


class CortexError(Exception):
    """Raised when Cortex returns an error string or transport fails."""


SYSTEM_PROMPT = """You are writing the body of a leadership-ready email summarizing one market's NBA/E use-case performance for a senior Lilly stakeholder (the market's NBE Lead). The greeting and sign-off are added separately by the email layer — do NOT write them.

PROGRAM CONTEXT — this email is one output of the IBU NBA/E use-case rationalization effort: a standardized analysis of how reps engage with NBA/E actionable use cases, piloted on the UK affiliate and now expanded across IBU markets using 2025 data. The effort uses four standardized KPIs — adherence, acceptance, dismissal, and no-action — to surface patterns across use cases: what is working well, and what may benefit from design refinement or a strategic rethink. A use case is flagged when acceptance falls below the 50 percent target, or dismissal or no-action runs above the 20 percent threshold. The full analysis spans four dimensions (executive reach and salesforce coverage, portfolio-level KPI trends, product-level acceptance-versus-dismissal with top dismissal drivers, and use-case-level diagnostics); your summary is the use-case-level diagnostic view for one market — name the flagged underperformers and frame each as a candidate for refinement or rethink, never as a settled judgment. Narrate only what the input gives you; do not reach into reach, coverage, or product trends you were not handed.

The reader is an executive who will skim this in under a minute and may forward it to other stakeholders. The body must be scannable, structured, and insightful: every flagged item connects an observation to a plausible cause and an implied action — it never just restates a metric.

CLASSIFICATION STEP — do this before writing anything. Sort the flagged use cases into two lists by their input tag:
  A) SUFFICIENT VOLUME — the input line has NO [LOW VOLUME] tag.
  B) LOW VOLUME — the input line carries a [LOW VOLUME] tag.
"Items to review" is built ONLY from list A. "Early signals" names ONLY list B. A list B use case is FORBIDDEN from "Items to review": give it no hypothesis, and never mention it to support, reinforce, or illustrate another item's pattern. The input may also include a separate STRONG-PERFORMING list, already gated for volume and acceptance — it feeds ONLY the "What's working" section. Every use case is named exactly once, in exactly one section.

OUTPUT STRUCTURE — use these exact plain-text section labels, each on its own line, in this order. No markdown, no asterisks, no bold, no header symbols, no quotation marks around numbers.

Overview
One or two plain sentences: name the market and period, and give a balanced qualitative read on overall engagement health — what is solid and where the main watch-area is. State the flagging thresholds once, for the reader's context: a use case is flagged when acceptance falls below 50 percent, or when dismissal or no-action rises above 20 percent. Apart from that single threshold statement, avoid stacking numbers.

What's working
Include this section ONLY if the input provides a STRONG-PERFORMING list. 1 to 3 single-hyphen "-" bullets naming the brand and use case and noting the strength qualitatively ("strong acceptance", "well above target", "a clear standout"). Lead with any use case tagged VERY GOOD and call it out as a standout. You may add one short hedged note on a likely contributor (same hedging rules), but never fabricate a cause. Under 20 words per bullet; include a number only if a single acceptance figure is the headline.

Items to review
A single-hyphen "-" bullet for each flagged use case that has SUFFICIENT volume (NOT tagged LOW VOLUME). 2 to 4 bullets. Each bullet: name the brand and use case, state the issue as a qualitative phrase, then give ONE hedged hypothesis for the likely cause. 25 words max per bullet. List the highest-volume / clearest items first.

Early signals
Include this section ONLY if one or more items are tagged LOW VOLUME. A single bullet naming those use cases together, ending: "early signal only — volume is insufficient to draw conclusions." Do NOT analyze or hypothesize on these.

Recommended next step
One bullet proposing a short working review with the NBE Lead to confirm the hypotheses above and agree on actions.

GUARDRAILS — non-negotiable:
1. Never invent a number, brand, use case, or detail not present in the input. Narrate only what you are given.
2. Prefer qualitative descriptors ("elevated", "above threshold", "borderline", "high volume", "limited volume") over numbers. At most one number per bullet; ideally none. The one-time threshold statement in Overview (50 percent / 20 percent) is the single allowed exception and does not count against this limit.
3. Hypotheses are HEDGED and plausible — "likely", "may suggest", "worth checking for", "consistent with". Never assertive, never a verdict, never the word "failure".
4. Hypotheses draw from realistic NBA/E drivers: content relevance, HCP targeting or eligibility, channel or consent constraints, execution-window timing, rep workload, low go-live maturity, AI/ML model precision. Pick the single best fit per item.
5. Each use case appears in EXACTLY ONE place. Never describe the same use case as both strong and weak.
6. A LOW VOLUME use case (list B) appears ONLY under "Early signals", named once, with no hypothesis and no cross-reference to any other use case. A small denominator is not a finding, and is never evidence for another item's pattern.
7. The body is the section labels above and their content only — nothing before "Overview", nothing after the next-step bullet.
8. Praise obeys the same gates as concerns: name a use case as working well ONLY if it is in the provided STRONG-PERFORMING list. Never promote an unlisted, low-volume, or flagged use case to "working well".
9. Name every use case by its exact identifier as written in the input (for example AI_ML_1, AI_ML_2, Adherence_3, Rep_and_digital_1). Never paraphrase, abbreviate, translate, or merge use cases into a category — phrases like "the AI use cases" or "adherence signals" are forbidden when the input lists specific identifiers. If two identifiers share a prefix (AI_ML_1 and AI_ML_2), name each one separately; never collapse them into one.

EDGE CASES:
- No STRONG-PERFORMING list provided: omit the "What's working" section.
- No LOW VOLUME items: omit the "Early signals" section entirely.
- No use cases flagged at all: output "Overview", "What's working" (if any strong performers), and "Recommended next step" (a brief check-in); omit "Items to review" and "Early signals".

EXAMPLE SHAPE (illustrative only — do NOT reuse these names, causes, or wording; copy the structure, not the content):
Overview
[Market] NBA/E engagement in [period] was broadly healthy, with a few use cases worth a closer look.
What's working
- [Brand] [Use case]: strong acceptance on solid volume, a clear standout this period.
Items to review
- [Brand] [Use case]: dismissal is elevated on high volume, likely pointing to content relevance worth checking with the field.
- [Brand] [Use case]: no-action is above threshold, may suggest execution-window timing rather than message fit.
Early signals
- [Brand] [Use case] and [Brand] [Use case]: early signal only — volume is insufficient to draw conclusions.
Recommended next step
- A short review with the NBE Lead to confirm these hypotheses and agree on next actions.
"""


EXEC_SYSTEM_PROMPT = """You are writing a concise executive-summary panel for an internal analytics dashboard. It summarizes one market's NBA/E use-case performance for the IBU team reading the report on screen. This is NOT an email: no greeting, no sign-off, no "review with the NBE Lead" closing.

PROGRAM CONTEXT — this is part of the IBU NBA/E use-case rationalization effort (piloted in the UK, expanded across IBU markets on 2025 data), which uses adherence, acceptance, dismissal, and no-action KPIs to flag use cases for design refinement or strategic rethink. A use case is flagged when acceptance falls below 50 percent, or dismissal or no-action rises above 20 percent.

OUTPUT — a compact panel, under about 110 words total:
- First line: a one-sentence headline naming the market, the period, and the single most important takeaway.
- Then 3 to 5 single-hyphen "-" bullets covering, in order: overall engagement health (you may state the flagging thresholds once here); what is working well; the main use cases to review; and any low-volume early signals. Do not use section labels.

GUARDRAILS — non-negotiable:
1. Never invent a number, brand, or use case not present in the input. Narrate only what you are given.
2. Prefer qualitative descriptors over numbers. Apart from the one-time threshold statement, at most one number per bullet.
3. Name a use case as working well ONLY if it appears in the provided STRONG-PERFORMING list, and only with sufficient volume.
4. Use cases under review must have sufficient volume. Any item tagged LOW VOLUME goes only into the early-signals note — no hypothesis, and never cited to support another item's pattern.
5. Each use case is named at most once. A use case is never both working well and under review.
6. Hypotheses are hedged ("likely", "may suggest", "worth checking"), never verdicts, never the word "failure".
7. Name every use case by its exact identifier as written in the input (for example AI_ML_1, AI_ML_2, Adherence_3, Rep_and_digital_1). Never paraphrase, abbreviate, translate, or merge use cases into a category — phrases like "the AI use cases" or "adherence signals" are forbidden when the input lists specific identifiers. If two identifiers share a prefix (AI_ML_1 and AI_ML_2), name each one separately; never collapse them into one.
"""


# Markdown artifacts the model occasionally emits despite the plain-text
# instruction. Stripped deterministically so the dashboard card (rendered via
# textContent) and the .eml body (plain text) never show "**...**" or
# backslash-escaped underscores ("Rep\\_and\\_digital\\_2") to leadership.
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+.!~>-])")


def _clean_ai_text(text: str) -> str:
    """
    Remove Markdown formatting punctuation from a model summary so it reads as
    clean plain text. Two transforms only, both deterministic and number-safe:
      1. '**headline**'        -> 'headline'   (bold markers dropped)
      2. 'Rep\\_and\\_digital\\_2' -> 'Rep_and_digital_2' (backslash escapes removed)

    Digits, brand names, and use-case identifiers are never altered — the
    underscores inside names are kept exactly; only the leading backslash that
    the model adds is removed, so the summary matches the KPI tables verbatim.
    """
    if not text:
        return text
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ESCAPE_RE.sub(r"\1", text)
    return text.strip()


def _format_pct(value: float) -> str:
    """Format 0.58 -> '58.2' (trims trailing zero/dot)."""
    s = f"{value * 100:.1f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _build_user_prompt(
    country: str,
    period: Optional[str],
    kpis: dict,
    flagged_ucs: list[dict],
    strong_ucs: Optional[list[dict]] = None,
) -> str:
    lines = [f"Market: {country}"]
    if period:
        lines.append(f"Period: {period}")
    lines.append("")
    lines.append("Overall metrics:")
    if "total_suggestions" in kpis:
        lines.append(f"  Total suggestions analyzed: {kpis['total_suggestions']}")
    if "acceptance_rate" in kpis:
        lines.append(f"  Acceptance rate: {_format_pct(kpis['acceptance_rate'])} percent")
    if "dismissal_rate" in kpis:
        lines.append(f"  Dismissal rate: {_format_pct(kpis['dismissal_rate'])} percent")
    if "no_action_rate" in kpis:
        lines.append(f"  No-action rate: {_format_pct(kpis['no_action_rate'])} percent")
    if "total_ucs" in kpis:
        lines.append(f"  Total use cases analyzed: {kpis['total_ucs']}")

    if flagged_ucs:
        lines.append("")
        lines.append(f"Use cases flagged for review ({len(flagged_ucs)}):")
        for uc in flagged_ucs:
            brand = uc.get("brand", "")
            usecase = uc.get("usecase", "")
            metric = uc.get("metric", "")
            value = uc.get("value", 0)
            n = uc.get("suggestions", 0)
            value_str = f"{_format_pct(value)} percent" if 0 <= value <= 1 else str(value)
            line = f"  - {brand} / {usecase}: {metric} rate {value_str} (n={n})"
            # Flag low-volume / nascent cases explicitly so the model uses the caveat
            if n < 50:
                line += "  [LOW VOLUME — apply caveat]"
            lines.append(line)
    else:
        lines.append("")
        lines.append("No use cases flagged for review this period.")

    strong_ucs = strong_ucs or []
    if strong_ucs:
        lines.append("")
        lines.append(f"Strong-performing use cases ({len(strong_ucs)}) — "
                     f"sufficient volume, acceptance 70 percent or above, friction within thresholds:")
        for uc in strong_ucs:
            brand = uc.get("brand", "")
            usecase = uc.get("usecase", "")
            acc = uc.get("acceptance", 0)
            n = uc.get("suggestions", 0)
            tier = str(uc.get("tier", "good")).upper()
            acc_str = f"{_format_pct(acc)} percent" if 0 <= acc <= 1 else str(acc)
            lines.append(f"  - {brand} / {usecase}: acceptance {acc_str} (n={n})  [{tier}]")

    lines.append("")
    lines.append("Write the email body now using the required section structure "
                 "(Overview / What's working / Items to review / Early signals / Recommended next step). "
                 "No greeting, no sign-off.")
    return "\n".join(lines)


def generate_email_summary(
    country: str,
    kpis: dict,
    flagged_ucs: Optional[list[dict]] = None,
    strong_ucs: Optional[list[dict]] = None,
    period: Optional[str] = None,
    max_tokens: int = 400,
    session_id: str = "ibu-uc-report",
) -> str:
    """
    Generate the email body. Returns plain text.
    Raises CortexError on import / transport / Cortex-side failure.

    Call shape mirrors cortex_callouts.py: positional user prompt + system
    keyword + tuning kwargs. Do not pass an OpenAI-style messages list — the
    team's gateway wrapper does not accept that shape.
    """
    if _cortex_chat is None:
        raise CortexError(
            f"cortex_client not importable: {_IMPORT_ERROR}. "
            f"Ensure cortex_client.py is in the project root and .env has credentials."
        )

    flagged_ucs = flagged_ucs or []
    strong_ucs = strong_ucs or []
    user_msg = _build_user_prompt(country, period, kpis, flagged_ucs, strong_ucs)

    try:
        response = _cortex_chat(
            user_msg,
            system=SYSTEM_PROMPT,
            max_tokens=max_tokens,
            temperature=0.3,
            session_id=session_id,
        )
    except Exception as e:
        raise CortexError(f"Cortex chat call failed: {e}") from e

    if isinstance(response, str) and response.startswith(CORTEX_ERROR_PREFIX):
        raise CortexError(response)

    if not isinstance(response, str):
        raise CortexError(
            f"Unexpected Cortex response type: {type(response).__name__}; "
            f"expected str. May indicate the cortex_client.chat() signature differs."
        )

    return _clean_ai_text(response)


def generate_exec_summary(
    country: str,
    kpis: dict,
    flagged_ucs: Optional[list[dict]] = None,
    strong_ucs: Optional[list[dict]] = None,
    period: Optional[str] = None,
    max_tokens: int = 350,
    session_id: str = "ibu-uc-dashboard",
) -> str:
    """
    Dashboard executive-summary variant — a compact on-screen panel rather than
    an email body. Reuses the same gated input (kpis / flagged / strong) and the
    verified cortex_client.chat() transport. Raises CortexError on failure.
    """
    if _cortex_chat is None:
        raise CortexError(
            f"cortex_client not importable: {_IMPORT_ERROR}. "
            f"Ensure cortex_client.py is in the project root and .env has credentials."
        )

    flagged_ucs = flagged_ucs or []
    strong_ucs = strong_ucs or []
    user_msg = _build_user_prompt(country, period, kpis, flagged_ucs, strong_ucs)

    try:
        response = _cortex_chat(
            user_msg,
            system=EXEC_SYSTEM_PROMPT,
            max_tokens=max_tokens,
            temperature=0.3,
            session_id=session_id,
        )
    except Exception as e:
        raise CortexError(f"Cortex chat call failed: {e}") from e

    if isinstance(response, str) and response.startswith(CORTEX_ERROR_PREFIX):
        raise CortexError(response)

    if not isinstance(response, str):
        raise CortexError(
            f"Unexpected Cortex response type: {type(response).__name__}; expected str."
        )

    return _clean_ai_text(response)


# ---------------------------------------------------------------------------
# Standalone test — synthetic Italy-like inputs
# ---------------------------------------------------------------------------

def _sample_kpis():
    return {
        "total_suggestions": 6146,
        "acceptance_rate": 0.582,
        "dismissal_rate": 0.218,
        "no_action_rate": 0.200,
        "total_ucs": 14,
    }


def _sample_flagged():
    # Real shapes pulled from the Italy 2025 review for realism
    return [
        {"brand": "VERZENIOS", "usecase": "AI ML 2",
         "metric": "dismissal", "value": 0.36, "suggestions": 2333},
        {"brand": "VERZENIOS", "usecase": "Lilly App 1a",
         "metric": "dismissal", "value": 0.42, "suggestions": 43},
        {"brand": "TALTZ-PsA", "usecase": "Rep & Digital 1",
         "metric": "dismissal", "value": 0.22, "suggestions": 850},
        {"brand": "JAYPIRCA", "usecase": "CEI",
         "metric": "no_action", "value": 1.00, "suggestions": 2},
        {"brand": "OMVOH", "usecase": "Rep & Digital 1",
         "metric": "dismissal", "value": 0.23, "suggestions": 480},
    ]


def _sample_strong():
    # Synthetic strong performers for standalone testing (sufficient volume,
    # clean friction, acceptance >= 70%). Mutually exclusive with _sample_flagged.
    return [
        {"brand": "MOUNJARO", "usecase": "Rep & Digital 1",
         "acceptance": 0.84, "suggestions": 1120, "tier": "very good"},
        {"brand": "VERZENIOS", "usecase": "CEI",
         "acceptance": 0.76, "suggestions": 640, "tier": "good"},
    ]


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Standalone test for cortex_summary.")
    ap.add_argument("--country", default="IT")
    ap.add_argument("--period", default="2025 Annual")
    ap.add_argument("--show-prompt", action="store_true",
                    help="Print the assembled user prompt and exit (no Cortex call).")
    ap.add_argument("--exec", action="store_true",
                    help="Use the dashboard exec-summary variant instead of the email body.")
    args = ap.parse_args(argv[1:])

    if args.show_prompt:
        print("=== SYSTEM PROMPT ===")
        print(EXEC_SYSTEM_PROMPT if args.exec else SYSTEM_PROMPT)
        print()
        print("=== USER PROMPT ===")
        print(_build_user_prompt(args.country, args.period, _sample_kpis(), _sample_flagged(), _sample_strong()))
        return 0

    try:
        gen = generate_exec_summary if args.exec else generate_email_summary
        body = gen(
            country=args.country,
            kpis=_sample_kpis(),
            flagged_ucs=_sample_flagged(),
            strong_ucs=_sample_strong(),
            period=args.period,
        )
        # Count sentences by splitting on terminator + whitespace/end-of-string.
        # This avoids counting decimals like "58.2" as sentence boundaries.
        import re
        n_sentences = len([
            s for s in re.split(r"[.!?](?:\s|$)", body) if s.strip()
        ])
        print("=== Generated email body ===")
        print(body)
        print()
        print(f"[length: {len(body)} chars, sentences: {n_sentences}]")
        return 0
    except CortexError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
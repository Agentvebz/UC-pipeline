"""
kpi_engine.py — Compute all KPI rates from actionable suggestion data.

Matches the UK notebook (EU_NBA_UC_Analysis__UK_.ipynb) exactly:

  kpi_rates()         — core function: adherence, acceptance, execution, dismissal, no_action
  compute_overall()   — overall KPIs across all data
  compute_by_country()— KPIs per country
  compute_by_brand()  — KPIs per medicine (brand)
  compute_by_month()  — KPIs per month
  compute_by_country_brand() — KPIs per country x brand
  compute_by_brand_usecase() — KPIs per medicine x sugg_name
  dismissal_breakdown() — Pareto of dismissal reasons
  compute_funnel()    — funnel: Total → Adhered → Accepted → Executed
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================================
# Core KPI function — matches notebook Cell 9 exactly
# ============================================================================

def kpi_rates(g: pd.DataFrame) -> pd.Series:
    """
    Compute all KPI rates for a group of suggestions.

    Matches your notebook:
        adherence_rate        = adhered / total
        acceptance_rate       = accepted / (accepted + dismissed)   ← "Tracker" rate
        acceptance_rate_total = accepted / total
        execution_rate        = executed / accepted
        dismissal_rate        = dismissed / (accepted + dismissed)  ← "Tracker" rate
        dismissal_rate_total  = dismissed / total
        no_action_rate        = ignored / total
        adherence_due_to_acceptance = accepted / adhered
        adherence_due_to_dismissal  = dismissed / adhered
    """
    total = len(g)
    accepted = int(g["is_accepted"].sum())
    dismissed = int(g["is_dismissed"].sum())
    adhered = int(g["is_adhered"].sum())
    executed = int(g["is_executed"].sum())
    ignored = int(g["is_ignored"].sum())

    return pd.Series({
        "total_suggestions": total,
        "adherence_rate": adhered / total if total else np.nan,
        "acceptance_rate": accepted / (accepted + dismissed) if (accepted + dismissed) else np.nan,
        "acceptance_rate_total": accepted / total if total else np.nan,
        "execution_rate": executed / accepted if accepted else np.nan,
        "dismissal_rate_total": dismissed / total if total else np.nan,
        "dismissal_rate": dismissed / (accepted + dismissed) if (accepted + dismissed) else np.nan,
        "no_action_rate": ignored / total if total else np.nan,
        "ignored_cnt": ignored,
        "accept_count": accepted,
        "dismiss_count": dismissed,
        "exec_count": executed,
        "adherence_due_to_acceptance": accepted / adhered if adhered else np.nan,
        "adherence_due_to_dismissal": dismissed / adhered if adhered else np.nan,
    })


# ============================================================================
# Groupby helpers
# ============================================================================

def compute_overall(df: pd.DataFrame) -> pd.DataFrame:
    """Overall KPIs across all data."""
    result = kpi_rates(df).to_frame("overall").T
    logger.info("Computed overall KPIs")
    return result


def compute_by_country(df: pd.DataFrame) -> pd.DataFrame:
    """KPIs per country."""
    result = df.groupby("country").apply(kpi_rates).reset_index()
    logger.info(f"Computed KPIs for {result['country'].nunique()} countries")
    return result


def compute_by_brand(df: pd.DataFrame) -> pd.DataFrame:
    """KPIs per medicine (brand)."""
    result = df.groupby("medicine").apply(kpi_rates).reset_index()
    logger.info(f"Computed KPIs for {result['medicine'].nunique()} brands")
    return result


def compute_by_month(df: pd.DataFrame) -> pd.DataFrame:
    """KPIs per month (from sugg_posted_date)."""
    df = df.copy()
    df["sugg_posted_date"] = pd.to_datetime(df["sugg_posted_date"], errors="coerce")
    df["month"] = df["sugg_posted_date"].dt.to_period("M").astype(str)
    result = df.groupby("month").apply(kpi_rates).reset_index()
    logger.info(f"Computed KPIs for {result['month'].nunique()} months")
    return result


def compute_by_country_brand(df: pd.DataFrame) -> pd.DataFrame:
    """KPIs per country x medicine."""
    result = df.groupby(["country", "medicine"]).apply(kpi_rates).reset_index()
    logger.info(f"Computed KPIs for {len(result)} country-brand combinations")
    return result


def compute_by_country_month(df: pd.DataFrame) -> pd.DataFrame:
    """KPIs per country x month."""
    df = df.copy()
    df["sugg_posted_date"] = pd.to_datetime(df["sugg_posted_date"], errors="coerce")
    df["month"] = df["sugg_posted_date"].dt.to_period("M").astype(str)
    result = df.groupby(["country", "month"]).apply(kpi_rates).reset_index()
    logger.info(f"Computed KPIs for {len(result)} country-month combinations")
    return result


def compute_by_brand_usecase(df: pd.DataFrame) -> pd.DataFrame:
    """
    KPIs per medicine x sugg_name (use case).

    Matches notebook Cell 27/29:
        actionable_df.groupby(["medicine", "sugg_name"]).apply(...)
    """
    # Normalize CEI variants (notebook Cell 26)
    df = df.copy()
    if "sugg_name" in df.columns:
        df["sugg_name"] = df["sugg_name"].apply(
            lambda x: "CEI" if str(x).startswith("CEI") else x
        )
    result = df.groupby(["medicine", "sugg_name"]).apply(kpi_rates).reset_index()
    logger.info(f"Computed KPIs for {len(result)} brand-usecase combinations")
    return result


def compute_by_usecase(df: pd.DataFrame) -> pd.DataFrame:
    """KPIs per use case (sugg_name) across all brands."""
    df = df.copy()
    if "sugg_name" in df.columns:
        df["sugg_name"] = df["sugg_name"].apply(
            lambda x: "CEI" if str(x).startswith("CEI") else x
        )
    result = df.groupby("sugg_name").apply(kpi_rates).reset_index()
    logger.info(f"Computed KPIs for {result['sugg_name'].nunique()} use cases")
    return result


# ============================================================================
# Dismissal breakdown — matches notebook Cell 24
# ============================================================================

def dismissal_breakdown(df: pd.DataFrame, by: str = None, top_n: int = 10) -> pd.DataFrame:
    """
    Pareto of dismissal reasons.

    Optionally grouped by country or medicine.
    Matches notebook Cell 24 + Cell 48.
    """
    import re

    dismissed = df[df["is_dismissed"] == 1].copy()
    reason_col = "survey_dismissal_answer_1"

    if reason_col not in dismissed.columns:
        logger.warning(f"Column '{reason_col}' not found — cannot compute dismissal breakdown.")
        return pd.DataFrame()

    # Clean reasons (notebook Cell 24)
    dismissed["reason_clean"] = (
        dismissed[reason_col]
        .dropna()
        .apply(lambda x: re.sub(r"^\d+\.\s*", "", str(x)).strip().rstrip("."))
    )

    dismissed = dismissed[dismissed["reason_clean"].notna()]

    if by and by in dismissed.columns:
        result = (
            dismissed.groupby([by, "reason_clean"])
            .size()
            .reset_index(name="count")
            .sort_values([by, "count"], ascending=[True, False])
        )
        # Add percentage within each group
        totals = result.groupby(by)["count"].transform("sum")
        result["pct"] = result["count"] / totals
    else:
        result = (
            dismissed["reason_clean"]
            .value_counts()
            .reset_index()
        )
        result.columns = ["reason", "count"]
        result["pct"] = result["count"] / result["count"].sum()
        result["cumulative_pct"] = result["pct"].cumsum()

    logger.info(f"Dismissal breakdown: {len(result)} entries")
    return result.head(top_n) if not by else result


# ============================================================================
# Funnel — matches notebook Cell 13
# ============================================================================

def compute_funnel(df: pd.DataFrame) -> pd.DataFrame:
    """
    KPI funnel: Total → Adhered → Accepted → Executed.
    Matches notebook Cell 13.
    """
    total = len(df)
    adhered = int(df["is_adhered"].sum())
    accepted = int(df["is_accepted"].sum())
    executed = int(df["is_executed"].sum())

    funnel = pd.DataFrame({
        "stage": ["Total Suggestions", "Adhered", "Accepted", "Executed"],
        "count": [total, adhered, accepted, executed],
        "pct_of_total": [
            1.0,
            adhered / total if total else 0,
            accepted / total if total else 0,
            executed / total if total else 0,
        ],
        "drop_off_pct": [
            0,
            (total - adhered) / total if total else 0,
            (adhered - accepted) / adhered if adhered else 0,
            (accepted - executed) / accepted if accepted else 0,
        ],
    })
    logger.info(f"Funnel: {total:,} → {adhered:,} → {accepted:,} → {executed:,}")
    return funnel


# ============================================================================
# Run all KPIs — single entry point
# ============================================================================

def run_kpi_engine(df: pd.DataFrame, output_dir: str = "./data/processed") -> dict[str, pd.DataFrame]:
    """
    Compute all KPIs and save as CSV files.

    Args:
        df: Cleaned actionable DataFrame (output of ingest.py)
        output_dir: Where to save CSV outputs

    Returns:
        Dict of DataFrames: {name: df}
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 50)
    logger.info("KPI ENGINE — START")
    logger.info("=" * 50)

    results = {}

    # 1. Overall
    results["overall"] = compute_overall(df)

    # 2. By country
    results["by_country"] = compute_by_country(df)

    # 3. By brand
    results["by_brand"] = compute_by_brand(df)

    # 4. By month
    results["by_month"] = compute_by_month(df)

    # 5. By country x brand
    results["by_country_brand"] = compute_by_country_brand(df)

    # 6. By country x month
    results["by_country_month"] = compute_by_country_month(df)

    # 7. By brand x use case
    results["by_brand_usecase"] = compute_by_brand_usecase(df)

    # 8. By use case (overall)
    results["by_usecase"] = compute_by_usecase(df)

    # 9. Dismissal breakdown (overall)
    results["dismissal_reasons"] = dismissal_breakdown(df, top_n=15)

    # 10. Dismissal breakdown by country
    results["dismissal_by_country"] = dismissal_breakdown(df, by="country")

    # 11. Funnel
    results["funnel"] = compute_funnel(df)

    # Save all as CSV
    for name, result_df in results.items():
        if not result_df.empty:
            path = out / f"kpi_{name}.csv"
            result_df.to_csv(path, index=False)
            logger.info(f"  Saved: {path}")

    logger.info("=" * 50)
    logger.info(f"KPI ENGINE COMPLETE — {len(results)} report(s) generated")
    logger.info("=" * 50)

    return results
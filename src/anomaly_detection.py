"""
anomaly_detection.py — Detect anomalies in KPI metrics.

Two detection methods:
  1. Z-score — flags when a metric deviates >2 std devs from its trailing history
  2. Threshold — flags when a metric crosses a hard business rule

Each anomaly gets a severity (warning / critical) and a plain-English explanation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================================
# Anomaly result
# ============================================================================

@dataclass
class Anomaly:
    metric: str
    dimension: str          # e.g. "GB", "MOUNJARO", "overall"
    current_value: float
    reference_value: float  # mean or threshold
    severity: str           # "warning" or "critical"
    direction: str          # "spike", "drop", or "breach"
    explanation: str        # Plain English


# ============================================================================
# Z-score detection — for trended metrics
# ============================================================================

def zscore_detect(
    df: pd.DataFrame,
    metric_col: str,
    group_col: str = None,
    time_col: str = "month",
    z_warning: float = 1.5,
    z_critical: float = 2.0,
    min_history: int = 2,
    direction: str = "both",  # "spike", "drop", or "both"
) -> list[Anomaly]:
    """
    Flag the latest period if it deviates significantly from history.

    For each group (country, brand, etc.):
      1. Sort by time
      2. Compute mean and std of all periods except the latest
      3. Compute z-score of the latest period
      4. Flag if |z| > threshold
    """
    anomalies: list[Anomaly] = []

    if group_col and group_col in df.columns:
        groups = df.groupby(group_col)
    else:
        groups = [("overall", df)]

    for name, group in groups:
        group = group.sort_values(time_col)
        if len(group) < min_history + 1:
            continue  # Not enough history

        history = group[metric_col].iloc[:-1]
        latest = group[metric_col].iloc[-1]
        latest_period = group[time_col].iloc[-1]

        mean = history.mean()
        std = history.std()

        if std == 0 or pd.isna(std) or pd.isna(latest):
            continue

        z = (latest - mean) / std

        # Check direction
        if direction == "spike" and z < 0:
            continue
        if direction == "drop" and z > 0:
            continue

        abs_z = abs(z)
        if abs_z >= z_critical:
            severity = "critical"
        elif abs_z >= z_warning:
            severity = "warning"
        else:
            continue

        change_dir = "spike" if z > 0 else "drop"
        pct_change = ((latest - mean) / mean * 100) if mean != 0 else 0

        anomalies.append(Anomaly(
            metric=metric_col,
            dimension=str(name),
            current_value=round(latest, 4),
            reference_value=round(mean, 4),
            severity=severity,
            direction=change_dir,
            explanation=(
                f"{metric_col} for {name} in {latest_period}: "
                f"{latest:.1%} ({change_dir} of {abs(pct_change):.0f}% vs "
                f"historical avg {mean:.1%}). Z-score: {z:+.1f}"
            ),
        ))

    return anomalies


# ============================================================================
# Threshold detection — for hard business rules
# ============================================================================

def threshold_detect(
    df: pd.DataFrame,
    metric_col: str,
    group_col: str = None,
    min_threshold: float = None,
    max_threshold: float = None,
    severity: str = "warning",
) -> list[Anomaly]:
    """
    Flag when a metric crosses a hard threshold.

    Examples:
      - adherence_rate must be >= 60%
      - no_action_rate must be <= 30%
      - dismissal_rate_total must be <= 40%
    """
    anomalies: list[Anomaly] = []

    if group_col and group_col in df.columns:
        for name, group in df.groupby(group_col):
            value = group[metric_col].iloc[-1] if len(group) > 0 else np.nan
            _check_threshold(anomalies, metric_col, str(name), value,
                           min_threshold, max_threshold, severity)
    else:
        value = df[metric_col].iloc[-1] if len(df) > 0 else np.nan
        _check_threshold(anomalies, metric_col, "overall", value,
                       min_threshold, max_threshold, severity)

    return anomalies


def _check_threshold(anomalies, metric, dimension, value, min_t, max_t, severity):
    if pd.isna(value):
        return

    if min_t is not None and value < min_t:
        anomalies.append(Anomaly(
            metric=metric,
            dimension=dimension,
            current_value=round(value, 4),
            reference_value=min_t,
            severity=severity,
            direction="drop",
            explanation=(
                f"{metric} for {dimension}: {value:.1%} is below "
                f"the minimum threshold of {min_t:.1%}"
            ),
        ))

    if max_t is not None and value > max_t:
        anomalies.append(Anomaly(
            metric=metric,
            dimension=dimension,
            current_value=round(value, 4),
            reference_value=max_t,
            severity=severity,
            direction="spike",
            explanation=(
                f"{metric} for {dimension}: {value:.1%} exceeds "
                f"the maximum threshold of {max_t:.1%}"
            ),
        ))


# ============================================================================
# Run all anomaly checks
# ============================================================================

def run_anomaly_detection(kpi_results: dict[str, pd.DataFrame]) -> list[Anomaly]:
    """
    Run all anomaly detection checks on KPI results.

    Args:
        kpi_results: Dict from kpi_engine.run_kpi_engine()

    Returns:
        List of Anomaly objects, sorted by severity (critical first)
    """
    logger.info("=" * 50)
    logger.info("ANOMALY DETECTION — START")
    logger.info("=" * 50)

    all_anomalies: list[Anomaly] = []

    # --- Z-score checks on monthly trends ---
    by_month = kpi_results.get("by_month", pd.DataFrame())
    if not by_month.empty and len(by_month) >= 3:
        for metric in ["adherence_rate", "acceptance_rate_total", "dismissal_rate_total", "no_action_rate"]:
            if metric in by_month.columns:
                anomalies = zscore_detect(by_month, metric, time_col="month")
                all_anomalies.extend(anomalies)

    # --- Z-score checks on country x month trends ---
    by_cm = kpi_results.get("by_country_month", pd.DataFrame())
    if not by_cm.empty:
        for metric in ["adherence_rate", "acceptance_rate_total", "no_action_rate"]:
            if metric in by_cm.columns:
                anomalies = zscore_detect(by_cm, metric, group_col="country", time_col="month")
                all_anomalies.extend(anomalies)

    # --- Threshold checks on country KPIs ---
    by_country = kpi_results.get("by_country", pd.DataFrame())
    if not by_country.empty:
        # Adherence should be >= 50%
        all_anomalies.extend(threshold_detect(
            by_country, "adherence_rate", group_col="country",
            min_threshold=0.50, severity="warning",
        ))
        # No action rate should be <= 35%
        all_anomalies.extend(threshold_detect(
            by_country, "no_action_rate", group_col="country",
            max_threshold=0.35, severity="warning",
        ))
        # Dismissal rate (total) should be <= 40%
        all_anomalies.extend(threshold_detect(
            by_country, "dismissal_rate_total", group_col="country",
            max_threshold=0.40, severity="critical",
        ))

    # --- Threshold checks on brand KPIs ---
    by_brand = kpi_results.get("by_brand", pd.DataFrame())
    if not by_brand.empty:
        # Adherence should be >= 50%
        all_anomalies.extend(threshold_detect(
            by_brand, "adherence_rate", group_col="medicine",
            min_threshold=0.50, severity="warning",
        ))
        # No action rate should be <= 35%
        all_anomalies.extend(threshold_detect(
            by_brand, "no_action_rate", group_col="medicine",
            max_threshold=0.35, severity="warning",
        ))

    # --- Sort: critical first, then warning ---
    severity_order = {"critical": 0, "warning": 1}
    all_anomalies.sort(key=lambda a: (severity_order.get(a.severity, 2), a.metric, a.dimension))

    # --- Log results ---
    critical = sum(1 for a in all_anomalies if a.severity == "critical")
    warnings = sum(1 for a in all_anomalies if a.severity == "warning")

    if all_anomalies:
        logger.info(f"Anomalies found: {critical} critical, {warnings} warning")
        for a in all_anomalies:
            icon = "🔴" if a.severity == "critical" else "🟡"
            logger.info(f"  {icon} {a.explanation}")
    else:
        logger.info("No anomalies detected — all metrics within normal range.")

    logger.info("=" * 50)
    logger.info(f"ANOMALY DETECTION COMPLETE — {len(all_anomalies)} anomaly(ies)")
    logger.info("=" * 50)

    return all_anomalies


def anomalies_to_dataframe(anomalies: list[Anomaly]) -> pd.DataFrame:
    """Convert anomaly list to a DataFrame for saving/display."""
    if not anomalies:
        return pd.DataFrame()
    return pd.DataFrame([
        {
            "severity": a.severity,
            "metric": a.metric,
            "dimension": a.dimension,
            "current_value": a.current_value,
            "reference_value": a.reference_value,
            "direction": a.direction,
            "explanation": a.explanation,
        }
        for a in anomalies
    ])
"""
ingest.py — Load, concatenate, deduplicate, and clean reporting extract files.

This replicates your manual notebook logic as a clean, reusable pipeline:

  Step 1+2: Load pipe-delimited .txt files and concatenate
  Step 3:   Strip whitespace on key columns
  Step 4:   Parse date columns (handles mixed formats)
  Step 5:   Deduplicate by GUID, keeping the latest row
  Step 6:   Merge suggestion type mapping (sugg_map.csv)
  Step 7:   Filter to actionable types only
  Step 8:   Split prod_name into country + medicine
  Step 9:   Derive KPI flags (is_accepted, is_dismissed, etc.)
  Step 10:  Clean dismissal reasons
  Step 11:  Save as Parquet + CSV
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================================
# Step 1+2: Load & Concat
# ============================================================================

def load_and_concat(file_paths: list[Path], cfg: dict) -> pd.DataFrame:
    """
    Read files and concatenate into one DataFrame.

    Auto-detects file format:
      - .parquet files → read with pd.read_parquet()
      - .txt/.csv files → read as pipe-delimited with latin-1 encoding
    """
    fmt = cfg.get("file_format", {})
    sep = fmt.get("separator", "|")
    encoding = fmt.get("encoding", "latin-1")
    quotechar = fmt.get("quotechar", '"')
    on_bad_lines = fmt.get("on_bad_lines", "skip")

    frames: list[pd.DataFrame] = []

    for path in file_paths:
        logger.info(f"Loading {path.name} ...")

        if path.suffix == ".parquet":
            # Parquet file (from S3 data lake)
            df = pd.read_parquet(path)
            # Convert all columns to string for consistent cleaning
            # (skip datetime columns — they're already properly typed)
            for col in df.columns:
                if df[col].dtype == "object":
                    df[col] = df[col].astype(str)
        else:
            # Pipe-delimited .txt/.csv file
            df = pd.read_csv(
                path,
                sep=sep,
                encoding=encoding,
                quotechar=quotechar,
                on_bad_lines=on_bad_lines,
                engine="python",
                dtype=str,
            )

        df["_source_file"] = path.name
        frames.append(df)
        logger.info(f"  -> {len(df):,} rows, {len(df.columns)} cols")

    if not frames:
        raise ValueError("No data files were loaded. Check your file paths.")

    combined = pd.concat(frames, ignore_index=True)
    logger.info(f"Concatenated: {len(combined):,} total rows from {len(frames)} file(s)")
    return combined


# ============================================================================
# Step 3: Strip whitespace
# ============================================================================

def strip_whitespace(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """
    Strip leading/trailing whitespace from specified columns.

    This fixes the issue from your notebook where
    record_type_name_vod__c sometimes had "  Suggestion_vod  "
    """
    for col in cols:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
    logger.info(f"Stripped whitespace on {len(cols)} column(s)")
    return df


# ============================================================================
# Step 4: Parse dates
# ============================================================================

def parse_dates(df: pd.DataFrame, date_cols: list[str]) -> pd.DataFrame:
    """
    Parse date columns robustly.

    Your real data has MIXED formats in the same column:
      - sugg_posted_date might be "2025-01-20" (YYYY-MM-DD)
      - response_date might be "01/20/2025" (MM/DD/YYYY)
    Some columns may also be tz-aware (from Parquet) — we strip timezone
    to keep everything consistent and avoid comparison errors.
    """
    for col in date_cols:
        if col not in df.columns:
            continue
        # Replace empty-ish strings with NaN before parsing
        df[col] = df[col].replace({"nan": np.nan, "": np.nan, "None": np.nan})
        df[col] = pd.to_datetime(df[col], format="mixed", dayfirst=False, errors="coerce")
        # Strip timezone info (real data has mixed tz-aware and tz-naive)
        if hasattr(df[col], "dt") and df[col].dt.tz is not None:
            df[col] = df[col].dt.tz_localize(None)
        n_parsed = df[col].notna().sum()
        logger.info(f"  Date '{col}': {n_parsed:,}/{len(df):,} parsed OK")
    return df


# ============================================================================
# Step 5: Dedup by GUID
# ============================================================================

def dedup_by_guid(df: pd.DataFrame, guid_col: str, date_cols: list[str]) -> pd.DataFrame:
    """
    Deduplicate rows sharing the same GUID.

    Matches your notebook Cell 2 exactly:
        rep_all["last_status_ts"] = rep_all[date_cols].max(axis=1)
        rep = rep_all.sort_values(["guid", "last_status_ts"])
                     .drop_duplicates(subset="guid", keep="last")
    """
    before = len(df)

    # Timezone stripping already done in parse_dates step
    # Compute last_status_ts = max of all date columns per row
    available = [c for c in date_cols if c in df.columns]
    if available:
        # Use numeric conversion to avoid Timestamp vs float comparison
        numeric_dates = pd.DataFrame()
        for col in available:
            numeric_dates[col] = pd.to_numeric(df[col], errors="coerce")
        df["last_status_ts"] = pd.to_datetime(numeric_dates.max(axis=1), errors="coerce")

        # Sort by guid + last_status_ts, keep LAST (most recent)
        df = df.sort_values([guid_col, "last_status_ts"], na_position="first")
        df = df.drop_duplicates(subset=guid_col, keep="last")
        df = df.drop(columns=["last_status_ts"])
    else:
        df = df.drop_duplicates(subset=guid_col, keep="last")

    after = len(df)
    logger.info(f"Dedup by '{guid_col}': {before:,} -> {after:,} ({before - after:,} duplicates removed)")
    return df.reset_index(drop=True)


# ============================================================================
# Step 6: Merge suggestion type mapping
# ============================================================================

def merge_suggestion_map(
    df: pd.DataFrame,
    map_path: Path,
    left_key: str,
    right_key: str,
) -> pd.DataFrame:
    """
    Left-join the suggestion type mapping file.

    This matches your notebook:
        sugg_df = pd.read_csv("sugg_map.csv")
        merged = rep.merge(sugg_df[["metric_sk", "suggestion_type"]],
                           left_on="metric_group_sk", right_on="metric_sk", how="left")
    """
    if not map_path.exists():
        logger.warning(f"Suggestion map file not found: {map_path} — skipping merge.")
        return df

    sugg_df = pd.read_csv(map_path)
    sugg_df = sugg_df.drop_duplicates(subset=right_key, keep="first")

    # Ensure merge keys are the same dtype
    df[left_key] = df[left_key].astype(str).str.strip()
    sugg_df[right_key] = sugg_df[right_key].astype(str).str.strip()

    merged = df.merge(
        sugg_df[[right_key, "suggestion_type"]],
        left_on=left_key,
        right_on=right_key,
        how="left",
    )
    logger.info(f"Suggestion type distribution:\n{merged['suggestion_type'].value_counts().to_string()}")
    return merged


# ============================================================================
# Step 7: Filter to actionable
# ============================================================================

def filter_actionable(df: pd.DataFrame, actionable_types: list[str]) -> pd.DataFrame:
    """
    Keep only rows where suggestion_type is actionable.

    This matches your notebook:
        actionable = merged[merged["suggestion_type"].isin(["Schedule a Call", "Send a VAE"])]
    """
    if "suggestion_type" not in df.columns:
        logger.warning("Column 'suggestion_type' missing — skipping filter.")
        return df

    mask = df["suggestion_type"].isin(actionable_types)
    result = df[mask].copy()
    logger.info(f"Filtered to actionable {actionable_types}: {len(df):,} -> {len(result):,} rows")
    return result.reset_index(drop=True)


# ============================================================================
# Step 8: Split prod_name -> country + medicine
# ============================================================================

def split_prod_name(df: pd.DataFrame) -> pd.DataFrame:
    """
    Split 'prod_name' on the first underscore.

    This matches your notebook:
        actionable[["country", "medicine"]] = actionable["prod_name"]
            .str.split("_", n=1, expand=True)

    Example: "GB_Verzenios" -> country="GB", medicine="Verzenios"
    """
    if "prod_name" not in df.columns:
        logger.warning("Column 'prod_name' not found — skipping split.")
        return df

    split = df["prod_name"].astype(str).str.split("_", n=1, expand=True)
    df["country"] = split[0] if 0 in split.columns else np.nan
    df["medicine"] = split[1] if 1 in split.columns else np.nan
    logger.info(f"Country distribution:\n{df['country'].value_counts().to_string()}")
    return df


# ============================================================================
# Step 9: Derive KPI flags
# ============================================================================

def derive_kpi_flags(df: pd.DataFrame, flag_cols: list[str]) -> pd.DataFrame:
    """
    Compute standard KPI funnel flags from raw Veeva action columns.

    This matches your UK notebook exactly:
        is_accepted  = (actioned_vod__c == 1) | (marked_as_complete_vod__c == 1)
        is_dismissed = (dismissed_vod__c == 1)
        is_adhered   = is_accepted | is_dismissed
        is_executed  = (actioned_vod__c == 1)
        is_ignored   = NOT is_adhered

    Definitions from SFE Glossary:
        Adherence = (Accepted + Dismissed) / Total
        Accepted  = Mark_As_Complete + Activity_Execution
        Executed  = Activity_Execution only
    """
    # Convert flag columns to numeric (they come as strings from our dtype=str load)
    for col in flag_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    # Derive flags
    df["is_accepted"] = (
        (df.get("actioned_vod__c", 0) == 1)
        | (df.get("marked_as_complete_vod__c", 0) == 1)
    ).astype(int)

    df["is_dismissed"] = (
        df.get("dismissed_vod__c", 0) == 1
    ).astype(int)

    df["is_adhered"] = (
        (df["is_accepted"] == 1) | (df["is_dismissed"] == 1)
    ).astype(int)

    df["is_executed"] = (
        df.get("actioned_vod__c", 0) == 1
    ).astype(int)

    df["is_ignored"] = (
        df["is_adhered"] == 0
    ).astype(int)

    logger.info(
        f"KPI flags -> Accepted: {df['is_accepted'].sum():,}, "
        f"Dismissed: {df['is_dismissed'].sum():,}, "
        f"Ignored: {df['is_ignored'].sum():,}"
    )
    return df


# ============================================================================
# Step 10: Clean dismissal reasons
# ============================================================================

def clean_dismissal_reasons(df: pd.DataFrame) -> pd.DataFrame:
    """
    Strip leading number prefix and trailing period from dismissal reasons.

    This matches your UK notebook:
        dismissed_df["reason_clean"] = dismissed_df[reason_col]
            .apply(lambda x: re.sub(r"^\d+\.\s*", "", str(x)).strip().rstrip("."))

    Example: "1. Already planned interaction with customer." -> "Already planned interaction with customer"
    """
    col = "survey_dismissal_answer_1"
    if col not in df.columns:
        return df

    df["dismissal_reason_clean"] = (
        df[col]
        .fillna("")
        .apply(lambda x: re.sub(r"^\d+\.\s*", "", str(x)).strip().rstrip("."))
        .replace("", np.nan)
    )
    n_reasons = df["dismissal_reason_clean"].notna().sum()
    logger.info(f"Cleaned dismissal reasons: {n_reasons:,} non-null entries")
    return df


# ============================================================================
# Step 11: Save outputs
# ============================================================================

def save_outputs(df: pd.DataFrame, output_cfg: dict) -> dict[str, Path]:
    """Save the cleaned DataFrame as Parquet (primary) and CSV (backup)."""
    out_dir = Path(output_cfg["processed_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    parquet_path = out_dir / output_cfg["parquet_file"]
    csv_path = out_dir / output_cfg["csv_backup"]

    df.to_parquet(parquet_path, index=False, engine="pyarrow")
    logger.info(f"Saved: {parquet_path}  ({parquet_path.stat().st_size / 1024:.0f} KB)")

    df.to_csv(csv_path, index=False)
    logger.info(f"Saved: {csv_path}  ({csv_path.stat().st_size / 1024:.0f} KB)")

    return {"parquet": parquet_path, "csv": csv_path}


# ============================================================================
# Full pipeline orchestrator
# ============================================================================

def run_ingestion(file_paths: list[Path], config: dict) -> pd.DataFrame:
    """
    Run the complete ingestion pipeline end-to-end.

    Args:
        file_paths: Local paths to downloaded extract files.
        config: Full pipeline config dict (from config.yaml).

    Returns:
        Cleaned, actionable DataFrame ready for KPI computation.
    """
    col_cfg = config.get("columns", {})
    sugg_cfg = config.get("suggestion_map", {})
    out_cfg = config.get("output", {})

    logger.info("=" * 50)
    logger.info("INGESTION PIPELINE — START")
    logger.info("=" * 50)

    # Step 1+2: Load & concat
    df = load_and_concat(file_paths, config)

    # Step 3: Strip whitespace
    df = strip_whitespace(df, col_cfg.get("strip_cols", []))

    # Step 4: Parse dates
    df = parse_dates(df, col_cfg.get("date_cols", []))

    # Step 5: Dedup by GUID
    guid_col = col_cfg.get("guid", "suggestion_external_id_vod__c")
    df = dedup_by_guid(df, guid_col, col_cfg.get("date_cols", []))

    # Step 6: Merge suggestion type map (if file exists)
    map_path = Path(sugg_cfg.get("source_file", "sugg_map.csv"))
    if map_path.exists():
        df = merge_suggestion_map(
            df,
            map_path,
            left_key=sugg_cfg.get("left_key", "metric_group_sk"),
            right_key=sugg_cfg.get("right_key", "metric_sk"),
        )

        # Step 7: Filter to actionable
        df = filter_actionable(df, sugg_cfg.get("actionable_types", []))

    # Step 8: Split prod_name
    df = split_prod_name(df)

    # Step 9: Derive KPI flags
    df = derive_kpi_flags(df, col_cfg.get("flag_cols", []))

    # Step 10: Clean dismissal reasons
    df = clean_dismissal_reasons(df)

    # Step 11: Save
    if out_cfg:
        save_outputs(df, out_cfg)

    logger.info("=" * 50)
    logger.info(f"INGESTION COMPLETE: {len(df):,} rows, {len(df.columns)} columns")
    logger.info("=" * 50)

    return df
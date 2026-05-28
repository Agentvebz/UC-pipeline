"""
analysis.py — Main entry point for the UC Performance analysis.

Orchestrates:
  Phase 1: Fetch files from SharePoint / S3 / Local
  Phase 2: Ingest, clean, deduplicate, derive KPI flags
  Phase 3: KPI computation
  Phase 4: Anomaly detection
  Phase 5: PPTX report generation

Usage:
    python -m src.analysis --source s3_parquet                    # All IBU countries
    python -m src.analysis --source s3_parquet --country GB       # UK only
    python -m src.analysis --source s3_parquet --country FR       # France only
    python -m src.analysis --source s3_parquet --country all      # Same as no flag
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from src.data_source import get_data_source
from src.ingest import run_ingestion
from src.kpi_engine import run_kpi_engine
from src.anomaly_detection import run_anomaly_detection, anomalies_to_dataframe
from src.report_generator import run_report_generator

logger = logging.getLogger("uc_analysis")

COUNTRY_NAMES = {
    "DE": "Germany",
    "ES": "Spain",
    "FR": "France",
    "GB": "United Kingdom",
    "IT": "Italy",
    "PL": "Poland",
    "CA": "Canada",
    "JP": "Japan",
    "SA": "Saudi Arabia",
    "AE": "UAE",
    "CN": "China",
}


def load_config(config_path: str = "config/config.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    logging.basicConfig(
        level=getattr(logging, log_cfg.get("level", "INFO")),
        format=log_cfg.get("format", "%(asctime)s | %(name)s | %(levelname)s | %(message)s"),
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def run(config: dict) -> None:
    setup_logging(config)

    country_filter = config.get("_country_filter", None)
    country_label = "IBU (All Countries)"
    if country_filter:
        country_name = COUNTRY_NAMES.get(country_filter, country_filter)
        country_label = f"{country_name} ({country_filter})"

    logger.info("=" * 60)
    logger.info("UC PERFORMANCE ANALYSIS — STARTING")
    logger.info(f"  Scope: {country_label}")
    logger.info("=" * 60)

    # --- Phase 1: Fetch files ---
    source_type = config.get("data_source", "local")
    logger.info(f"Data source: {source_type.upper()}")

    source = get_data_source(config)
    staging_dir = Path(config.get("output", {}).get("staging_dir", "./data/staging"))

    file_paths = source.download_all(staging_dir)
    if not file_paths:
        logger.error("No files downloaded. Analysis cannot proceed.")
        sys.exit(1)

    logger.info(f"Downloaded {len(file_paths)} file(s) to {staging_dir}")

    # --- Phase 2: Ingest & Clean ---
    df = run_ingestion(file_paths, config)

    # --- Country Filter ---
    if country_filter and "country" in df.columns:
        before = len(df)
        df = df[df["country"] == country_filter].copy()
        after = len(df)
        logger.info(f"Country filter '{country_filter}': {before:,} -> {after:,} rows")
        if after == 0:
            logger.error(f"No data for country '{country_filter}'. Available: {sorted(df['country'].unique()) if before > 0 else 'none'}")
            sys.exit(1)

    # Store country info in config for report generator
    config["_country_filter"] = country_filter
    config["_country_label"] = country_label
    config["_country_name"] = COUNTRY_NAMES.get(country_filter, country_filter) if country_filter else None

    # --- Phase 3: Compute KPIs ---
    if len(df) > 0:
        # Set output dir — add country subfolder if filtering
        base_output = config.get("output", {}).get("processed_dir", "./data/processed")
        if country_filter:
            output_dir = str(Path(base_output) / country_filter)
        else:
            output_dir = base_output
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        # Save the (possibly country-filtered) data for report generator
        filtered_parquet = Path(output_dir) / config.get("output", {}).get("parquet_file", "actionable_suggestions.parquet")
        df.to_parquet(str(filtered_parquet), index=False)
        logger.info(f"Saved filtered data: {filtered_parquet} ({len(df):,} rows)")

        kpi_results = run_kpi_engine(df, output_dir=output_dir)

        # --- Phase 4: Anomaly Detection ---
        anomalies = run_anomaly_detection(kpi_results)
        if anomalies:
            anomaly_df = anomalies_to_dataframe(anomalies)
            anomaly_path = Path(output_dir) / "kpi_anomalies.csv"
            anomaly_df.to_csv(anomaly_path, index=False)
            logger.info(f"  Saved: {anomaly_path}")
        else:
            anomalies = []

        # --- Phase 5: Report Generation ---
        run_report_generator(kpi_results, anomalies, config, output_dir=output_dir)
    else:
        logger.warning("No data after ingestion — skipping KPI computation.")
        kpi_results = {}

    # --- Summary ---
    logger.info("")
    logger.info("=" * 60)
    logger.info(f"ANALYSIS COMPLETE — {country_label}")
    logger.info("=" * 60)
    logger.info(f"  Total rows:  {len(df):,}")
    logger.info(f"  Columns:     {len(df.columns)}")
    if "country" in df.columns:
        logger.info(f"  Countries:   {sorted(df['country'].dropna().unique())}")
    if "medicine" in df.columns:
        logger.info(f"  Medicines:   {sorted(df['medicine'].dropna().unique())}")
    if "is_accepted" in df.columns:
        logger.info(f"  Accepted:    {df['is_accepted'].sum():,}  ({df['is_accepted'].mean():.1%})")
        logger.info(f"  Dismissed:   {df['is_dismissed'].sum():,}  ({df['is_dismissed'].mean():.1%})")
        logger.info(f"  Ignored:     {df['is_ignored'].sum():,}  ({df['is_ignored'].mean():.1%})")
    if country_filter:
        logger.info(f"  Output dir:  data/processed/{country_filter}/")
    logger.info("=" * 60)

    return df


def main():
    parser = argparse.ArgumentParser(description="UC Performance Analysis")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config YAML")
    parser.add_argument("--source", choices=["sharepoint", "sharepoint_onprem", "s3_parquet", "s3", "local"],
                        help="Override data source")
    parser.add_argument("--country", type=str, default=None,
                        help="Filter to a specific country code (e.g. GB, FR, DE, IT, ES, PL) or 'all' for IBU-wide")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.source:
        config["data_source"] = args.source

    # Handle country filter
    if args.country and args.country.upper() != "ALL":
        config["_country_filter"] = args.country.upper()
    else:
        config["_country_filter"] = None

    run(config)


if __name__ == "__main__":
    main()
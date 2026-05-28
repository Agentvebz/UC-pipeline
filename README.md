# UC Performance Analysis Pipeline

Automated reporting pipeline for **IBU NBA/E Suggestion Performance** analysis. Reads from the Lilly S3 data lake, computes KPIs, detects anomalies, and generates branded PowerPoint reports — all on demand through a simple web form.

## What it does

1. Pulls actionable suggestion data from the S3 data lake (Parquet)
2. Cleans, deduplicates, and filters to actionable suggestions
3. Computes 14 KPIs across 11 dimensions (brand, use case, country, month)
4. Detects anomalies (z-score + threshold based)
5. Generates a branded PPTX report matching the IBU template

## How to use (for the team)

1. Open the web form: **https://Agentvebz.github.io/UC-pipeline/**
2. Select a **country** (or All IBU) and a **date range**
3. Click **Generate Report**
4. Wait 2-3 minutes, then download the PPTX

No login, no setup, no AWS access required — just a browser.

## Architecture

```
GitHub Pages (web form)
    -> GitHub Actions (workflow_dispatch)
    -> OIDC auth with AWS (no stored keys)
    -> Python pipeline reads S3, computes KPIs, builds PPTX
    -> PPTX available as downloadable artifact
```

## Report contents

| Slide | Content |
|-------|---------|
| Cover | Country, assessment period |
| Executive View | HCP/HCO summaries, salesforce breakdown |
| Adherence Rate | Adherence rate by product |
| Product Performance | Acceptance vs Dismissal + dismissal reasons |
| Use Case Heatmap | Product x Use Case acceptance heatmap |
| Use Case Detail | Per-brand use case performance + key callouts |
| Anomalies | Flagged metrics outside normal range |
| Appendix | Metric definitions + scope |

## Project structure

```
UC-pipeline/
├── .github/workflows/generate_report.yml   # CI pipeline
├── docs/index.html                          # Web form (GitHub Pages)
├── config/config.yaml                       # Data source + settings
├── src/
│   ├── data_source.py                       # S3 Parquet ingestion
│   ├── ingest.py                            # ETL pipeline
│   ├── kpi_engine.py                        # KPI computation
│   ├── anomaly_detection.py                 # Anomaly detection
│   ├── report_generator.py                  # PPTX generation
│   ├── slide_layout.py                      # Dynamic layout engine
│   └── analysis.py                          # CLI orchestrator
├── IBU_template.pptx                        # Branded template
├── sugg_map.csv                             # Suggestion type mapping
└── requirements.txt
```

## Running locally (developers)

```bash
pip install -r requirements.txt
aws sso login --profile lilly
python -m src.analysis --source s3_parquet --country GB
```

Output is saved to `data/processed/GB/performance_report_GB.pptx`.

## Configuration

Data source, date range, S3 bucket, and KPI thresholds are configured in `config/config.yaml`.

---

*IBU Omnichannel Analytics | Company Confidential © 2026 Eli Lilly and Company*
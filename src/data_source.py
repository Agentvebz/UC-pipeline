"""
data_source.py — Fetch reporting extract files from SharePoint, S3, or local disk.

Each source implements the same interface:
    list_files()   -> list of file metadata dicts
    download(file_meta, dest_dir) -> local Path

Usage:
    source = get_data_source(config)
    files  = source.list_files()
    paths  = [source.download(f, staging_dir) for f in files]
"""

from __future__ import annotations

import abc
import fnmatch
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ============================================================================
# Abstract base — defines the interface all sources must follow
# ============================================================================

class DataSource(abc.ABC):
    """Common interface every data source must implement."""

    @abc.abstractmethod
    def list_files(self) -> list[dict[str, Any]]:
        """Return metadata dicts for each file matching the configured pattern."""

    @abc.abstractmethod
    def download(self, file_meta: dict[str, Any], dest_dir: Path) -> Path:
        """Download a single file to dest_dir and return its local path."""

    def download_all(self, dest_dir: Path) -> list[Path]:
        """Convenience: download every file that list_files() finds."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        files = self.list_files()
        if not files:
            logger.warning("No files found matching the configured pattern.")
            return []
        logger.info(f"Found {len(files)} file(s) to download.")
        paths: list[Path] = []
        for f in files:
            p = self.download(f, dest_dir)
            paths.append(p)
            logger.info(f"  Downloaded: {p.name}  ({p.stat().st_size / 1024:.0f} KB)")
        return paths


# ============================================================================
# SharePoint via Microsoft Graph API
# ============================================================================

class SharePointSource(DataSource):
    """
    Fetch files from a SharePoint document library using the Graph API.

    Auth: Client-credentials flow (Entra ID app registration).
    Requires: pip install requests
    """

    TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    GRAPH_BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, cfg: dict):
        self.tenant_id = os.path.expandvars(cfg["tenant_id"])
        self.client_id = os.path.expandvars(cfg["client_id"])
        self.client_secret = os.path.expandvars(cfg["client_secret"])
        self.site_name = cfg["site_name"]
        self.drive_name = cfg.get("drive_name", "Documents")
        self.folder_path = cfg["folder_path"]
        self.pattern = cfg.get("file_pattern", "*.txt")
        self._token: str | None = None

    # ----- Authentication -----

    def _get_token(self) -> str:
        """Acquire an app-only access token via client credentials."""
        if self._token:
            return self._token
        import requests

        url = self.TOKEN_URL.format(tenant=self.tenant_id)
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
        }
        resp = requests.post(url, data=data, timeout=30)
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        logger.info("SharePoint: Access token acquired.")
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # ----- Graph API helpers -----

    def _get_site_id(self) -> str:
        """Find the SharePoint site ID by name."""
        import requests
        url = f"{self.GRAPH_BASE}/sites?search={self.site_name}"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        sites = resp.json().get("value", [])
        if not sites:
            raise ValueError(f"SharePoint site '{self.site_name}' not found.")
        site_id = sites[0]["id"]
        logger.info(f"SharePoint: Found site '{self.site_name}' -> {site_id}")
        return site_id

    def _get_drive_id(self, site_id: str) -> str:
        """Find the document library (drive) ID within the site."""
        import requests
        url = f"{self.GRAPH_BASE}/sites/{site_id}/drives"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        for d in resp.json().get("value", []):
            if d["name"] == self.drive_name:
                logger.info(f"SharePoint: Found drive '{self.drive_name}' -> {d['id']}")
                return d["id"]
        raise ValueError(f"Drive '{self.drive_name}' not found in site.")

    def _list_folder(self, drive_id: str) -> list[dict]:
        """List all items in the configured folder, handling pagination."""
        import requests
        url = f"{self.GRAPH_BASE}/drives/{drive_id}/root:/{self.folder_path}:/children"
        items: list[dict] = []
        while url:
            resp = requests.get(url, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")  # pagination
        return items

    # ----- Interface methods -----

    def list_files(self) -> list[dict[str, Any]]:
        site_id = self._get_site_id()
        drive_id = self._get_drive_id(site_id)
        items = self._list_folder(drive_id)
        matched = []
        for item in items:
            if "file" not in item:
                continue  # skip subfolders
            name = item["name"]
            if fnmatch.fnmatch(name, self.pattern):
                matched.append({
                    "name": name,
                    "id": item["id"],
                    "drive_id": drive_id,
                    "size": item.get("size", 0),
                    "modified": item.get("lastModifiedDateTime"),
                    "download_url": item.get("@microsoft.graph.downloadUrl"),
                })
        logger.info(f"SharePoint: {len(matched)} file(s) match '{self.pattern}'")
        return sorted(matched, key=lambda x: x["name"])

    def download(self, file_meta: dict[str, Any], dest_dir: Path) -> Path:
        import requests
        dest = dest_dir / file_meta["name"]
        # Use the pre-signed download URL if available
        url = file_meta.get("download_url")
        if not url:
            # Fallback: request content via Graph endpoint
            url = (
                f"{self.GRAPH_BASE}/drives/{file_meta['drive_id']}"
                f"/items/{file_meta['id']}/content"
            )
        resp = requests.get(url, headers=self._headers(), timeout=120, stream=True)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return dest


# ============================================================================
# AWS S3
# ============================================================================

class S3Source(DataSource):
    """
    Fetch files from an S3 bucket using boto3.
    Requires: pip install boto3
    """

# ============================================================================
# AWS S3 — Parquet (Hive-partitioned, for Lilly data lake)
# ============================================================================

class S3ParquetSource(DataSource):
    """
    Read Hive-partitioned Parquet data from Lilly's S3 data lake.

    Uses boto3 to list only the date partitions we need (fast),
    downloads just those Parquet files, then reads locally.

    This avoids PyArrow scanning the entire dataset (which hangs
    on large datasets with junk folders like _SUCCESS, backups, etc.)

    Requires: pip install boto3 pyarrow
    """

    def __init__(self, cfg: dict):
        self.bucket = cfg["bucket"]
        self.prefix = cfg.get("prefix", "").strip("/") + "/"
        self.region = cfg.get("region", "us-east-1")
        self.start_date = cfg.get("start_date", "2025-01-01")
        self.end_date = cfg.get("end_date", "2025-12-31")
        self.profile = cfg.get("profile", None)  # AWS SSO profile name
        self._client = None

    def _s3(self):
        if self._client is None:
            import boto3
            if self.profile:
                # Use SSO profile — auto-refreshes tokens
                session = boto3.Session(profile_name=self.profile, region_name=self.region)
                self._client = session.client("s3")
                logger.info(f"S3 Parquet: Using AWS SSO profile '{self.profile}'")
            else:
                # Fallback to environment variables
                self._client = boto3.client("s3", region_name=self.region)
        return self._client

    def _list_date_partitions(self) -> list[str]:
        """
        List all generated_date=YYYY-MM-DD/ partitions using boto3.
        Filter to only those within our date range.
        """
        paginator = self._s3().get_paginator("list_objects_v2")
        dates_found: list[str] = []

        for page in paginator.paginate(
            Bucket=self.bucket,
            Prefix=self.prefix + "generated_date=",
            Delimiter="/",
        ):
            for cp in page.get("CommonPrefixes", []):
                prefix = cp["Prefix"]
                # Extract date from "...generated_date=2025-01-06/"
                for part in prefix.rstrip("/").split("/"):
                    if part.startswith("generated_date="):
                        date_str = part.split("=")[1]
                        if self.start_date <= date_str <= self.end_date:
                            dates_found.append(date_str)

        return sorted(dates_found)

    def _list_parquet_files(self, date_str: str) -> list[str]:
        """List all .parquet files inside a specific date partition."""
        partition_prefix = f"{self.prefix}generated_date={date_str}/"
        keys: list[str] = []

        paginator = self._s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=partition_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".parquet"):
                    keys.append(key)

        return keys

    def list_files(self) -> list[dict[str, Any]]:
        """List date partitions within our date range."""
        logger.info(f"S3 Parquet: Listing partitions in s3://{self.bucket}/{self.prefix}")
        logger.info(f"S3 Parquet: Date filter {self.start_date} to {self.end_date}")

        dates = self._list_date_partitions()
        logger.info(f"S3 Parquet: Found {len(dates)} date partition(s) in range")

        if dates:
            logger.info(f"S3 Parquet: First: {dates[0]}, Last: {dates[-1]}")

        return [{"name": d, "date": d} for d in dates]

    def download(self, file_meta: dict[str, Any], dest_dir: Path) -> Path:
        """Download all Parquet files for a single date partition."""
        date_str = file_meta["date"]
        parquet_keys = self._list_parquet_files(date_str)

        if not parquet_keys:
            logger.warning(f"S3 Parquet: No .parquet files in generated_date={date_str}/")
            return dest_dir / f"empty_{date_str}.parquet"

        # Download each parquet file and concat
        import pandas as pd
        frames: list[pd.DataFrame] = []

        for key in parquet_keys:
            filename = key.split("/")[-1]
            local_path = dest_dir / f"{date_str}_{filename}"
            self._s3().download_file(self.bucket, key, str(local_path))
            df = pd.read_parquet(local_path)
            df["generated_date"] = date_str
            frames.append(df)
            local_path.unlink()  # Clean up individual file

        combined = pd.concat(frames, ignore_index=True)
        dest = dest_dir / f"extract_{date_str}.parquet"
        combined.to_parquet(dest, index=False)
        return dest

    def download_all(self, dest_dir: Path) -> list[Path]:
        """Download all date partitions and merge into one file."""
        import pandas as pd

        dest_dir.mkdir(parents=True, exist_ok=True)
        date_metas = self.list_files()

        if not date_metas:
            logger.warning("S3 Parquet: No partitions found in date range.")
            return []

        logger.info(f"S3 Parquet: Downloading {len(date_metas)} partition(s)...")

        all_frames: list[pd.DataFrame] = []
        for i, meta in enumerate(date_metas, 1):
            date_str = meta["date"]
            logger.info(f"  [{i}/{len(date_metas)}] generated_date={date_str}")

            parquet_keys = self._list_parquet_files(date_str)
            for key in parquet_keys:
                filename = key.split("/")[-1]
                local_path = dest_dir / f"temp_{date_str}_{filename}"

                self._s3().download_file(self.bucket, key, str(local_path))
                df = pd.read_parquet(local_path)
                df["generated_date"] = date_str
                all_frames.append(df)
                local_path.unlink()  # Clean up temp file

        if not all_frames:
            logger.warning("S3 Parquet: No data found in any partition.")
            return []

        combined = pd.concat(all_frames, ignore_index=True)
        logger.info(f"S3 Parquet: Total loaded: {len(combined):,} rows, {len(combined.columns)} columns")

        # Save as single Parquet
        dest = dest_dir / "reporting_extract_from_s3.parquet"
        combined.to_parquet(dest, index=False)
        logger.info(f"S3 Parquet: Saved to {dest} ({dest.stat().st_size / 1024:.0f} KB)")

        return [dest]


# ============================================================================
# AWS S3 — Raw files (original, for .txt extracts)
# ============================================================================

class S3Source(DataSource):
    """Fetch raw files from an S3 bucket using boto3."""

    def __init__(self, cfg: dict):
        self.bucket = cfg["bucket"]
        self.prefix = cfg.get("prefix", "")
        self.region = cfg.get("region", "us-east-1")
        self.pattern = cfg.get("file_pattern", "*.txt")
        self._client = None

    def _s3(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("s3", region_name=self.region)
        return self._client

    def list_files(self) -> list[dict[str, Any]]:
        paginator = self._s3().get_paginator("list_objects_v2")
        matched: list[dict] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                name = key.rsplit("/", 1)[-1]
                if fnmatch.fnmatch(name, self.pattern):
                    matched.append({
                        "name": name,
                        "key": key,
                        "size": obj.get("Size", 0),
                        "modified": str(obj.get("LastModified", "")),
                    })
        logger.info(f"S3: {len(matched)} file(s) match '{self.pattern}' in s3://{self.bucket}/{self.prefix}")
        return sorted(matched, key=lambda x: x["name"])

    def download(self, file_meta: dict[str, Any], dest_dir: Path) -> Path:
        dest = dest_dir / file_meta["name"]
        self._s3().download_file(self.bucket, file_meta["key"], str(dest))
        return dest


# ============================================================================
# Local filesystem (for development / testing)
# ============================================================================

class LocalSource(DataSource):
    """Read files from a local directory."""

    def __init__(self, cfg: dict):
        self.folder = Path(cfg["folder"])
        self.pattern = cfg.get("file_pattern", "*.txt")

    def list_files(self) -> list[dict[str, Any]]:
        if not self.folder.exists():
            logger.warning(f"Local folder does not exist: {self.folder}")
            return []
        matched = []
        for p in sorted(self.folder.iterdir()):
            if p.is_file() and fnmatch.fnmatch(p.name, self.pattern):
                matched.append({
                    "name": p.name,
                    "path": str(p),
                    "size": p.stat().st_size,
                })
        logger.info(f"Local: {len(matched)} file(s) match '{self.pattern}' in {self.folder}")
        return matched

    def download(self, file_meta: dict[str, Any], dest_dir: Path) -> Path:
        src = Path(file_meta["path"])
        dest = dest_dir / file_meta["name"]
        if src != dest:
            shutil.copy2(src, dest)
        return dest


# ============================================================================
# SharePoint On-Premises (Lilly collab.lilly.com)
# ============================================================================

class SharePointOnPremSource(DataSource):
    """
    Fetch files from SharePoint On-Premises using the REST API.

    Auth: NTLM (Windows integrated auth) — uses your Windows login
    automatically, same as when your browser opens collab.lilly.com.

    Requires: pip install requests requests-ntlm

    Config example:
        sharepoint_onprem:
          site_url: "https://collab.lilly.com/sites/IBUNBANBE-workingspace"
          folder_path: "01-Phase 1/IBU ZCE Operations/ZCE Output Files/Commercial/M5"
          file_pattern: "LLY3_REPORTING_EXTRACT_*.txt"
          username: ""     # Leave empty to use Windows login
          password: ""     # Leave empty to use Windows login
    """

    def __init__(self, cfg: dict):
        self.site_url = cfg["site_url"].rstrip("/")
        self.folder_path = cfg["folder_path"]
        self.pattern = cfg.get("file_pattern", "*.txt")
        self.username = cfg.get("username", "")
        self.password = cfg.get("password", "")
        self._session = None

    def _get_session(self):
        """Create a requests session with NTLM auth (Windows integrated)."""
        if self._session:
            return self._session

        import requests
        from requests_ntlm import HttpNtlmAuth

        self._session = requests.Session()

        # Priority: 1) config values, 2) env vars, 3) auto-detect
        username = self.username or os.environ.get("SP_USERNAME", "")
        password = self.password or os.environ.get("SP_PASSWORD", "")

        if username and password:
            # Explicit credentials — make sure domain is included
            if "\\" not in username and "/" not in username:
                username = f"LILLY\\{username}"
            self._session.auth = HttpNtlmAuth(username, password)
            logger.info(f"SharePoint On-Prem: Using credentials for {username}")
        else:
            # Auto-detect from Windows environment
            import getpass
            win_user = os.environ.get("USERNAME", getpass.getuser())
            domain = "LILLY"
            ntlm_user = f"{domain}\\{win_user}"
            logger.warning(
                f"SharePoint On-Prem: No password provided. Trying {ntlm_user} with empty password. "
                f"If this fails with 403, set SP_USERNAME and SP_PASSWORD environment variables."
            )
            self._session.auth = HttpNtlmAuth(ntlm_user, "")

        # Set headers that SharePoint expects
        self._session.headers.update({
            "Accept": "application/json;odata=verbose",
        })

        return self._session

    def _api_url(self, endpoint: str) -> str:
        """Build a SharePoint REST API URL."""
        return f"{self.site_url}/_api/web/{endpoint}"

    def _get_folder_url(self) -> str:
        """Build the REST API URL for the folder contents."""
        # SharePoint REST API path for Shared Documents library
        sp_path = f"Shared Documents/{self.folder_path}"
        encoded_path = sp_path.replace(" ", "%20")
        return self._api_url(
            f"GetFolderByServerRelativeUrl('/sites/"
            f"{self.site_url.split('/sites/')[1]}/Shared%20Documents/"
            f"{encoded_path}')/Files"
        )

    def list_files(self) -> list[dict[str, Any]]:
        """List all files in the configured folder matching the pattern."""
        session = self._get_session()

        # Build the server-relative URL for the folder
        site_path = self.site_url.split("collab.lilly.com")[1]
        folder_server_path = f"{site_path}/Shared Documents/{self.folder_path}"

        url = self._api_url(
            f"GetFolderByServerRelativeUrl('{folder_server_path}')/Files"
        )

        logger.info(f"SharePoint On-Prem: Listing files at {folder_server_path}")
        logger.info(f"SharePoint On-Prem: Full URL = {url}")

        resp = session.get(url, timeout=30)

        # Debug: show what SharePoint returned if it fails
        if resp.status_code != 200:
            logger.error(f"SharePoint returned {resp.status_code}")
            logger.error(f"Response body: {resp.text[:500]}")

            # Try alternate approach: maybe the site needs a different path format
            # Some SharePoint on-prem sites use different relative URL structures
            logger.info("Trying alternate URL format...")

            # Try without the leading /sites/ prefix in the relative URL
            alt_url = self._api_url(
                f"GetFolderByServerRelativeUrl("
                f"'/Shared Documents/{self.folder_path}')/Files"
            )
            logger.info(f"SharePoint On-Prem: Trying alt URL = {alt_url}")
            resp = session.get(alt_url, timeout=30)

            if resp.status_code != 200:
                logger.error(f"Alt URL also returned {resp.status_code}")
                logger.error(f"Response body: {resp.text[:500]}")

                # Last try: use the Lists/Documents approach
                logger.info("Trying Lists API approach...")
                list_url = (
                    f"{self.site_url}/_api/web/lists/"
                    f"GetByTitle('Documents')/items"
                    f"?$select=FileLeafRef,FileRef,File_x0020_Size,Modified"
                    f"&$filter=startswith(FileRef,'{folder_server_path}')"
                )
                logger.info(f"SharePoint On-Prem: Trying list URL = {list_url}")
                resp = session.get(list_url, timeout=30)

                if resp.status_code != 200:
                    logger.error(f"Lists API also returned {resp.status_code}")
                    logger.error(f"Response: {resp.text[:500]}")
                    resp.raise_for_status()

        data = resp.json()

        # SharePoint REST API returns results in d.results
        files_data = data.get("d", {}).get("results", [])

        matched = []
        for item in files_data:
            name = item.get("Name", item.get("FileLeafRef", ""))
            if fnmatch.fnmatch(name, self.pattern):
                matched.append({
                    "name": name,
                    "server_relative_url": item.get("ServerRelativeUrl", item.get("FileRef", "")),
                    "size": item.get("Length", item.get("File_x0020_Size", 0)),
                    "modified": item.get("TimeLastModified", item.get("Modified", "")),
                })

        logger.info(f"SharePoint On-Prem: {len(matched)} file(s) match '{self.pattern}'")
        return sorted(matched, key=lambda x: x["name"])

    def download(self, file_meta: dict[str, Any], dest_dir: Path) -> Path:
        """Download a single file from SharePoint."""
        session = self._get_session()
        dest = dest_dir / file_meta["name"]

        # Use the file content endpoint
        server_rel_url = file_meta["server_relative_url"]
        url = (
            f"{self.site_url}/_api/web/GetFileByServerRelativeUrl"
            f"('{server_rel_url}')/$value"
        )

        resp = session.get(url, timeout=120, stream=True)
        resp.raise_for_status()

        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        return dest


# ============================================================================
# Factory — picks the right source based on config
# ============================================================================

def get_data_source(config: dict) -> DataSource:
    """
    Return the right DataSource based on config['data_source'].

    Args:
        config: The full pipeline config dict (parsed from config.yaml).
    """
    source_type = config.get("data_source", "local").lower()

    if source_type == "sharepoint":
        return SharePointSource(config["sharepoint"])
    elif source_type == "sharepoint_onprem":
        return SharePointOnPremSource(config["sharepoint_onprem"])
    elif source_type == "s3_parquet":
        return S3ParquetSource(config["s3_parquet"])
    elif source_type == "s3":
        return S3Source(config["s3"])
    elif source_type == "local":
        return LocalSource(config["local"])
    else:
        raise ValueError(
            f"Unknown data_source: '{source_type}'. "
            f"Use 'sharepoint', 'sharepoint_onprem', 's3_parquet', 's3', or 'local'."
        )
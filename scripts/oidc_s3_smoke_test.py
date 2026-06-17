#!/usr/bin/env python3
"""OIDC -> S3 smoke test. Run only inside GitHub Actions after configure-aws-credentials.
Confirms: (1) we have an assumed-role identity, (2) we can list S3, (3) we can read a parquet."""
import os
import sys
import boto3
import pandas as pd

BUCKET = os.environ["SMOKE_BUCKET"]
PREFIX = os.environ.get("SMOKE_PREFIX", "") or ""
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def fail(msg, exc=None):
    print(f"FAIL: {msg}")
    if exc is not None:
        print(f"  -> {type(exc).__name__}: {exc}")
    sys.exit(1)


def discover_parquet(s3, bucket, start_prefix="", max_prefixes=400, max_depth=6):
    """Bounded breadth-first search for the first .parquet key. S3 has no real
    folders, so we walk CommonPrefixes (delimited by '/') one level at a time."""
    from collections import deque
    queue = deque([(start_prefix, 0)])
    explored, seen = [], 0
    paginator = s3.get_paginator("list_objects_v2")
    while queue and seen < max_prefixes:
        prefix, depth = queue.popleft()
        seen += 1
        explored.append(prefix or "<root>")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for obj in page.get("Contents", []):
                if obj["Key"].lower().endswith(".parquet"):
                    return obj["Key"], explored
            if depth < max_depth:
                for cp in page.get("CommonPrefixes", []):
                    queue.append((cp["Prefix"], depth + 1))
    return None, explored


def main():
    # 1. Identity — confirm we are the assumed role, not a static user
    try:
        ident = boto3.client("sts", region_name=REGION).get_caller_identity()
        print(f"PASS identity: {ident['Arn']}")
        if "assumed-role" not in ident["Arn"]:
            print("  WARNING: ARN is not an assumed-role; OIDC assume may not have happened.")
    except Exception as e:
        fail("could not get caller identity", e)

    s3 = boto3.client("s3", region_name=REGION)

    # 2. List objects under the prefix (sanity + confirms s3:ListBucket)
    try:
        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=PREFIX, MaxKeys=200)
    except Exception as e:
        fail(f"list_objects_v2 denied/failed on s3://{BUCKET}/{PREFIX}", e)
    keys = [o["Key"] for o in resp.get("Contents", [])]
    print(f"PASS list: found {len(keys)} object(s) directly under s3://{BUCKET}/{PREFIX}")

    # 2b. Discovery — find a parquet, descending into subfolders if needed
    parquet_key = next((k for k in keys if k.lower().endswith(".parquet")), None)
    if parquet_key is None:
        print("No parquet directly under the prefix; auto-discovering subfolders...")
        parquet_key, explored = discover_parquet(s3, BUCKET, PREFIX)
        print("  Explored prefixes:", explored[:30])
        if parquet_key is None:
            print(f"DISCOVERY: no .parquet found under s3://{BUCKET}/{PREFIX or '<root>'} "
                  f"within search limits.")
            print("  Re-run with a more specific 'prefix' input, or confirm the bucket is correct.")
            sys.exit(2)
    print(f"PASS discover: using s3://{BUCKET}/{parquet_key}")

    # 3. Read one parquet end-to-end (confirms s3:GetObject + format)
    target = f"s3://{BUCKET}/{parquet_key}"
    try:
        df = pd.read_parquet(target, storage_options={"client_kwargs": {"region_name": REGION}})
    except Exception as e:
        fail(f"could not read parquet {target}", e)

    print(f"PASS read: {target} -> shape {df.shape}")
    print("Columns:", list(df.columns)[:25])
    print(df.head().to_string())
    print("\nALL CHECKPOINTS PASSED — OIDC -> S3 parquet read works.")


if __name__ == "__main__":
    main()

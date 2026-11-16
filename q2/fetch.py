import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3

S3_BUCKET = os.environ["S3_BUCKET"]
FEED_URL = os.environ.get(
    "FEED_URL",
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
)

s3_client = boto3.client("s3")


def lambda_handler(event, context):
    now_utc = datetime.now(timezone.utc)
    timestamp_log = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        req = urllib.request.Request(
            FEED_URL,
            headers={"User-Agent": "EarthquakeDataPipeline/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            status_code = response.getcode()
            raw_bytes = response.read()
    except urllib.error.HTTPError as exc:
        print(f"[{timestamp_log}] HTTP error fetching USGS feed: status={exc.code}, reason={exc.reason}")
        return {"statusCode": exc.code, "message": "HTTP error from USGS feed"}
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[{timestamp_log}] Network/timeout error fetching USGS feed: {exc}")
        return {"statusCode": 504, "message": "Timeout or network error fetching USGS feed"}
    except Exception as exc:
        print(f"[{timestamp_log}] Unexpected error fetching USGS feed: {exc}")
        return {"statusCode": 500, "message": "Unexpected error fetching USGS feed"}

    if status_code != 200:
        print(f"[{timestamp_log}] Non-200 HTTP status={status_code}; skipping S3 upload")
        return {"statusCode": status_code, "message": "Non-200 status from USGS feed"}

    try:
        raw_text = raw_bytes.decode("utf-8")
        payload = json.loads(raw_text)
        feature_count = len(payload.get("features", []))
    except Exception as exc:
        print(f"[{timestamp_log}] Invalid JSON payload (status={status_code}): {exc}")
        return {"statusCode": 502, "message": "Invalid JSON from USGS feed"}

    s3_key = f"raw/{now_utc.strftime('%Y-%m-%dT%H-%M-%S')}.json"
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=raw_bytes,
        ContentType="application/json",
    )

    print(
        f"[{timestamp_log}] status={status_code} features={feature_count} "
        f"saved=s3://{S3_BUCKET}/{s3_key}"
    )
    return {
        "statusCode": 200,
        "s3_bucket": S3_BUCKET,
        "s3_key": s3_key,
        "feature_count": feature_count,
    }

import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote_plus

import boto3

DYNAMODB_TABLE = os.environ["DYNAMODB_TABLE"]

s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE)


def _is_valid_feature(feature):
    if not isinstance(feature, dict):
        return False

    eq_id = feature.get("id")
    props = feature.get("properties")
    geom = feature.get("geometry")

    if not eq_id or not isinstance(props, dict) or not isinstance(geom, dict):
        return False

    # Must be an actual earthquake
    if props.get("type") != "earthquake":
        return False

    # Magnitude must be present and numeric
    mag = props.get("mag")
    if mag is None or isinstance(mag, bool):
        return False

    # Timestamp must be present
    if props.get("time") is None:
        return False

    # Coordinates must have at least [longitude, latitude, depth_km]
    coords = geom.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 3:
        return False
    if any(c is None or isinstance(c, bool) for c in coords[:3]):
        return False

    try:
        lon = float(coords[0])
        lat = float(coords[1])
        float(coords[2])
        float(mag)
    except (TypeError, ValueError):
        return False

    if lat < -90.0 or lat > 90.0 or lon < -180.0 or lon > 180.0:
        return False

    return True


def _transform_feature(feature):
    eq_id = str(feature["id"])
    props = feature["properties"]
    coords = feature["geometry"]["coordinates"]

    time_ms = int(props["time"])
    time_iso = datetime.fromtimestamp(time_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    return {
        "PK": "EQ",
        "SK": f"{time_iso}#{eq_id}",
        "earthquake_id": eq_id,
        "magnitude": Decimal(str(props["mag"])),
        "mag_type": str(props.get("magType") or ""),
        "place": str(props.get("place") or ""),
        "time": time_ms,
        "time_iso": time_iso,
        "latitude": Decimal(str(coords[1])),
        "longitude": Decimal(str(coords[0])),
        "depth_km": Decimal(str(coords[2])),
        "tsunami": int(props.get("tsunami") or 0),
        "significance": int(props.get("sig") or 0),
        "status": str(props.get("status") or ""),
        "url": str(props.get("url") or ""),
    }


def lambda_handler(event, context):
    total_features = 0
    written_count = 0
    skipped_count = 0

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])

        obj = s3_client.get_object(Bucket=bucket, Key=key)
        raw_data = json.loads(obj["Body"].read().decode("utf-8"))
        features = raw_data.get("features", [])

        file_total = len(features)
        file_written = 0
        file_skipped = 0

        for feature in features:
            if not _is_valid_feature(feature):
                file_skipped += 1
                continue

            try:
                item = _transform_feature(feature)
                table.put_item(Item=item)
                file_written += 1
            except (InvalidOperation, ValueError, KeyError) as exc:
                print(f"Skipping malformed feature id={feature.get('id')}: {exc}")
                file_skipped += 1

        total_features += file_total
        written_count += file_written
        skipped_count += file_skipped

        print(
            f"Processed s3://{bucket}/{key} — "
            f"total={file_total}, written={file_written}, skipped={file_skipped}"
        )

    return {
        "statusCode": 200,
        "total_features": total_features,
        "written": written_count,
        "skipped": skipped_count,
    }

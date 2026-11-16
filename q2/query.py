import json
import os
from datetime import datetime
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

DYNAMODB_TABLE = os.environ["DYNAMODB_TABLE"]
GSI_NAME = os.environ.get("GSI_NAME", "earthquake_id-index")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE)


def _to_json_native(value):
    if isinstance(value, list):
        return [_to_json_native(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_json_native(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    return value


def _response(status_code, body_dict):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(_to_json_native(body_dict)),
    }


def _parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None


def _handle_list_earthquakes(query_params):
    start_str = (query_params.get("start") or "").strip()
    end_str = (query_params.get("end") or "").strip()

    start_dt = _parse_date(start_str)
    end_dt = _parse_date(end_str)
    if not start_dt or not end_dt or start_dt > end_dt:
        return _response(
            400,
            {
                "error": "Both 'start' and 'end' query parameters are required in YYYY-MM-DD format with start <= end."
            },
        )

    # Parse min_magnitude (default 0)
    raw_min_mag = query_params.get("min_magnitude")
    try:
        min_mag_val = float(raw_min_mag) if raw_min_mag is not None and raw_min_mag != "" else 0.0
    except ValueError:
        return _response(400, {"error": "Invalid 'min_magnitude' parameter; must be a number."})

    # Parse limit (default 50, max 500)
    raw_limit = query_params.get("limit")
    try:
        limit = int(raw_limit) if raw_limit is not None and raw_limit != "" else 50
    except ValueError:
        return _response(400, {"error": "Invalid 'limit' parameter; must be an integer."})

    if limit <= 0:
        return _response(400, {"error": "'limit' must be a positive integer."})
    limit = min(limit, 500)

    # ISO-8601 sort key range: PK = "EQ", SK between start 00:00:00Z and end 23:59:59Z~
    sk_start = f"{start_str}T00:00:00Z"
    sk_end = f"{end_str}T23:59:59Z~"

    query_kwargs = {
        "KeyConditionExpression": Key("PK").eq("EQ") & Key("SK").between(sk_start, sk_end),
        "ScanIndexForward": False,
    }
    if min_mag_val > 0:
        query_kwargs["FilterExpression"] = Attr("magnitude").gte(Decimal(str(min_mag_val)))

    matched_items = []
    while len(matched_items) < limit:
        resp = table.query(**query_kwargs)
        matched_items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    matched_items = matched_items[:limit]

    earthquakes = [
        {
            "earthquake_id": item["earthquake_id"],
            "magnitude": item["magnitude"],
            "place": item.get("place", ""),
            "time": item.get("time_iso", ""),
            "latitude": item["latitude"],
            "longitude": item["longitude"],
            "depth_km": item["depth_km"],
        }
        for item in matched_items
    ]

    min_mag_display = int(min_mag_val) if min_mag_val.is_integer() else min_mag_val

    return _response(
        200,
        {
            "parameters": {
                "start": start_str,
                "end": end_str,
                "min_magnitude": min_mag_display,
                "limit": limit,
            },
            "earthquakes": earthquakes,
            "count": len(earthquakes),
        },
    )


def _handle_single_earthquake(earthquake_id):
    resp = table.query(
        IndexName=GSI_NAME,
        KeyConditionExpression=Key("earthquake_id").eq(earthquake_id),
    )
    items = resp.get("Items", [])
    if not items:
        return _response(
            404,
            {"error": "Earthquake not found", "earthquake_id": earthquake_id},
        )

    # Sort descending by time_iso in case an updated origin time ever created two SKs
    items.sort(key=lambda x: x.get("time_iso", ""), reverse=True)
    item = items[0]

    return _response(
        200,
        {
            "earthquake_id": item["earthquake_id"],
            "magnitude": item["magnitude"],
            "mag_type": item.get("mag_type", ""),
            "place": item.get("place", ""),
            "time": item.get("time_iso", ""),
            "latitude": item["latitude"],
            "longitude": item["longitude"],
            "depth_km": item["depth_km"],
            "tsunami": item.get("tsunami", 0),
            "significance": item.get("significance", 0),
            "status": item.get("status", ""),
            "url": item.get("url", ""),
        },
    )


def lambda_handler(event, context):
    try:
        raw_path = event.get("rawPath", "")
        stage = (event.get("requestContext") or {}).get("stage")
        if stage and stage != "$default" and raw_path.startswith(f"/{stage}"):
            raw_path = raw_path[len(stage) + 1 :] or "/"

        raw_path = raw_path.rstrip("/") or "/"
        query_params = event.get("queryStringParameters") or {}
        path_params = event.get("pathParameters") or {}

        if path_params.get("earthquake_id"):
            return _handle_single_earthquake(path_params["earthquake_id"])

        if raw_path == "/earthquakes":
            return _handle_list_earthquakes(query_params)

        if raw_path.startswith("/earthquakes/"):
            eq_id = raw_path.split("/earthquakes/", 1)[1]
            if eq_id:
                return _handle_single_earthquake(eq_id)

        return _response(404, {"error": f"Route not found: {raw_path}"})
    except Exception as exc:
        print(f"Unhandled error in query handler: {exc}")
        return _response(500, {"error": "Internal server error"})

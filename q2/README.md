# Earthquake Data Pipeline

## 1. API Gateway Base URL
https://1ta7rxwtzi.execute-api.us-west-2.amazonaws.com

curl -i "https://1ta7rxwtzi.execute-api.us-west-2.amazonaws.com/default/earthquakes?start=2026-11-15&end=2026-11-17"

## 2. DynamoDB Schema Design
- **Table Name:** `earthquakes`
- **Primary Key:**
  - **Partition Key (`PK`):** `String` — fixed value `"EQ"`
  - **Sort Key (`SK`):** `String` — composite string `<time_iso>#<earthquake_id>` (e.g., `2026-04-08T14:30:00Z#us7000abc1`)
- **Global Secondary Index (GSI):**
  - **Index Name:** `earthquake_id-index`
  - **Partition Key:** `earthquake_id` (`String`)

### Key Design Justification
1. **Time-Sorted Date-Range Queries (`GET /earthquakes`):** Storing all earthquake items under a single partition key (`PK = "EQ"`) with an ISO-8601 UTC timestamp prefix in the sort key (`SK = "<time_iso>#<earthquake_id>"`) keeps records ordered chronologically within the partition. Because ISO-8601 timestamps sort lexicographically, date-range queries (`SK BETWEEN "YYYY-MM-DDT00:00:00Z" AND "YYYY-MM-DDT23:59:59Z~"`) with `ScanIndexForward=False` return the most recent earthquakes first without scanning the table or parsing date strings. Appending `#<earthquake_id>` guarantees sort-key uniqueness if multiple earthquakes occur during the same second.
2. **Direct Single-Event Lookups (`GET /earthquakes/{earthquake_id}`):** Querying the base table requires knowing both `PK` and the exact timestamp inside `SK`. Adding the `earthquake_id-index` GSI enables direct partition lookups by `earthquake_id` alone when a client requests a single event.

## 3. Data Flow Architecture
The pipeline consists of three decoupled AWS Lambda functions coordinated asynchronously through S3 and DynamoDB:
1. **Ingestion (`fetch.py`):** An Amazon EventBridge scheduled rule triggers `fetch.py` every 10 minutes (`rate(10 minutes)`). The function fetches the USGS `all_hour.geojson` feed with a 10-second timeout and writes the raw GeoJSON response to S3 (`earthquakes-081155410967`) under `raw/YYYY-MM-DDTHH-MM-SS.json`. Non-200 HTTP responses, timeouts, or network errors are logged and return cleanly without raising so EventBridge does not retry stale fetches.
2. **Transformation & Storage (`process.py`):** S3 triggers `process.py` automatically on `ObjectCreated` events for objects matching the `raw/` prefix. The handler reads the raw GeoJSON object, filters out invalid records (missing/null `mag`, non-`"earthquake"` event types, missing coordinates, or out-of-range latitude/longitude), converts numeric values to `Decimal`, constructs the `PK` and `SK` keys, and writes structured items to the `earthquakes` DynamoDB table.
3. **HTTP Query API (`query.py`):** Amazon API Gateway (HTTP API) routes `GET /earthquakes` and `GET /earthquakes/{earthquake_id}` requests to `query.py`. The handler validates query parameters (`start`, `end`, `min_magnitude`, `limit`), executes either a sort-key range query on the main table or a GSI lookup on `earthquake_id-index`, converts DynamoDB `Decimal` values to standard JSON numbers, and returns JSON responses with appropriate HTTP status codes (`200`, `400`, `404`, or `500`).

## 4. Idempotent Inserts & De-duplication
Although `fetch.py` runs every 10 minutes and pulls a rolling 1-hour window from USGS—meaning the same earthquake appears in up to six consecutive raw S3 files—it does not produce duplicate records in DynamoDB:
- Each earthquake's primary key is uniquely defined by `(PK="EQ", SK="<time_iso>#<earthquake_id>")`, combining its fixed origin timestamp (`properties.time`) and its unique USGS event ID (`feature.id`).
- When `process.py` executes `table.put_item(Item=item)` for an earthquake that was already processed in an earlier run, DynamoDB performs an idempotent overwrite of the existing item with the matching `PK` and `SK` (refreshing mutable fields like `status` or `significance` if USGS updated the event) rather than creating a duplicate item.

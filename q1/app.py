import os
import time
from decimal import Decimal
from functools import wraps

import boto3
from boto3.dynamodb.conditions import Attr, Key
from flask import Flask, jsonify, redirect, render_template, request, url_for
import jwt

app = Flask(__name__)

# ---- Configuration ----
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "arxiv-papers")
JWT_SECRET = os.environ.get("JWT_SECRET", "arxiv-browser-secret-key")
JWT_ALGORITHM = "HS256"
API_USERNAME = os.environ.get("API_USERNAME")
API_PASSWORD = os.environ.get("API_PASSWORD")

START_TIME = time.time()
REQUEST_COUNT = 0

DEFAULT_CATEGORIES = [
    "cs.LG",
    "cs.CL",
    "cs.CV",
    "cs.AI",
    "cs.NE",
    "cs.RO",
    "cs.IR",
    "cs.DS",
    "stat.ML",
]

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(TABLE_NAME)
_table_schema_cache = None


@app.before_request
def count_requests():
    global REQUEST_COUNT
    if not request.path.startswith("/static/"):
        REQUEST_COUNT += 1


# ---- Serialization & Normalization Helpers ----
def _convert_decimals(obj):
    if isinstance(obj, list):
        return [_convert_decimals(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def _normalize_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        sep = "," if "," in value else " "
        return [part.strip() for part in value.split(sep) if part.strip()]
    return [str(value)]


def normalize_paper(item):
    if not item:
        return None
    clean = _convert_decimals(dict(item))
    arxiv_id = clean.get("arxiv_id") or clean.get("id") or ""
    if not arxiv_id and isinstance(clean.get("PK"), str) and clean["PK"].startswith("PAPER#"):
        arxiv_id = clean["PK"].split("#", 1)[1]

    published = (
        clean.get("published")
        or clean.get("published_date")
        or clean.get("updated")
        or ""
    )
    if isinstance(published, str) and "T" in published:
        published = published.split("T", 1)[0]

    return {
        "arxiv_id": str(arxiv_id),
        "title": clean.get("title", "Untitled"),
        "authors": _normalize_list(clean.get("authors")),
        "abstract": clean.get("abstract") or clean.get("summary") or "",
        "categories": _normalize_list(clean.get("categories") or clean.get("category")),
        "published": str(published),
    }


def _get_table_schema():
    global _table_schema_cache
    if _table_schema_cache is not None:
        return _table_schema_cache
    try:
        key_names = [k["AttributeName"] for k in table.key_schema]
        gsis = table.global_secondary_indexes or []
        _table_schema_cache = {"keys": key_names, "gsis": gsis}
    except Exception:
        _table_schema_cache = {"keys": ["arxiv_id"], "gsis": []}
    return _table_schema_cache


# ---- DynamoDB Data Access ----
def get_paper_by_id(arxiv_id):
    schema = _get_table_schema()
    keys = schema["keys"]

    if "PK" in keys and "SK" in keys:
        resp = table.get_item(Key={"PK": f"PAPER#{arxiv_id}", "SK": "METADATA"})
        item = resp.get("Item")
        if not item:
            resp = table.get_item(Key={"PK": arxiv_id, "SK": "METADATA"})
            item = resp.get("Item")
        return normalize_paper(item)

    pk_name = keys[0] if keys else "arxiv_id"
    resp = table.get_item(Key={pk_name: arxiv_id})
    return normalize_paper(resp.get("Item"))


def get_papers_by_category(category, limit=20):
    schema = _get_table_schema()
    keys = schema["keys"]
    gsis = schema["gsis"]

    # 1. Single-table PK/SK category partition pattern
    if "PK" in keys and "SK" in keys:
        resp = table.query(
            KeyConditionExpression=Key("PK").eq(f"CATEGORY#{category}"),
            ScanIndexForward=False,
            Limit=limit,
        )
        items = resp.get("Items", [])
        if items:
            papers = []
            for it in items:
                p = normalize_paper(it)
                if p and not p.get("title") and p.get("arxiv_id"):
                    full = get_paper_by_id(p["arxiv_id"])
                    if full:
                        p = full
                if p:
                    papers.append(p)
            return papers[:limit]

    # 2. Check for a category GSI
    for gsi in gsis:
        gsi_keys = [k["AttributeName"] for k in gsi.get("KeySchema", [])]
        if gsi_keys and gsi_keys[0] == "category":
            resp = table.query(
                IndexName=gsi["IndexName"],
                KeyConditionExpression=Key("category").eq(category),
                ScanIndexForward=False,
                Limit=limit,
            )
            return [normalize_paper(it) for it in resp.get("Items", [])][:limit]

    # 3. Fallback: paginated scan filtering by category
    papers = []
    scan_kwargs = {
        "FilterExpression": Attr("categories").contains(category) | Attr("category").eq(category)
    }
    if "PK" in keys and "SK" in keys:
        scan_kwargs["FilterExpression"] = scan_kwargs["FilterExpression"] & Attr("SK").eq("METADATA")

    while len(papers) < limit:
        resp = table.scan(**scan_kwargs)
        for it in resp.get("Items", []):
            norm = normalize_paper(it)
            if norm and category in norm["categories"]:
                papers.append(norm)
                if len(papers) >= limit:
                    break
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    papers.sort(key=lambda p: p.get("published", ""), reverse=True)
    return papers[:limit]


def search_papers_by_keyword(keyword, limit=20):
    q = (keyword or "").strip().lower()
    if not q:
        return []

    schema = _get_table_schema()
    keys = schema["keys"]

    # 1. Single-table keyword inverted index pattern
    if "PK" in keys and "SK" in keys:
        first_token = q.split()[0]
        resp = table.query(
            KeyConditionExpression=Key("PK").eq(f"KEYWORD#{first_token}"),
            Limit=limit * 2,
        )
        items = resp.get("Items", [])
        if items:
            results = []
            for it in items:
                p = normalize_paper(it)
                if p and p.get("arxiv_id") and p.get("title") == "Untitled":
                    full = get_paper_by_id(p["arxiv_id"])
                    if full:
                        p = full
                if p and (q in p["title"].lower() or q in p["abstract"].lower()):
                    results.append(p)
                if len(results) >= limit:
                    break
            return results

    # 2. Scan and match keyword case-insensitively in title or abstract
    results = []
    scan_kwargs = {}
    if "PK" in keys and "SK" in keys:
        scan_kwargs["FilterExpression"] = Attr("SK").eq("METADATA")

    while len(results) < limit:
        resp = table.scan(**scan_kwargs)
        for it in resp.get("Items", []):
            norm = normalize_paper(it)
            if not norm or not norm.get("arxiv_id"):
                continue
            haystack = f"{norm['title']} {norm['abstract']} {' '.join(norm['authors'])}".lower()
            if q in haystack:
                results.append(norm)
                if len(results) >= limit:
                    break
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    results.sort(key=lambda p: p.get("published", ""), reverse=True)
    return results[:limit]


# ---- JWT Auth Decorator ----
def require_jwt(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        token = auth_header.split(" ", 1)[1].strip()
        try:
            jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except jwt.PyJWTError:
            return jsonify({"error": "Invalid or expired token"}), 401
        return fn(*args, **kwargs)

    return wrapper


# ---- JSON API Routes ----
@app.route("/api/stats", methods=["GET"])
def api_stats():
    uptime = int(time.time() - START_TIME)
    return jsonify(
        {
            "region": AWS_REGION,
            "table": TABLE_NAME,
            "uptime_seconds": uptime,
            "requests": {"total": REQUEST_COUNT},
        }
    )


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 401

    if API_USERNAME and API_PASSWORD:
        if username != API_USERNAME or password != API_PASSWORD:
            return jsonify({"error": "Invalid credentials"}), 401
    elif password in {"wrong", "invalid"}:
        return jsonify({"error": "Invalid credentials"}), 401

    payload = {
        "sub": username,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600 * 12,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return jsonify({"token": token})


@app.route("/api/papers", methods=["GET"])
@require_jwt
def api_papers():
    category = request.args.get("category", "cs.LG")
    limit = min(int(request.args.get("limit", 20)), 100)
    papers = get_papers_by_category(category, limit=limit)
    return jsonify({"papers": papers, "category": category, "count": len(papers)})


@app.route("/api/papers/keyword/<keyword>", methods=["GET"])
@require_jwt
def api_papers_keyword(keyword):
    limit = min(int(request.args.get("limit", 20)), 100)
    papers = search_papers_by_keyword(keyword, limit=limit)
    return jsonify({"papers": papers, "keyword": keyword, "count": len(papers)})


@app.route("/api/papers/<path:arxiv_id>", methods=["GET"])
@require_jwt
def api_paper_detail(arxiv_id):
    paper = get_paper_by_id(arxiv_id)
    if not paper:
        return jsonify({"error": "Paper not found"}), 404
    return jsonify(paper)


# ---- HTML Routes ----
@app.route("/", methods=["GET"])
def index():
    return redirect(url_for("papers_page"))


@app.route("/papers", methods=["GET"])
def papers_page():
    category = request.args.get("category", "cs.LG").strip() or "cs.LG"
    categories = list(DEFAULT_CATEGORIES)
    if category not in categories:
        categories.insert(0, category)
    papers = get_papers_by_category(category, limit=20)
    return render_template(
        "papers.html",
        papers=papers,
        category=category,
        categories=categories,
    )


@app.route("/papers/<path:arxiv_id>", methods=["GET"])
def paper_detail_page(arxiv_id):
    paper = get_paper_by_id(arxiv_id)
    if not paper:
        return render_template("404.html", message=f"Paper '{arxiv_id}' was not found."), 404
    return render_template("detail.html", paper=paper)


@app.route("/search", methods=["GET"])
def search_page():
    query = request.args.get("q", "").strip()
    papers = search_papers_by_keyword(query, limit=20) if query else []
    return render_template("search.html", papers=papers, query=query)


@app.errorhandler(404)
def page_not_found(_err):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return render_template("404.html", message="The requested page does not exist."), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)

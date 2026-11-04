import os
import time
import re
import json
import hmac
import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, Response

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "development-only-change-me")
DB_PATH = Path(os.getenv("DATABASE_PATH", "data/creatorpay.db"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE / DB_PATH

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
PAYPAL_MODE = os.getenv("PAYPAL_MODE", "sandbox").lower()
PAYPAL_BASE = (
    "https://api-m.sandbox.paypal.com"
    if PAYPAL_MODE == "sandbox"
    else "https://api-m.paypal.com"
)


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn



def requested_page_count(idea):
    words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
        "fifteen": 15, "sixteen": 16, "seventeen": 17,
        "eighteen": 18, "nineteen": 19, "twenty": 20
    }
    pattern = (
        r"\b(?:exactly\s+|only\s+|just\s+)?"
        r"(\d{1,2}|" + "|".join(words) +
        r")\s*(?:-\s*)?(?:pages?|pgs?)\b"
    )
    match = re.search(pattern, str(idea or "").lower())
    if not match:
        return 5
    value = match.group(1)
    number = int(value) if value.isdigit() else words[value]
    return max(1, min(20, number))


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            content TEXT NOT NULL,
            price TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL,
            paypal_order_id TEXT UNIQUE,
            amount TEXT NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL,
            capture_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        """)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(products)").fetchall()
        }
        if "page_count" not in columns:
            conn.execute(
                "ALTER TABLE products ADD COLUMN "
                "page_count INTEGER NOT NULL DEFAULT 5"
            )


def now():
    return datetime.now(timezone.utc).isoformat()


def error(message, status=400):
    return jsonify({"error": message}), status



def gemini_post(prompt, generation_config, timeout, operation):
    """Call Gemini with bounded retries for temporary provider failures."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    headers = {"x-goog-api-key": os.getenv("GEMINI_API_KEY", "")}
    retryable_statuses = {500, 502, 503, 504}

    for attempt in range(4):
        try:
            response = requests.post(
                url,
                headers=headers,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": generation_config,
                },
                timeout=timeout,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            app.logger.warning(
                "Gemini %s attempt %s/4 network failure: %s",
                operation, attempt + 1, type(exc).__name__
            )
            if attempt == 3:
                raise
            time.sleep(min(2 ** (attempt + 1), 12))
            continue

        if response.status_code not in retryable_statuses:
            return response

        app.logger.warning(
            "Gemini %s attempt %s/4 returned HTTP %s",
            operation, attempt + 1, response.status_code
        )
        if attempt == 3:
            return response

        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = min(max(float(retry_after), 1), 20)
        except (TypeError, ValueError):
            delay = min(2 ** (attempt + 1), 12)
        time.sleep(delay)

    return response


def ai_post(prompt, generation_config, timeout, operation):
    """Try Groq first, then automatically fall back to Gemini."""
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    groq_failure = "Groq is not configured."

    if groq_key:
        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {groq_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": generation_config.get("temperature", 0.65),
                    "max_tokens": generation_config.get(
                        "maxOutputTokens", 2500
                    ),
                },
                timeout=timeout,
            )

            if response.ok:
                body = response.json()
                content = (
                    body.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                if isinstance(content, str) and content.strip():
                    normalized = requests.Response()
                    normalized.status_code = 200
                    normalized.url = response.url
                    normalized.headers["Content-Type"] = "application/json"
                    normalized.headers["X-AI-Provider"] = "Groq"
                    normalized.headers["X-AI-Model"] = GROQ_MODEL
                    normalized._content = json.dumps({
                        "candidates": [{
                            "content": {
                                "parts": [{"text": content.strip()}]
                            }
                        }]
                    }).encode("utf-8")
                    app.logger.info(
                        "%s completed with Groq model %s",
                        operation, GROQ_MODEL
                    )
                    return normalized

                groq_failure = "Groq returned an empty response."
            else:
                groq_failure = f"Groq returned HTTP {response.status_code}."

        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            groq_failure = f"Groq request failed ({type(exc).__name__})."

        app.logger.warning(
            "%s: %s Trying Gemini fallback.",
            operation, groq_failure
        )

    if gemini_key:
        try:
            response = gemini_post(
                prompt, generation_config, timeout, operation
            )
            response.headers["X-AI-Provider"] = "Google Gemini"
            response.headers["X-AI-Model"] = GEMINI_MODEL

            if not response.ok:
                detail = ""
                try:
                    provider_body = response.json()
                    detail = str(
                        provider_body.get("error", {}).get("message", "")
                    ).strip()
                except (ValueError, AttributeError, TypeError):
                    detail = ""

                app.logger.warning(
                    "%s: Gemini fallback returned HTTP %s; %s",
                    operation,
                    response.status_code,
                    detail[:500] or "No provider error message returned"
                )
            else:
                app.logger.info(
                    "%s completed with Gemini model %s",
                    operation, GEMINI_MODEL
                )

            return response

        except requests.RequestException as exc:
            app.logger.error(
                "%s: Gemini fallback network failure (%s)",
                operation, type(exc).__name__
            )
            raise

    unavailable = requests.Response()
    unavailable.status_code = 503
    unavailable._content = json.dumps({
        "error": {"message": groq_failure + " Gemini fallback is not configured."}
    }).encode("utf-8")
    unavailable.headers["X-AI-Provider"] = "Unavailable"
    unavailable.headers["X-AI-Model"] = "none"
    return unavailable


def ai_model(response):
    return response.headers.get("X-AI-Model", GEMINI_MODEL)



def delivery_token(paypal_order_id):
    secret = app.config["SECRET_KEY"]
    return hmac.new(
        secret.encode("utf-8"),
        ("creatorpay-delivery:" + paypal_order_id).encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def paypal_access_token():
    client_id = os.getenv("PAYPAL_CLIENT_ID", "")
    secret = os.getenv("PAYPAL_CLIENT_SECRET", "")
    if not client_id or not secret:
        raise RuntimeError("PayPal Sandbox credentials are not configured.")
    response = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(client_id, secret),
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    if not response.ok:
        raise RuntimeError("PayPal authentication failed.")
    return response.json()["access_token"]


def paypal_request(method, path, payload=None, request_id=None):
    token = paypal_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    response = requests.request(
        method, f"{PAYPAL_BASE}{path}", headers=headers,
        json=payload, timeout=30
    )
    if not response.ok:
        try:
            body = response.json()
        except Exception:
            body = {}

        details = body.get("details") or []
        safe_details = [
            {
                "issue": item.get("issue"),
                "description": item.get("description"),
            }
            for item in details
            if isinstance(item, dict)
        ]

        print(
            "[PAYPAL API ERROR]",
            {
                "method": method,
                "path": path,
                "http_status": response.status_code,
                "debug_id": response.headers.get("PayPal-Debug-Id"),
                "name": body.get("name"),
                "message": body.get("message"),
                "details": safe_details,
            },
            flush=True,
        )
        raise RuntimeError(f"PayPal returned HTTP {response.status_code}.")
    return response.json()


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "app": "CreatorPay AI",
        "ai_configured": bool(os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY")),
        "ai_primary_provider": "Groq",
        "ai_providers": {
            "groq": bool(os.getenv("GROQ_API_KEY")),
            "gemini": bool(os.getenv("GEMINI_API_KEY")),
        },
        "paypal_configured": bool(
            os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_CLIENT_SECRET")
        ),
        "paypal_mode": PAYPAL_MODE,
    })


@app.get("/api/config")
def public_config():
    return jsonify({"paypal_client_id": os.getenv("PAYPAL_CLIENT_ID", "")})


@app.post("/api/ai/generate")
def generate_product():
    data = request.get_json(silent=True) or {}
    idea = str(data.get("idea", "")).strip()
    audience = str(data.get("audience", "")).strip()
    product_type = str(data.get("product_type", "digital guide")).strip()
    page_count = requested_page_count(idea)

    if not idea or len(idea) > 1200:
        return error("Enter a product idea (maximum 1,200 characters).")
    if len(audience) > 500 or len(product_type) > 100:
        return error("Please shorten the product details.")
    if not (os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY")):
        return error("AI is not connected. Configure Groq or Gemini.", 503)

    prompt = f"""
You are the professional digital-product planner inside CreatorPay AI.

Create a detailed, useful OUTLINE for an original digital product.
Do not write the complete manuscript yet. Each chapter will be expanded
in a separate request.

PRODUCT IDEA: {idea}
TARGET AUDIENCE: {audience or "Not specified"}
PRODUCT FORMAT: {product_type}
REQUESTED FINAL PDF LENGTH: exactly {page_count} pages.
Plan the introduction, chapters and conclusion to fit this page limit.

Use these exact section headings:

PRODUCT TITLE
SHORT DESCRIPTION
PRODUCT CONTENT
LISTING DESCRIPTION
SUGGESTED PRICE (USD)

Under PRODUCT CONTENT provide:
- An introduction explaining the reader's goal.
- For 1–2 pages, create 2 concise chapters.
- For 3–4 pages, create 3 or 4 chapters.
- For 5–20 pages, create 5–7 appropriately sized chapters.
- Use headings in this format:
  ### Chapter 1: Descriptive chapter title
- Four to six specific teaching points under each chapter.
- Practical examples and exercises to develop in each chapter.
- A conclusion.
- A launch/action checklist where appropriate.

Rules:
- Make the outline specific enough to expand into a complete product.
- Write original material.
- Never invent credentials, reviews, customer results or guarantees.
- Never promise that buyers will make money.
- Do not invent statistics or factual references.
- Mark assumptions and advise checking important facts.
- Suggested price is advisory only.
- Return the requested sections, not commentary.
"""
    try:
        response = ai_post(
            prompt,
            {"temperature": 0.65, "maxOutputTokens": 2500},
            timeout=(20, 90),
            operation="product outline",
        )
        if not response.ok:
            if response.status_code == 429:
                return error(
                    "Gemini rate limit or quota reached. Wait before trying again.",
                    429
                )
            if response.status_code in (500, 502, 503, 504):
                return error(
                    "Gemini remained temporarily unavailable after four attempts "
                    f"(HTTP {response.status_code}). Your previous draft was kept. "
                    "Please retry shortly.",
                    503
                )
            return error(
                f"Gemini rejected the request (HTTP {response.status_code}).",
                502
            )
        body = response.json()
        text = "\n".join(
            part.get("text", "")
            for candidate in body.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
        ).strip()
        if not text:
            return error("The AI provider returned an empty draft.", 502)

        def extract_section(label, following):
            next_heading = "|".join(re.escape(x) for x in following)
            pattern = (
                r"(?ims)^\s*(?:#{1,6}\s*)?(?:\*\*)?"
                + re.escape(label)
                + r"(?:\*\*)?\s*:?\s*\n"
                + r"(.*?)(?=^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:"
                + next_heading
                + r")(?:\*\*)?\s*:?\s*$|\Z)"
            )
            match = re.search(pattern, text)
            return match.group(1).strip() if match else ""

        title = extract_section("PRODUCT TITLE", [
            "SHORT DESCRIPTION", "PRODUCT CONTENT",
            "LISTING DESCRIPTION", "SUGGESTED PRICE (USD)"
        ])
        short_description = extract_section("SHORT DESCRIPTION", [
            "PRODUCT CONTENT", "LISTING DESCRIPTION", "SUGGESTED PRICE (USD)"
        ])
        listing_description = extract_section("LISTING DESCRIPTION", [
            "SUGGESTED PRICE (USD)"
        ])
        price_text = extract_section("SUGGESTED PRICE (USD)", [])
        price_match = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", price_text)

        return jsonify({
            "draft": text,
            "title": title[:120].strip("*# "),
            "short_description": (short_description or listing_description)[:1000],
            "suggested_price": price_match.group(1) if price_match else "",
            "page_count": page_count,
            "model": ai_model(response),
            "provider": response.headers.get("X-AI-Provider", "Google Gemini")
        })
    except requests.Timeout:
        app.logger.warning("Gemini product outline timed out after retries.")
        return error(
            "Gemini timed out after four attempts. Your previous draft was kept.",
            504
        )
    except requests.RequestException:
        app.logger.exception("Gemini product outline network request failed.")
        return error(
            "Could not reach Gemini after four attempts. Your previous draft was kept.",
            502
        )


@app.post("/api/ai/expand-chapter")
def expand_chapter():
    data = request.get_json(silent=True) or {}
    idea = str(data.get("idea", "")).strip()
    audience = str(data.get("audience", "")).strip()
    product_type = str(data.get("product_type", "E-book")).strip()
    chapter_title = str(data.get("chapter_title", "")).strip()
    chapter_outline = str(data.get("chapter_outline", "")).strip()
    try:
        page_count = int(data.get("page_count") or requested_page_count(idea))
        chapter_count = int(data.get("chapter_count") or 2)
    except (TypeError, ValueError):
        return error("Invalid page or chapter count.")
    if not 1 <= page_count <= 20 or not 1 <= chapter_count <= 10:
        return error("Page or chapter count is outside the allowed range.")
    target_words = max(80, min(1800, (page_count * 300) // chapter_count))

    if not idea or len(idea) > 1200:
        return error("A valid product idea is required.")
    if not chapter_title or len(chapter_title) > 250:
        return error("A valid chapter title is required.")
    if not chapter_outline or len(chapter_outline) > 5000:
        return error("A valid chapter outline is required.")
    if len(audience) > 500 or len(product_type) > 100:
        return error("Please shorten the product details.")

    if not (os.getenv("GROQ_API_KEY") or os.getenv("GEMINI_API_KEY")):
        return error("AI is not connected. Configure Groq or Gemini.", 503)

    prompt = f"""
You are writing one complete chapter of an original digital product for CreatorPay AI.

PRODUCT IDEA: {idea}
TARGET AUDIENCE: {audience or "Beginners"}
FORMAT: {product_type}
TOTAL PRODUCT LENGTH: exactly {page_count} PDF pages.
TARGET LENGTH FOR THIS CHAPTER: approximately {target_words} words.
There are {chapter_count} chapters. Keep the entire manuscript within the page budget, including introduction and conclusion.
CHAPTER: {chapter_title}

CHAPTER PLAN:
{chapter_outline}

Write the actual chapter, not another outline. Make it detailed, practical,
self-contained and appropriate for the target audience.

Include:
- A clear explanation of the topic.
- Detailed step-by-step instructions.
- Realistic examples, including Nigerian beginner examples where relevant.
- Practical activities, exercises or worksheets.
- Common mistakes and how to avoid them.
- A short chapter summary and actionable next steps.

Use clear headings and readable paragraphs. Avoid padding, repetition,
invented statistics, fake testimonials, guaranteed income claims or
unverified factual claims. Do not claim that CreatorPay supports features
that are not implemented. Mark assumptions clearly.

Return only the finished chapter in Markdown.
"""
    try:
        response = ai_post(
            prompt,
            {"temperature": 0.65, "maxOutputTokens": 3000},
            timeout=(20, 120),
            operation="chapter expansion",
        )
        if not response.ok:
            detail = ""
            try:
                provider_body = response.json()
                detail = str(
                    provider_body.get("error", {}).get("message", "")
                ).strip()
            except (ValueError, AttributeError):
                detail = ""

            app.logger.warning(
                "Gemini chapter request failed: HTTP %s; %s",
                response.status_code,
                detail[:400] or "No provider message returned"
            )

            if response.status_code == 429:
                message = "Gemini rate limit or quota reached. Wait before trying again."
            elif response.status_code in (500, 502, 503, 504):
                message = "Gemini remained unavailable after four attempts. Your completed chapters are preserved; retry this chapter shortly."
            else:
                message = f"Gemini rejected the chapter request (HTTP {response.status_code})."

            if detail:
                message += " Details: " + detail[:300]

            return error(message, 502)

        try:
            body = response.json()
        except ValueError:
            app.logger.warning("Gemini returned invalid JSON for chapter generation.")
            return error("Gemini returned an unreadable response. Please retry.", 502)

        chapter = "\n".join(
            part.get("text", "")
            for candidate in body.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
        ).strip()

        if not chapter:
            app.logger.warning(
                "Gemini returned no chapter text. Candidate count: %s",
                len(body.get("candidates", []))
            )
            return error("Gemini returned an empty chapter. Please retry.", 502)

        return jsonify({
            "chapter": chapter,
            "model": ai_model(response),
            "provider": response.headers.get("X-AI-Provider", "Google Gemini")
        })
    except requests.Timeout:
        app.logger.warning("Gemini chapter request timed out.")
        return error("Gemini took too long to write this chapter. Please retry.", 504)
    except requests.RequestException:
        app.logger.exception("Gemini chapter network request failed.")
        return error("Could not reach the AI provider while writing this chapter.", 502)


@app.get("/api/products")
def products():
    with db() as conn:
        rows = conn.execute(
            "SELECT id,title,description,price,currency,created_at,page_count "
            "FROM products ORDER BY created_at DESC"
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/products")
def create_product():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()
    content = str(data.get("content", "")).strip()
    try:
        page_count = int(data.get("page_count", 5))
    except (TypeError, ValueError):
        return error("Enter a valid page count.")
    if not 1 <= page_count <= 20:
        return error("Page count must be between 1 and 20.")
    try:
        price = Decimal(str(data.get("price", ""))).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return error("Enter a valid USD price.")
    if not title or len(title) > 120:
        return error("Product title is required (maximum 120 characters).")
    if not description or len(description) > 1000:
        return error("A short description is required (maximum 1,000 characters).")
    if not content or len(content) > 50000:
        return error("Product content is required (maximum 50,000 characters).")
    if price < Decimal("1.00") or price > Decimal("10000.00"):
        return error("Price must be between $1.00 and $10,000.00.")

    product_id = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO products "
            "(id,title,description,content,price,currency,created_at,page_count) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (product_id, title, description, content, str(price), "USD", now(), page_count)
        )
    return jsonify({"id": product_id, "message": "Product saved."}), 201


@app.post("/api/paypal/create-order")
def create_paypal_order():
    data = request.get_json(silent=True) or {}
    product_id = str(data.get("product_id", ""))
    with db() as conn:
        product = conn.execute(
            "SELECT * FROM products WHERE id=?", (product_id,)
        ).fetchone()
    if not product:
        return error("Product not found.", 404)

    local_order_id = str(uuid.uuid4())
    try:
        result = paypal_request(
            "POST",
            "/v2/checkout/orders",
            {
                "intent": "CAPTURE",
                "purchase_units": [{
                    "reference_id": local_order_id,
                    "custom_id": product_id,
                    "description": product["title"][:127],
                    "amount": {
                        "currency_code": product["currency"],
                        "value": product["price"],
                    },
                }],
                "payment_source": {
                    "paypal": {
                        "experience_context": {
                            "brand_name": "CreatorPay AI",
                            "landing_page": "LOGIN",
                            "user_action": "PAY_NOW",
                            "shipping_preference": "NO_SHIPPING",
                        }
                    }
                },
            },
            request_id=local_order_id,
        )
        paypal_id = result["id"]
        with db() as conn:
            conn.execute(
                "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
                (local_order_id, product_id, paypal_id, product["price"],
                 product["currency"], "CREATED", None, now())
            )
        return jsonify({"id": paypal_id})
    except (requests.RequestException, RuntimeError, KeyError):
        return error("Could not create a PayPal order. Check Sandbox configuration.", 502)


@app.post("/api/paypal/capture-order")
def capture_paypal_order():
    data = request.get_json(silent=True) or {}
    paypal_id = str(data.get("order_id", ""))
    if not paypal_id or len(paypal_id) > 80:
        return error("A valid PayPal order ID is required.")

    with db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE paypal_order_id=?", (paypal_id,)
        ).fetchone()
    if not order:
        return error("Order not found.", 404)
    if order["status"] == "COMPLETED":
        return jsonify({
            "status": "COMPLETED",
            "message": "Payment already recorded.",
            "product_id": order["product_id"],
            "delivery_token": delivery_token(paypal_id),
        })

    try:
        # Retrieve the PayPal order before attempting capture.
        paypal_order = paypal_request(
            "GET",
            f"/v2/checkout/orders/{paypal_id}",
        )

        paypal_units = paypal_order.get("purchase_units") or []
        if len(paypal_units) != 1:
            return error("PayPal order has an unexpected purchase-unit structure.", 409)

        paypal_unit = paypal_units[0]
        paypal_amount = paypal_unit.get("amount") or {}

        order_matches = (
            paypal_order.get("id") == paypal_id
            and paypal_order.get("intent") == "CAPTURE"
            and paypal_order.get("status") == "APPROVED"
            and paypal_unit.get("reference_id") == order["id"]
            and paypal_unit.get("custom_id") == order["product_id"]
            and paypal_amount.get("currency_code") == order["currency"]
            and paypal_amount.get("value") == order["amount"]
        )

        if not order_matches:
            print(
                "[PAYPAL ORDER LOOKUP MISMATCH]",
                {
                    "paypal_order_id": paypal_id,
                    "paypal_status": paypal_order.get("status"),
                    "reference_matches": paypal_unit.get("reference_id") == order["id"],
                    "product_matches": paypal_unit.get("custom_id") == order["product_id"],
                    "amount_matches": (
                        paypal_amount.get("value") == order["amount"]
                        and paypal_amount.get("currency_code") == order["currency"]
                    ),
                },
                flush=True,
            )
            return error("PayPal order is not approved or does not match this purchase.", 409)

        result = paypal_request(
            "POST", f"/v2/checkout/orders/{paypal_id}/capture", {},
            request_id=f"capture-{order['id']}"
        )
        unit = (result.get("purchase_units") or [{}])[0]
        capture = (unit.get("payments", {}).get("captures") or [{}])[0]
        amount = capture.get("amount", {})
        valid = (
            result.get("status") == "COMPLETED"
            and capture.get("status") == "COMPLETED"
            and amount.get("currency_code") == order["currency"]
            and amount.get("value") == order["amount"]
        )
        if not valid:
            return error("PayPal did not confirm the expected completed payment.", 409)

        with db() as conn:
            conn.execute(
                "UPDATE orders SET status='COMPLETED',capture_id=? WHERE id=?",
                (capture.get("id"), order["id"])
            )
        return jsonify({
            "status": "COMPLETED",
            "message": "Payment verified. Your product is ready.",
            "product_id": order["product_id"],
            "delivery_token": delivery_token(paypal_id),
        })
    except (requests.RequestException, RuntimeError, KeyError, IndexError):
        return error("PayPal capture could not be verified.", 502)


@app.post("/api/orders/deliver")
def deliver_purchased_product():
    data = request.get_json(silent=True) or {}
    paypal_id = str(data.get("order_id", ""))
    supplied_token = str(data.get("delivery_token", ""))

    if not paypal_id or len(paypal_id) > 80 or len(supplied_token) != 64:
        return error("A valid completed order and delivery token are required.", 403)

    expected_token = delivery_token(paypal_id)
    if not hmac.compare_digest(supplied_token, expected_token):
        return error("Delivery authorization failed.", 403)

    with db() as conn:
        row = conn.execute(
            """
            SELECT p.title, p.content, o.status
            FROM orders o
            JOIN products p ON p.id = o.product_id
            WHERE o.paypal_order_id = ?
            """,
            (paypal_id,)
        ).fetchone()

    if not row or row["status"] != "COMPLETED":
        return error("Verified payment is required before delivery.", 403)

    return jsonify({
        "title": row["title"],
        "content": row["content"],
        "delivery_status": "AUTHORIZED",
    })


def make_purchase_pdf(title, content, page_count=5):
    import textwrap

    try:
        page_count = max(1, min(20, int(page_count)))
    except (TypeError, ValueError):
        page_count = 5

    lines = [str(title), "", "Purchased through CreatorPay AI", ""]
    for paragraph in str(content).splitlines():
        lines.extend(textwrap.wrap(paragraph, 85) or [""])

    total = len(lines)
    pages = [
        lines[total * i // page_count:total * (i + 1) // page_count]
        for i in range(page_count)
    ]

    max_lines = max(1, max(len(page) for page in pages))
    leading = min(14.0, 780.0 / max_lines)
    font_size = min(10.0, max(4.0, leading * 0.70))

    def pdf_string(value):
        raw = str(value).encode("cp1252", errors="replace")
        escaped = bytearray()
        for b in raw:
            if b in (40, 41, 92):
                escaped.extend(b"\\" + bytes([b]))
            elif b < 32 or b > 126:
                escaped.extend(f"\\{b:03o}".encode("ascii"))
            else:
                escaped.append(b)
        return b"(" + bytes(escaped) + b")"

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    ]
    page_ids = []

    for i, page_lines in enumerate(pages):
        page_id = 4 + i * 2
        stream_id = page_id + 1
        page_ids.append(page_id)

        commands = [
            b"BT",
            f"/F1 {font_size:.2f} Tf".encode("ascii"),
            b"50 790 Td",
            f"{leading:.2f} TL".encode("ascii")
        ]
        for line in page_lines:
            commands.append(pdf_string(line) + b" Tj T*")
        commands.append(b"ET")
        stream = b"\n".join(commands)

        page = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents " +
            str(stream_id).encode() + b" 0 R >>"
        )
        stream_obj = (
            b"<< /Length " + str(len(stream)).encode() +
            b" >>\nstream\n" + stream + b"\nendstream"
        )
        objects.extend([page, stream_obj])

    objects[1] = (
        b"<< /Type /Pages /Kids [" +
        b" ".join(str(x).encode() + b" 0 R" for x in page_ids) +
        b"] /Count " + str(page_count).encode() + b" >>"
    )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(
            str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
        )

    xref = len(output)
    output.extend(
        b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
        b"0000000000 65535 f \n"
    )
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())

    output.extend(
        b"trailer\n<< /Size " + str(len(objects) + 1).encode() +
        b" /Root 1 0 R >>\nstartxref\n" +
        str(xref).encode() + b"\n%%EOF\n"
    )
    return bytes(output)


@app.post("/api/orders/download")
def download_purchased_product():
    data = request.get_json(silent=True) or {}
    paypal_id = str(data.get("order_id", ""))
    supplied_token = str(data.get("delivery_token", ""))
    file_format = str(data.get("format", "")).lower()

    if file_format not in ("pdf", "txt"):
        return error("Choose PDF or TXT format.", 400)

    if not paypal_id or len(paypal_id) > 80 or len(supplied_token) != 64:
        return error("A valid completed order and delivery token are required.", 403)

    expected_token = delivery_token(paypal_id)
    if not hmac.compare_digest(supplied_token, expected_token):
        return error("Download authorization failed.", 403)

    with db() as conn:
        row = conn.execute(
            """
            SELECT p.title, p.content, p.page_count, o.status
            FROM orders o
            JOIN products p ON p.id = o.product_id
            WHERE o.paypal_order_id = ?
            """,
            (paypal_id,)
        ).fetchone()

    if not row or row["status"] != "COMPLETED":
        return error("Verified payment is required before downloading.", 403)

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", row["title"]).strip("-")
    safe_name = safe_name or "creatorpay-purchase"

    if file_format == "txt":
        file_bytes = (row["title"] + "\n\n" + row["content"]).encode("utf-8")
        mimetype = "text/plain; charset=utf-8"
        filename = safe_name + ".txt"
    else:
        file_bytes = make_purchase_pdf(
            row["title"], row["content"], row["page_count"]
        )
        mimetype = "application/pdf"
        filename = safe_name + ".pdf"

    response = Response(file_bytes, mimetype=mimetype)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)

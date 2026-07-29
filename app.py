"""
@all toolz market place — standalone web store.

Runs as its own Render web service, completely separate from the Telegram
bot's process. Shares the SAME Turso database as the bot (reads the same
products/categories tables) but has its OWN checkout flow — manual bank
transfer, confirmed by an admin — and its OWN orders table (web_orders), so
a purchase made on the website never gets confused with a Telegram order.

Env vars needed (set these in Render's Environment tab for THIS service):
  TURSO_DATABASE_URL     - same value as the bot uses
  TURSO_AUTH_TOKEN       - same value as the bot uses
  BOT_TOKEN              - same bot token the Telegram bot uses (only used
                           here to send plain HTTP notifications — this
                           file does NOT run the bot itself)
  ADMIN_IDS              - same comma-separated admin Telegram IDs as the bot
  ADMIN_PANEL_KEY         - a long random string; the shared "password" for
                           the /admin order-review page (e.g. ?key=THIS)
  BANK_NAME              - shown to customers on the payment instructions page
  BANK_ACCOUNT_NUMBER    - shown to customers on the payment instructions page
  BANK_ACCOUNT_NAME      - shown to customers on the payment instructions page
  FLASK_SECRET_KEY       - any random string, used to sign the session cookie
"""

import os
import time
import uuid
import logging

import requests
import libsql
from flask import (
    Flask, render_template, request, redirect, url_for, flash, abort,
    send_from_directory,
)

logging.basicConfig(level=logging.INFO)

app = Flask(
    __name__,
    # Your .html files sit in the same folder as app.py, not inside a
    # "templates/" subfolder (Flask's default expectation) — this tells
    # Flask to look right here instead, so every page stops throwing
    # "TemplateNotFound" without needing to move a single file on GitHub.
    template_folder=os.path.dirname(os.path.abspath(__file__)) or ".",
)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [x.strip() for x in _raw_admins.split(",") if x.strip().isdigit()]
ADMIN_PANEL_KEY = os.getenv("ADMIN_PANEL_KEY", "")

BANK_NAME = os.getenv("BANK_NAME", "Set BANK_NAME in Render")
BANK_ACCOUNT_NUMBER = os.getenv("BANK_ACCOUNT_NUMBER", "0000000000")
BANK_ACCOUNT_NAME = os.getenv("BANK_ACCOUNT_NAME", "Set BANK_ACCOUNT_NAME in Render")


# ---------------------------------------------------------------------------
# DATABASE — same Turso DB as the bot, plus one new table just for web orders
# ---------------------------------------------------------------------------

def _get_conn():
    if TURSO_DATABASE_URL:
        conn = libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    else:
        conn = libsql.connect("marketplace.db")
    try:
        conn.isolation_level = None
    except Exception:
        pass
    return conn


def _run_with_retry(fn, attempts: int = 3, delay: float = 0.4):
    last_err = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(delay * (attempt + 1))
    raise last_err


def _row_to_dict(cur, row):
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def fetchall(sql, params=()):
    def _run():
        conn = _get_conn()
        try:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            return [_row_to_dict(cur, r) for r in rows]
        finally:
            conn.close()
    return _run_with_retry(_run)


def fetchone(sql, params=()):
    rows = fetchall(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    def _run():
        conn = _get_conn()
        try:
            cur = conn.execute(sql, params)
            try:
                conn.commit()
            except Exception:
                pass
            return cur.lastrowid
        finally:
            conn.close()
    return _run_with_retry(_run)


WEB_ORDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_orders (
    web_order_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id         INTEGER NOT NULL,
    quantity           INTEGER NOT NULL,
    total_price        REAL NOT NULL,
    customer_name      TEXT,
    customer_email     TEXT,
    customer_phone     TEXT,
    order_reference    TEXT UNIQUE,
    status             TEXT NOT NULL DEFAULT 'pending_payment',
    created_at         TEXT DEFAULT CURRENT_TIMESTAMP
);
"""
# status values: pending_payment -> awaiting_review -> confirmed | rejected


def init_db():
    def _run():
        conn = _get_conn()
        try:
            conn.execute(WEB_ORDERS_SCHEMA)
            try:
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()
    _run_with_retry(_run)


# ---------------------------------------------------------------------------
# TELEGRAM NOTIFICATIONS — plain HTTP calls, no bot process running here
# ---------------------------------------------------------------------------

def notify_admins(text: str):
    if not BOT_TOKEN:
        return
    for admin_id in ADMIN_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": admin_id, "text": text},
                timeout=8,
            )
        except Exception:
            logging.exception("Failed to notify admin %s", admin_id)


def send_proof_photo_to_admins(file_storage, caption: str):
    """Forward the uploaded payment screenshot to every admin via Telegram."""
    if not BOT_TOKEN:
        return
    file_bytes = file_storage.read()
    filename = file_storage.filename or "proof.jpg"
    mimetype = file_storage.mimetype or "image/jpeg"
    for admin_id in ADMIN_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": admin_id, "caption": caption},
                files={"photo": (filename, file_bytes, mimetype)},
                timeout=20,
            )
        except Exception:
            logging.exception("Failed to send proof photo to admin %s", admin_id)


# ---------------------------------------------------------------------------
# STORE DATA HELPERS
# ---------------------------------------------------------------------------

def get_categories():
    return fetchall("SELECT * FROM categories ORDER BY name")


def get_active_products(category_id=None):
    if category_id:
        return fetchall(
            "SELECT * FROM products WHERE status='active' AND stock_qty > 0 AND category_id=? ORDER BY created_at DESC",
            (category_id,),
        )
    return fetchall(
        "SELECT * FROM products WHERE status='active' AND stock_qty > 0 ORDER BY created_at DESC"
    )


def get_product(product_id):
    return fetchone("SELECT * FROM products WHERE product_id=?", (product_id,))


def get_order(reference):
    return fetchone("SELECT * FROM web_orders WHERE order_reference=?", (reference,))


def decrement_stock(product_id, quantity):
    execute(
        "UPDATE products SET stock_qty = MAX(0, stock_qty - ?) WHERE product_id=?",
        (quantity, product_id),
    )


# ---------------------------------------------------------------------------
# ROUTES — storefront
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    category_id = request.args.get("category", type=int)
    categories = get_categories()
    products = get_active_products(category_id)
    return render_template(
        "index.html", categories=categories, products=products, active_category=category_id
    )


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    product = get_product(product_id)
    if not product or product["status"] != "active" or product["stock_qty"] <= 0:
        flash("That item isn't available anymore.")
        return redirect(url_for("home"))
    return render_template("product.html", product=product)


@app.route("/product/<int:product_id>/checkout", methods=["GET", "POST"])
def checkout(product_id):
    product = get_product(product_id)
    if not product or product["status"] != "active" or product["stock_qty"] <= 0:
        flash("That item isn't available anymore.")
        return redirect(url_for("home"))

    if request.method == "POST":
        try:
            quantity = max(1, int(request.form.get("quantity", 1)))
        except ValueError:
            quantity = 1
        quantity = min(quantity, product["stock_qty"])
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()

        if not name or not email or not phone:
            flash("Please fill in your name, email, and phone number.")
            return render_template("checkout.html", product=product)

        total_price = product["price"] * quantity
        reference = f"web_{uuid.uuid4().hex[:20]}"

        execute(
            """INSERT INTO web_orders
               (product_id, quantity, total_price, customer_name, customer_email,
                customer_phone, order_reference, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_payment')""",
            (product_id, quantity, total_price, name, email, phone, reference),
        )

        notify_admins(
            f"🆕 New WEBSITE order #{reference}\n"
            f"Item: {product['title']}\n"
            f"Qty: {quantity} — Total: ₦{total_price:.2f}\n"
            f"Buyer: {name} | {phone} | {email}\n"
            f"Status: waiting for bank transfer + proof."
        )

        return redirect(url_for("payment_instructions", reference=reference))

    return render_template("checkout.html", product=product)


@app.route("/order/<reference>/pay", methods=["GET", "POST"])
def payment_instructions(reference):
    order = get_order(reference)
    if not order:
        flash("We couldn't find that order.")
        return redirect(url_for("home"))
    product = get_product(order["product_id"])

    if order["status"] not in ("pending_payment", "awaiting_review"):
        return redirect(url_for("order_status", reference=reference))

    if request.method == "POST":
        proof = request.files.get("proof")
        if not proof or not proof.filename:
            flash("Please attach a screenshot of your bank transfer.")
            return render_template(
                "payment_instructions.html", order=order, product=product,
                bank_name=BANK_NAME, bank_account_number=BANK_ACCOUNT_NUMBER,
                bank_account_name=BANK_ACCOUNT_NAME,
            )

        send_proof_photo_to_admins(
            proof,
            caption=(
                f"💳 Payment proof for order #{order['order_reference']}\n"
                f"Item: {product['title'] if product else '(deleted product)'}\n"
                f"Qty: {order['quantity']} — Total: ₦{order['total_price']:.2f}\n"
                f"Buyer: {order['customer_name']} | {order['customer_phone']} | {order['customer_email']}\n"
                f"Reply in the admin panel to confirm or reject this order."
            ),
        )
        execute(
            "UPDATE web_orders SET status='awaiting_review' WHERE web_order_id=?",
            (order["web_order_id"],),
        )
        return redirect(url_for("order_status", reference=reference))

    return render_template(
        "payment_instructions.html", order=order, product=product,
        bank_name=BANK_NAME, bank_account_number=BANK_ACCOUNT_NUMBER,
        bank_account_name=BANK_ACCOUNT_NAME,
    )


@app.route("/order/<reference>/status")
def order_status(reference):
    order = get_order(reference)
    if not order:
        flash("We couldn't find that order.")
        return redirect(url_for("home"))
    product = get_product(order["product_id"])
    return render_template("order_status.html", order=order, product=product)


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/style.css")
def style_css():
    # Serves ONLY this one file — deliberately not turning the whole app
    # folder into a public "static" directory, which would expose app.py's
    # source code (and anything else sitting next to it) at a guessable URL.
    folder = os.path.dirname(os.path.abspath(__file__)) or "."
    return send_from_directory(folder, "style.css", mimetype="text/css")


# ---------------------------------------------------------------------------
# ROUTES — lightweight admin review (protected by a shared key, not a login)
# ---------------------------------------------------------------------------

def _check_admin_key():
    key = request.args.get("key", "")
    if not ADMIN_PANEL_KEY or key != ADMIN_PANEL_KEY:
        abort(403)


@app.route("/admin")
def admin_orders():
    _check_admin_key()
    orders = fetchall(
        "SELECT * FROM web_orders WHERE status IN ('pending_payment','awaiting_review') "
        "ORDER BY created_at ASC"
    )
    products_by_id = {p["product_id"]: p for p in fetchall("SELECT * FROM products")}
    return render_template(
        "admin.html", orders=orders, products_by_id=products_by_id, key=ADMIN_PANEL_KEY
    )


@app.route("/admin/order/<reference>/confirm", methods=["POST"])
def admin_confirm(reference):
    _check_admin_key()
    order = get_order(reference)
    if order and order["status"] in ("pending_payment", "awaiting_review"):
        execute(
            "UPDATE web_orders SET status='confirmed' WHERE web_order_id=?",
            (order["web_order_id"],),
        )
        decrement_stock(order["product_id"], order["quantity"])
        notify_admins(f"✅ Order #{reference} marked CONFIRMED.")
    return redirect(url_for("admin_orders", key=ADMIN_PANEL_KEY))


@app.route("/admin/order/<reference>/reject", methods=["POST"])
def admin_reject(reference):
    _check_admin_key()
    order = get_order(reference)
    if order and order["status"] in ("pending_payment", "awaiting_review"):
        execute(
            "UPDATE web_orders SET status='rejected' WHERE web_order_id=?",
            (order["web_order_id"],),
        )
        notify_admins(f"❌ Order #{reference} marked REJECTED.")
    return redirect(url_for("admin_orders", key=ADMIN_PANEL_KEY))


init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

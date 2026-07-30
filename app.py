"""
Digital Products Marketplace — buyers and sellers both have real accounts.

- Buyers register, browse products, and pay by bank transfer (screenshot
  reviewed by you before the order is confirmed).
- Sellers register, get their own dashboard, and list their own digital
  products (title, description, price). No file hosting here — once an
  order is confirmed, the BUYER is shown the SELLER's WhatsApp/Telegram
  contact and reaches out directly to receive the item. That keeps the
  site simple: no file storage, no upload limits to worry about.
- You (the site owner) log in at /admin/login and confirm/reject payments
  after checking the screenshot sent to your Telegram.

Env vars needed (set these in Render's Environment tab):
  TURSO_DATABASE_URL, TURSO_AUTH_TOKEN   - your Turso database
  BOT_TOKEN, ADMIN_IDS                   - to receive payment-proof photos
  ADMIN_EMAIL, ADMIN_PASSWORD            - your own login for /admin/login
  BANK_NAME, BANK_ACCOUNT_NUMBER, BANK_ACCOUNT_NAME
  FLASK_SECRET_KEY                       - any random string
"""

import os
import time
import uuid
import logging
from functools import wraps

import requests
import sqlite3
from flask import (
    Flask, render_template, request, redirect, url_for, flash, abort, session
)
from werkzeug.security import generate_password_hash, check_password_hash

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_TELEGRAM_IDS = [x.strip() for x in _raw_admins.split(",") if x.strip().isdigit()]

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

BANK_NAME = os.getenv("BANK_NAME", "Set BANK_NAME in Render")
BANK_ACCOUNT_NUMBER = os.getenv("BANK_ACCOUNT_NUMBER", "0000000000")
BANK_ACCOUNT_NAME = os.getenv("BANK_ACCOUNT_NAME", "Set BANK_ACCOUNT_NAME in Render")


# ---------------------------------------------------------------------------
# DATABASE — talks to Turso over plain HTTPS (not the libsql Python driver,
# which conflicts with gunicorn's worker model). Falls back to a local
# SQLite file if TURSO_DATABASE_URL isn't set (e.g. quick local testing).
# ---------------------------------------------------------------------------

_LOCAL_DB_PATH = "marketplace2.db"


def _turso_base_url():
    url = (TURSO_DATABASE_URL or "").strip()
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    return url.rstrip("/")


def _turso_arg(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _turso_cell(cell):
    if not cell:
        return None
    t = cell.get("type")
    v = cell.get("value")
    if t == "null" or v is None:
        return None
    if t == "integer":
        return int(v)
    if t == "float":
        return float(v)
    return v


def _turso_execute(sql, params=()):
    url = f"{_turso_base_url()}/v2/pipeline"
    headers = {
        "Authorization": f"Bearer {TURSO_AUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": [_turso_arg(p) for p in params]}},
            {"type": "close"},
        ]
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()
    result = resp.json()["results"][0]
    if result.get("type") == "error":
        raise RuntimeError(result.get("error", {}).get("message", "Turso query failed"))
    exec_result = result["response"]["result"]
    cols = [c["name"] for c in exec_result.get("cols", [])]
    rows = exec_result.get("rows", [])
    last_id = exec_result.get("last_insert_rowid")
    return cols, rows, last_id


def _local_conn():
    conn = sqlite3.connect(_LOCAL_DB_PATH)
    conn.row_factory = sqlite3.Row
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


def fetchall(sql, params=()):
    if not TURSO_DATABASE_URL:
        def _run():
            conn = _local_conn()
            try:
                cur = conn.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()
        return _run_with_retry(_run)

    def _run():
        cols, rows, _ = _turso_execute(sql, params)
        return [{c: _turso_cell(v) for c, v in zip(cols, row)} for row in rows]
    return _run_with_retry(_run)


def fetchone(sql, params=()):
    rows = fetchall(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    if not TURSO_DATABASE_URL:
        def _run():
            conn = _local_conn()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()
        return _run_with_retry(_run)

    def _run():
        _, _, last_id = _turso_execute(sql, params)
        return int(last_id) if last_id is not None else None
    return _run_with_retry(_run)


SCHEMA = """
CREATE TABLE IF NOT EXISTS mp_users (
    user_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT UNIQUE NOT NULL,
    password_hash     TEXT NOT NULL,
    name              TEXT NOT NULL,
    is_seller         INTEGER NOT NULL DEFAULT 0,
    contact_platform  TEXT,
    contact_value     TEXT,
    profile_photo     TEXT,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# Safe to re-run: adds the column to a database that already has mp_users
# from before this feature existed. Ignored if the column is already there.
ALTER_ADD_PROFILE_PHOTO = "ALTER TABLE mp_users ADD COLUMN profile_photo TEXT"

SCHEMA_PRODUCTS = """
CREATE TABLE IF NOT EXISTS mp_products (
    product_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id     INTEGER NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT,
    price         REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_ORDERS = """
CREATE TABLE IF NOT EXISTS mp_orders (
    order_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_reference  TEXT UNIQUE NOT NULL,
    buyer_id         INTEGER NOT NULL,
    product_id       INTEGER NOT NULL,
    seller_id        INTEGER NOT NULL,
    total_price      REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending_payment',
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP
);
"""
# order status values: pending_payment -> awaiting_review -> confirmed | rejected

SCHEMA_POSTS = """
CREATE TABLE IF NOT EXISTS mp_posts (
    post_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_LIKES = """
CREATE TABLE IF NOT EXISTS mp_likes (
    like_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(post_id, user_id)
);
"""

SCHEMA_COMMENTS = """
CREATE TABLE IF NOT EXISTS mp_comments (
    comment_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db():
    try:
        for stmt in (SCHEMA, SCHEMA_PRODUCTS, SCHEMA_ORDERS, SCHEMA_POSTS, SCHEMA_LIKES, SCHEMA_COMMENTS):
            if not TURSO_DATABASE_URL:
                conn = _local_conn()
                try:
                    conn.execute(stmt)
                    conn.commit()
                finally:
                    conn.close()
            else:
                _run_with_retry(lambda s=stmt: _turso_execute(s))

        # Add profile_photo to any database that predates this feature.
        try:
            if not TURSO_DATABASE_URL:
                conn = _local_conn()
                try:
                    conn.execute(ALTER_ADD_PROFILE_PHOTO)
                    conn.commit()
                finally:
                    conn.close()
            else:
                _turso_execute(ALTER_ADD_PROFILE_PHOTO)
        except Exception:
            pass  # column already exists — that's fine
    except Exception:
        logging.exception("init_db failed at startup; will retry lazily on first request")


# ---------------------------------------------------------------------------
# TELEGRAM NOTIFICATIONS
# ---------------------------------------------------------------------------

def notify_admins(text: str):
    if not BOT_TOKEN:
        return
    for admin_id in ADMIN_TELEGRAM_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": admin_id, "text": text},
                timeout=8,
            )
        except Exception:
            logging.exception("Failed to notify admin %s", admin_id)


def send_proof_photo_to_admins(file_storage, caption: str):
    if not BOT_TOKEN:
        return
    file_bytes = file_storage.read()
    filename = file_storage.filename or "proof.jpg"
    mimetype = file_storage.mimetype or "image/jpeg"
    for admin_id in ADMIN_TELEGRAM_IDS:
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
# AUTH HELPERS
# ---------------------------------------------------------------------------

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return fetchone("SELECT * FROM mp_users WHERE user_id=?", (uid,))


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in first.")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def seller_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            flash("Please log in first.")
            return redirect(url_for("login", next=request.path))
        if not user["is_seller"]:
            flash("You need a seller account for that.")
            return redirect(url_for("home"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


def contact_link(platform, value):
    if not value:
        return None
    value = value.strip()
    if platform == "whatsapp":
        digits = "".join(ch for ch in value if ch.isdigit())
        return f"https://wa.me/{digits}"
    if platform == "telegram":
        handle = value.lstrip("@")
        return f"https://t.me/{handle}"
    return None


# ---------------------------------------------------------------------------
# ROUTES — auth
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        account_type = request.form.get("account_type", "buyer")
        contact_platform = request.form.get("contact_platform", "").strip()
        contact_value = request.form.get("contact_value", "").strip()
        wants_seller = account_type in ("seller", "both")

        if not name or not email or "@" not in email or len(password) < 6:
            flash("Please fill in a valid name, email, and a password of at least 6 characters.")
            return render_template("register.html")

        if wants_seller and (not contact_platform or not contact_value):
            flash("Sellers need to provide a WhatsApp or Telegram contact so buyers can reach them.")
            return render_template("register.html")

        existing = fetchone("SELECT user_id FROM mp_users WHERE email=?", (email,))
        if existing:
            flash("An account with that email already exists — try logging in instead.")
            return redirect(url_for("login"))

        password_hash = generate_password_hash(password)
        user_id = execute(
            """INSERT INTO mp_users (email, password_hash, name, is_seller, contact_platform, contact_value)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (email, password_hash, name, 1 if wants_seller else 0,
             contact_platform if wants_seller else None,
             contact_value if wants_seller else None),
        )
        session["user_id"] = user_id
        flash("Welcome! Your account is ready.")
        return redirect(url_for("home"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.args.get("next") or request.form.get("next") or url_for("home")
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = fetchone("SELECT * FROM mp_users WHERE email=?", (email,))
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.")
            return render_template("login.html", next=next_url)
        session["user_id"] = user["user_id"]
        return redirect(next_url)
    return render_template("login.html", next=next_url)


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# ROUTES — storefront
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    products = fetchall(
        """SELECT p.*, u.name AS seller_name FROM mp_products p
           JOIN mp_users u ON u.user_id = p.seller_id
           WHERE p.status='active' ORDER BY p.created_at DESC"""
    )
    return render_template("index.html", products=products)


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    product = fetchone(
        """SELECT p.*, u.name AS seller_name FROM mp_products p
           JOIN mp_users u ON u.user_id = p.seller_id
           WHERE p.product_id=?""",
        (product_id,),
    )
    if not product or product["status"] != "active":
        flash("That item isn't available anymore.")
        return redirect(url_for("home"))
    return render_template("product.html", product=product)


@app.route("/product/<int:product_id>/buy", methods=["POST"])
@login_required
def buy_product(product_id):
    product = fetchone("SELECT * FROM mp_products WHERE product_id=?", (product_id,))
    if not product or product["status"] != "active":
        flash("That item isn't available anymore.")
        return redirect(url_for("home"))

    user = current_user()
    if user["user_id"] == product["seller_id"]:
        flash("You can't buy your own listing.")
        return redirect(url_for("product_detail", product_id=product_id))

    reference = f"ord_{uuid.uuid4().hex[:20]}"
    execute(
        """INSERT INTO mp_orders (order_reference, buyer_id, product_id, seller_id, total_price, status)
           VALUES (?, ?, ?, ?, ?, 'pending_payment')""",
        (reference, user["user_id"], product_id, product["seller_id"], product["price"]),
    )
    notify_admins(
        f"🆕 New order #{reference}\n"
        f"Item: {product['title']} — ₦{product['price']:.2f}\n"
        f"Buyer: {user['name']} ({user['email']})\n"
        f"Status: waiting for bank transfer + proof."
    )
    return redirect(url_for("payment_instructions", reference=reference))


@app.route("/order/<reference>/pay", methods=["GET", "POST"])
@login_required
def payment_instructions(reference):
    order = fetchone("SELECT * FROM mp_orders WHERE order_reference=?", (reference,))
    if not order or order["buyer_id"] != current_user()["user_id"]:
        flash("We couldn't find that order.")
        return redirect(url_for("home"))
    product = fetchone("SELECT * FROM mp_products WHERE product_id=?", (order["product_id"],))

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
                f"Total: ₦{order['total_price']:.2f}\n"
                f"Reply in /admin to confirm or reject this order."
            ),
        )
        execute("UPDATE mp_orders SET status='awaiting_review' WHERE order_id=?", (order["order_id"],))
        return redirect(url_for("order_status", reference=reference))

    return render_template(
        "payment_instructions.html", order=order, product=product,
        bank_name=BANK_NAME, bank_account_number=BANK_ACCOUNT_NUMBER,
        bank_account_name=BANK_ACCOUNT_NAME,
    )


@app.route("/order/<reference>/status")
@login_required
def order_status(reference):
    order = fetchone("SELECT * FROM mp_orders WHERE order_reference=?", (reference,))
    if not order or order["buyer_id"] != current_user()["user_id"]:
        flash("We couldn't find that order.")
        return redirect(url_for("home"))
    product = fetchone("SELECT * FROM mp_products WHERE product_id=?", (order["product_id"],))
    seller = fetchone("SELECT * FROM mp_users WHERE user_id=?", (order["seller_id"],))
    seller_link = None
    if order["status"] == "confirmed" and seller:
        seller_link = contact_link(seller["contact_platform"], seller["contact_value"])
    return render_template(
        "order_status.html", order=order, product=product, seller=seller, seller_link=seller_link
    )


# ---------------------------------------------------------------------------
# ROUTES — seller dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@seller_required
def dashboard():
    user = current_user()
    products = fetchall(
        "SELECT * FROM mp_products WHERE seller_id=? ORDER BY created_at DESC", (user["user_id"],)
    )
    orders = fetchall(
        """SELECT o.*, p.title AS product_title FROM mp_orders o
           JOIN mp_products p ON p.product_id = o.product_id
           WHERE o.seller_id=? ORDER BY o.created_at DESC""",
        (user["user_id"],),
    )
    return render_template("dashboard.html", products=products, orders=orders)


@app.route("/dashboard/products/new", methods=["GET", "POST"])
@seller_required
def product_new():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        try:
            price = float(request.form.get("price", "0"))
        except ValueError:
            price = 0
        if not title or price <= 0:
            flash("Please enter a title and a valid price.")
            return render_template("product_form.html", product=None)
        execute(
            "INSERT INTO mp_products (seller_id, title, description, price, status) VALUES (?, ?, ?, ?, 'active')",
            (current_user()["user_id"], title, description, price),
        )
        flash("Listing added.")
        return redirect(url_for("dashboard"))
    return render_template("product_form.html", product=None)


@app.route("/dashboard/products/<int:product_id>/edit", methods=["GET", "POST"])
@seller_required
def product_edit(product_id):
    product = fetchone("SELECT * FROM mp_products WHERE product_id=?", (product_id,))
    if not product or product["seller_id"] != current_user()["user_id"]:
        flash("That listing isn't yours.")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        try:
            price = float(request.form.get("price", "0"))
        except ValueError:
            price = 0
        if not title or price <= 0:
            flash("Please enter a title and a valid price.")
            return render_template("product_form.html", product=product)
        execute(
            "UPDATE mp_products SET title=?, description=?, price=? WHERE product_id=?",
            (title, description, price, product_id),
        )
        flash("Listing updated.")
        return redirect(url_for("dashboard"))

    return render_template("product_form.html", product=product)


@app.route("/dashboard/products/<int:product_id>/toggle", methods=["POST"])
@seller_required
def product_toggle(product_id):
    product = fetchone("SELECT * FROM mp_products WHERE product_id=?", (product_id,))
    if product and product["seller_id"] == current_user()["user_id"]:
        new_status = "inactive" if product["status"] == "active" else "active"
        execute("UPDATE mp_products SET status=? WHERE product_id=?", (new_status, product_id))
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# ROUTES — admin (real login, not a URL key)
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if (ADMIN_EMAIL and ADMIN_PASSWORD
                and email == ADMIN_EMAIL.strip().lower()
                and password == ADMIN_PASSWORD):
            session["is_admin"] = True
            return redirect(url_for("admin_orders"))
        flash("Incorrect admin email or password.")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_orders():
    orders = fetchall(
        """SELECT o.*, p.title AS product_title,
                  b.name AS buyer_name, b.email AS buyer_email,
                  s.name AS seller_name, s.email AS seller_email
           FROM mp_orders o
           JOIN mp_products p ON p.product_id = o.product_id
           JOIN mp_users b ON b.user_id = o.buyer_id
           JOIN mp_users s ON s.user_id = o.seller_id
           WHERE o.status IN ('pending_payment', 'awaiting_review')
           ORDER BY o.created_at ASC"""
    )
    return render_template("admin.html", orders=orders)


@app.route("/admin/order/<reference>/confirm", methods=["POST"])
@admin_required
def admin_confirm(reference):
    order = fetchone("SELECT * FROM mp_orders WHERE order_reference=?", (reference,))
    if order and order["status"] in ("pending_payment", "awaiting_review"):
        execute("UPDATE mp_orders SET status='confirmed' WHERE order_id=?", (order["order_id"],))
        notify_admins(f"✅ Order #{reference} marked CONFIRMED.")
    return redirect(url_for("admin_orders"))


@app.route("/admin/order/<reference>/reject", methods=["POST"])
@admin_required
def admin_reject(reference):
    order = fetchone("SELECT * FROM mp_orders WHERE order_reference=?", (reference,))
    if order and order["status"] in ("pending_payment", "awaiting_review"):
        execute("UPDATE mp_orders SET status='rejected' WHERE order_id=?", (order["order_id"],))
        notify_admins(f"❌ Order #{reference} marked REJECTED.")
    return redirect(url_for("admin_orders"))


@app.route("/health")
def health():
    return {"status": "ok"}, 200


# ---------------------------------------------------------------------------
# ROUTES — social feed, profiles, likes, comments
# ---------------------------------------------------------------------------

def _posts_with_counts(where_clause="", params=()):
    posts = fetchall(
        f"""SELECT p.*, u.name AS author_name FROM mp_posts p
            JOIN mp_users u ON u.user_id = p.user_id
            {where_clause}
            ORDER BY p.created_at DESC""",
        params,
    )
    me = current_user()
    for post in posts:
        likes = fetchall("SELECT user_id FROM mp_likes WHERE post_id=?", (post["post_id"],))
        post["like_count"] = len(likes)
        post["liked_by_me"] = bool(me and any(l["user_id"] == me["user_id"] for l in likes))
        post["comments"] = fetchall(
            """SELECT c.*, u.name AS author_name FROM mp_comments c
               JOIN mp_users u ON u.user_id = c.user_id
               WHERE c.post_id=? ORDER BY c.created_at ASC""",
            (post["post_id"],),
        )
    return posts


@app.route("/feed")
def feed():
    posts = _posts_with_counts()
    return render_template("feed.html", posts=posts)


@app.route("/feed/new", methods=["POST"])
@login_required
def post_new():
    content = request.form.get("content", "").strip()
    if content:
        execute(
            "INSERT INTO mp_posts (user_id, content) VALUES (?, ?)",
            (current_user()["user_id"], content),
        )
    return redirect(url_for("feed"))


@app.route("/post/<int:post_id>/like", methods=["POST"])
@login_required
def post_like(post_id):
    user = current_user()
    existing = fetchone(
        "SELECT * FROM mp_likes WHERE post_id=? AND user_id=?", (post_id, user["user_id"])
    )
    if existing:
        execute("DELETE FROM mp_likes WHERE post_id=? AND user_id=?", (post_id, user["user_id"]))
    else:
        execute("INSERT INTO mp_likes (post_id, user_id) VALUES (?, ?)", (post_id, user["user_id"]))
    return redirect(request.referrer or url_for("feed"))


@app.route("/post/<int:post_id>/comment", methods=["POST"])
@login_required
def post_comment(post_id):
    content = request.form.get("content", "").strip()
    if content:
        execute(
            "INSERT INTO mp_comments (post_id, user_id, content) VALUES (?, ?, ?)",
            (post_id, current_user()["user_id"], content),
        )
    return redirect(request.referrer or url_for("feed"))


@app.route("/profile/<int:user_id>")
def profile(user_id):
    profile_user = fetchone("SELECT * FROM mp_users WHERE user_id=?", (user_id,))
    if not profile_user:
        flash("That profile doesn't exist.")
        return redirect(url_for("feed"))
    posts = _posts_with_counts("WHERE p.user_id=?", (user_id,))
    listings = []
    if profile_user["is_seller"]:
        listings = fetchall(
            "SELECT * FROM mp_products WHERE seller_id=? AND status='active' ORDER BY created_at DESC",
            (user_id,),
        )
    return render_template("profile.html", profile_user=profile_user, posts=posts, listings=listings)


MAX_PHOTO_BYTES = 400 * 1024  # 400 KB — kept small since photos are stored as text in the database


@app.route("/profile/photo", methods=["POST"])
@login_required
def profile_photo_upload():
    photo = request.files.get("photo")
    if not photo or not photo.filename:
        flash("Please choose an image.")
        return redirect(url_for("profile", user_id=current_user()["user_id"]))

    data = photo.read()
    if len(data) > MAX_PHOTO_BYTES:
        flash("That image is too large — please use one under 400KB.")
        return redirect(url_for("profile", user_id=current_user()["user_id"]))

    mimetype = photo.mimetype or "image/jpeg"
    if not mimetype.startswith("image/"):
        flash("Please upload an image file.")
        return redirect(url_for("profile", user_id=current_user()["user_id"]))

    import base64
    data_uri = f"data:{mimetype};base64,{base64.b64encode(data).decode()}"
    execute(
        "UPDATE mp_users SET profile_photo=? WHERE user_id=?",
        (data_uri, current_user()["user_id"]),
    )
    return redirect(url_for("profile", user_id=current_user()["user_id"]))


@app.route("/people")
def people():
    users = fetchall("SELECT * FROM mp_users ORDER BY name ASC")
    return render_template("people.html", users=users)


@app.route("/menu")
def menu_page():
    return render_template("menu.html")


init_db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

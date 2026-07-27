"""
Telegram Marketplace Bot — Phase 1 (Foundation) + Phase 4 (Orders)
Single-file version: everything (config, database, keyboards, handlers) lives here.

Setup:
    pip install -r requirements.txt
    export BOT_TOKEN=your_token_here      (or put it in a .env file)
    export ADMIN_IDS=123456789            (your numeric Telegram user ID)
    python bot.py
"""

import os
import time
import asyncio
import logging
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import libsql
import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    CopyTextButton,
)

logging.basicConfig(level=logging.INFO)
load_dotenv()

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Set at startup from bot.get_me() — used to build referral links
# (https://t.me/<username>?start=<code>). Falls back to an env var override
# in case fetching it ever fails.
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

# ---------------------------------------------------------------------------
# DATABASE CONNECTION
#
# Uses Turso (a free, hosted, persistent SQLite-compatible database) when
# TURSO_DATABASE_URL / TURSO_AUTH_TOKEN are set in the environment — this is
# the recommended setup, since it survives Render restarts/sleeps for free.
#
# If those aren't set, it falls back to a local file (DB_PATH) for local
# testing only — on Render's free tier, a local file gets wiped on every
# restart, so don't rely on it in production.
# ---------------------------------------------------------------------------
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "marketplace.db")


def _get_conn():
    if TURSO_DATABASE_URL:
        conn = libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
    else:
        conn = libsql.connect(DB_PATH)
    try:
        conn.isolation_level = None  # autocommit — avoids a separate commit()
        # round-trip, which can fail with a transient "stream not found"
        # error on Turso under Render's free tier.
    except Exception:
        pass
    return conn


def _run_with_retry(fn, attempts: int = 3, delay: float = 0.4):
    """Run a sync DB function, retrying on transient Turso/network errors
    (e.g. 'stream not found') instead of crashing the handler."""
    last_err = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(delay * (attempt + 1))
                continue
    raise last_err

_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()}

# How many units left counts as "running low" (0 is always "out of stock"
# regardless of this setting). Override with LOW_STOCK_THRESHOLD env var.
LOW_STOCK_THRESHOLD = int(os.getenv("LOW_STOCK_THRESHOLD", "3"))

# Orders that sit at "awaiting_payment" (buyer never uploaded a screenshot)
# longer than this get auto-expired. Override with ORDER_EXPIRY_MINUTES env var.
ORDER_EXPIRY_MINUTES = float(os.getenv("ORDER_EXPIRY_MINUTES", "20"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Set it as an env var or in a .env file.")

# =========================================================
# PAYMENT METHODS (Phase 2)
#
# No gateway has public verification APIs without a registered merchant
# account, so this bot doesn't auto-confirm payments. Instead: the buyer
# picks a method, sees your account/wallet details for it, pays manually,
# then uploads a screenshot for seller/admin approval (Phase 4).
#
# Fill in your real details below, or override any of them with an env var
# of the same name (e.g. set OPAY_DETAILS in Render's Environment tab).
# =========================================================

PAYMENT_METHODS = {
    "opay": {
        "label": "🇳🇬 Opay",
        "details": os.getenv("OPAY_DETAILS", "Opay\nAccount Name: <your name>"),
        "copy_value": os.getenv("OPAY_ACCOUNT_NUMBER", ""),
    },
    "palmpay": {
        "label": "🇳🇬 PalmPay",
        "details": os.getenv("PALMPAY_DETAILS", "PalmPay\nAccount Name: <your name>"),
        "copy_value": os.getenv("PALMPAY_ACCOUNT_NUMBER", ""),
    },
    "kuda": {
        "label": "🇳🇬 Kuda",
        "details": os.getenv("KUDA_DETAILS", "Kuda Bank\nAccount Name: <your name>"),
        "copy_value": os.getenv("KUDA_ACCOUNT_NUMBER", ""),
    },
    "moniepoint": {
        "label": "🇳🇬 Moniepoint",
        "details": os.getenv("MONIEPOINT_DETAILS", "Moniepoint\nAccount Name: <your name>"),
        "copy_value": os.getenv("MONIEPOINT_ACCOUNT_NUMBER", ""),
    },
    "bank_transfer": {
        "label": "🏦 Bank Transfer",
        "details": os.getenv("BANK_TRANSFER_DETAILS", "Bank: <your bank>\nAccount Name: <your name>"),
        "copy_value": os.getenv("BANK_TRANSFER_ACCOUNT_NUMBER", ""),
    },
    "usdt_trc20": {
        "label": "🪙 USDT (TRC20)",
        "details": os.getenv("USDT_TRC20_DETAILS", "USDT (TRC20)"),
        "copy_value": os.getenv("USDT_TRC20_WALLET", ""),
    },
    "bitcoin": {
        "label": "🪙 Bitcoin",
        "details": os.getenv("BITCOIN_DETAILS", "Bitcoin (BTC)"),
        "copy_value": os.getenv("BITCOIN_WALLET", ""),
    },
    "ethereum": {
        "label": "🪙 Ethereum",
        "details": os.getenv("ETHEREUM_DETAILS", "Ethereum (ETH)"),
        "copy_value": os.getenv("ETHEREUM_WALLET", ""),
    },
    "bnb": {
        "label": "🪙 BNB",
        "details": os.getenv("BNB_DETAILS", "BNB (BEP20)"),
        "copy_value": os.getenv("BNB_WALLET", ""),
    },
}

# Naira-priced product totals need converting when a buyer picks a crypto
# payment method. These map each crypto method key to its CoinGecko id
# (for fetching a live NGN rate) and a short display symbol.
CRYPTO_COINGECKO_IDS = {
    "usdt_trc20": "tether",
    "bitcoin": "bitcoin",
    "ethereum": "ethereum",
    "bnb": "binancecoin",
}
CRYPTO_SYMBOLS = {
    "usdt_trc20": "USDT",
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "bnb": "BNB",
}
CRYPTO_DECIMALS = {
    "usdt_trc20": 2,
    "bitcoin": 8,
    "ethereum": 6,
    "bnb": 6,
}

_crypto_rate_cache: dict = {}  # coin_id -> (rate_ngn_per_unit, fetched_at_epoch)
_CRYPTO_RATE_TTL_SECONDS = 300  # refresh at most every 5 minutes


async def get_ngn_rate(coin_id: str):
    """Returns how many Naira 1 unit of `coin_id` is worth right now, via
    CoinGecko's free API. Cached for 5 minutes so repeated purchases don't
    hammer the API. Returns None if the rate can't be fetched and there's
    no cached value to fall back on."""
    now = time.time()
    cached = _crypto_rate_cache.get(coin_id)
    if cached and (now - cached[1]) < _CRYPTO_RATE_TTL_SECONDS:
        return cached[0]
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=ngn"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json()
                rate = data.get(coin_id, {}).get("ngn")
                if rate:
                    _crypto_rate_cache[coin_id] = (rate, now)
                    return rate
    except Exception:
        logging.exception("Failed to fetch live NGN rate for %s", coin_id)
    # Fall back to a stale cached rate rather than nothing, if we have one.
    if cached:
        return cached[0]
    return None


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _looks_like_command(text: str) -> bool:
    """True if text is a stray slash-command (e.g. '/buy', '/browse') typed
    while the bot was mid-conversation waiting for a plain-text answer.
    Used to stop those commands from being swallowed as literal input
    (e.g. a shop name accidentally becoming '/browse')."""
    return bool(text) and text.startswith("/")


# =========================================================
# DATABASE
# =========================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    role        TEXT NOT NULL DEFAULT 'buyer',
    seller_status TEXT DEFAULT NULL,
    shop_name   TEXT,
    referred_by INTEGER,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payment_method_settings (
    method_key   TEXT PRIMARY KEY,
    details      TEXT NOT NULL,
    copy_value   TEXT
);

CREATE TABLE IF NOT EXISTS active_chats (
    buyer_id     INTEGER PRIMARY KEY,
    admin_id     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_focus (
    admin_id     INTEGER PRIMARY KEY,
    buyer_id     INTEGER
);

CREATE TABLE IF NOT EXISTS categories (
    category_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    emoji       TEXT DEFAULT '📦'
);

CREATE TABLE IF NOT EXISTS products (
    product_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id    INTEGER NOT NULL,
    category_id  INTEGER,
    title        TEXT NOT NULL,
    description  TEXT,
    price        REAL NOT NULL DEFAULT 0,
    stock_qty    INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (seller_id) REFERENCES users(user_id),
    FOREIGN KEY (category_id) REFERENCES categories(category_id)
);

CREATE TABLE IF NOT EXISTS product_photos (
    photo_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL,
    file_id      TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS favourites (
    user_id     INTEGER NOT NULL,
    product_id  INTEGER NOT NULL,
    PRIMARY KEY (user_id, product_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
    order_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_id      INTEGER NOT NULL,
    product_id    INTEGER NOT NULL,
    seller_id     INTEGER NOT NULL,
    quantity      INTEGER NOT NULL DEFAULT 1,
    total_price   REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'awaiting_payment',
    -- status: awaiting_payment | awaiting_approval | approved | rejected | completed | expired
    payment_method TEXT,
    screenshot_file_id TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (buyer_id) REFERENCES users(user_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id),
    FOREIGN KEY (seller_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL,
    buyer_id    INTEGER NOT NULL,
    product_id  INTEGER NOT NULL,
    rating      INTEGER NOT NULL,
    comment     TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (buyer_id) REFERENCES users(user_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
);
"""


async def _fetchall(sql: str, params: tuple = ()) -> list:
    """Run a SELECT and return rows as plain dicts (subscriptable like
    row['col'], same as aiosqlite.Row was)."""
    def _run():
        conn = _get_conn()
        try:
            cur = conn.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()
    return await asyncio.to_thread(_run_with_retry, _run)


async def _fetchone(sql: str, params: tuple = ()):
    rows = await _fetchall(sql, params)
    return rows[0] if rows else None


async def _execute(sql: str, params: tuple = ()) -> int:
    """Run an INSERT/UPDATE/DELETE. Returns lastrowid (for INSERTs)."""
    def _run():
        conn = _get_conn()
        try:
            cur = conn.execute(sql, params)
            try:
                conn.commit()
            except Exception:
                # Connection is in autocommit mode already, or Turso's
                # transient "stream not found" fired on this no-op commit —
                # the write itself (execute above) already succeeded.
                pass
            return cur.lastrowid
        finally:
            conn.close()
    return await asyncio.to_thread(_run_with_retry, _run)


async def init_db():
    def _run():
        conn = _get_conn()
        try:
            for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
                conn.execute(stmt)
            # Migration for databases created before the referral system was
            # added — CREATE TABLE IF NOT EXISTS won't add a column to a
            # table that already exists, so add it here if missing.
            try:
                conn.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
            except Exception:
                pass  # column already exists
            try:
                conn.execute("ALTER TABLE payment_method_settings ADD COLUMN copy_value TEXT")
            except Exception:
                pass  # column already exists
            try:
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()
    await asyncio.to_thread(_run_with_retry, _run)


async def upsert_user(user_id: int, username: str, full_name: str):
    await _execute(
        """INSERT INTO users (user_id, username, full_name)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
        (user_id, username, full_name),
    )


async def get_user(user_id: int):
    return await _fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))


async def set_referrer(user_id: int, referrer_id: int):
    """Records who referred this user — only takes effect if they don't
    already have a referrer, so it can't be overwritten later."""
    await _execute(
        "UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL",
        (referrer_id, user_id),
    )


async def count_referrals(user_id: int) -> int:
    row = await _fetchone("SELECT COUNT(*) AS c FROM users WHERE referred_by=?", (user_id,))
    return row["c"] if row else 0


async def list_referrals(user_id: int):
    return await _fetchall(
        "SELECT * FROM users WHERE referred_by=? ORDER BY created_at DESC", (user_id,)
    )


async def list_all_users():
    return await _fetchall("SELECT user_id FROM users")


async def request_seller_status(user_id: int, shop_name: str):
    await _execute(
        "UPDATE users SET seller_status='pending', shop_name=? WHERE user_id=?",
        (shop_name, user_id),
    )


async def set_seller_decision(user_id: int, approved: bool):
    status = "approved" if approved else "rejected"
    role = "seller" if approved else "buyer"
    await _execute(
        "UPDATE users SET seller_status=?, role=? WHERE user_id=?",
        (status, role, user_id),
    )


async def get_pending_sellers():
    return await _fetchall("SELECT * FROM users WHERE seller_status='pending'")


async def add_category(name: str, emoji: str = "📦"):
    await _execute(
        "INSERT OR IGNORE INTO categories (name, emoji) VALUES (?, ?)", (name, emoji)
    )


async def update_category(category_id: int, name: str, emoji: str):
    await _execute(
        "UPDATE categories SET name=?, emoji=? WHERE category_id=?", (name, emoji, category_id)
    )


async def count_active_products_in_category(category_id: int) -> int:
    row = await _fetchone(
        "SELECT COUNT(*) as cnt FROM products WHERE category_id=? AND status='active'",
        (category_id,),
    )
    return row["cnt"] if row else 0


async def remove_category(category_id: int):
    # Products (active or previously removed) still pointing at this
    # category must be orphaned first — the DB's foreign key rule blocks
    # deleting a category that anything still references.
    await _execute(
        "UPDATE products SET category_id=NULL WHERE category_id=?", (category_id,)
    )
    await _execute("DELETE FROM categories WHERE category_id=?", (category_id,))


async def set_payment_method_details(method_key: str, details: str, copy_value: str = None):
    await _execute(
        """INSERT INTO payment_method_settings (method_key, details, copy_value) VALUES (?, ?, ?)
           ON CONFLICT(method_key) DO UPDATE SET details=excluded.details, copy_value=excluded.copy_value"""
        if copy_value is not None else
        """INSERT INTO payment_method_settings (method_key, details) VALUES (?, ?)
           ON CONFLICT(method_key) DO UPDATE SET details=excluded.details""",
        (method_key, details, copy_value) if copy_value is not None else (method_key, details),
    )


async def set_payment_method_copy_value(method_key: str, copy_value: str):
    await _execute(
        """INSERT INTO payment_method_settings (method_key, details, copy_value)
           VALUES (?, COALESCE((SELECT details FROM payment_method_settings WHERE method_key=?), ''), ?)
           ON CONFLICT(method_key) DO UPDATE SET copy_value=excluded.copy_value""",
        (method_key, method_key, copy_value),
    )


async def get_payment_method_details(method_key: str):
    row = await _fetchone(
        "SELECT details FROM payment_method_settings WHERE method_key=?", (method_key,)
    )
    return row["details"] if row else None


async def get_payment_method_copy_value(method_key: str):
    row = await _fetchone(
        "SELECT copy_value FROM payment_method_settings WHERE method_key=?", (method_key,)
    )
    return row["copy_value"] if row and row["copy_value"] else None


async def start_chat(buyer_id: int, admin_id: int):
    """Opens (or re-opens) a chat with this buyer for this admin, and makes
    it the admin's focused chat — i.e. where their next plain messages go.
    Multiple buyers can be open per admin at once; this doesn't close any
    of the admin's other chats."""
    await _execute(
        """INSERT INTO active_chats (buyer_id, admin_id) VALUES (?, ?)
           ON CONFLICT(buyer_id) DO UPDATE SET admin_id=excluded.admin_id""",
        (buyer_id, admin_id),
    )
    await set_focus(admin_id, buyer_id)


async def end_chat(buyer_id: int):
    admin_id = await get_chat_admin(buyer_id)
    await _execute("DELETE FROM active_chats WHERE buyer_id=?", (buyer_id,))
    if admin_id is not None:
        focus = await get_focus(admin_id)
        if focus == buyer_id:
            # That was the focused chat — auto-switch to another of this
            # admin's remaining open chats, if any, else clear focus.
            remaining = await list_admin_chats(admin_id)
            await set_focus(admin_id, remaining[0]["buyer_id"] if remaining else None)


async def get_chat_admin(buyer_id: int):
    row = await _fetchone("SELECT admin_id FROM active_chats WHERE buyer_id=?", (buyer_id,))
    return row["admin_id"] if row else None


async def list_admin_chats(admin_id: int):
    """All buyers this admin currently has an open chat with, most recent
    row order is not guaranteed — fine for a short switch-list."""
    return await _fetchall(
        "SELECT buyer_id FROM active_chats WHERE admin_id=?", (admin_id,)
    )


async def set_focus(admin_id: int, buyer_id):
    await _execute(
        """INSERT INTO admin_focus (admin_id, buyer_id) VALUES (?, ?)
           ON CONFLICT(admin_id) DO UPDATE SET buyer_id=excluded.buyer_id""",
        (admin_id, buyer_id),
    )


async def get_focus(admin_id: int):
    row = await _fetchone("SELECT buyer_id FROM admin_focus WHERE admin_id=?", (admin_id,))
    return row["buyer_id"] if row else None


async def list_categories():
    return await _fetchall("SELECT * FROM categories ORDER BY name")


async def create_product(seller_id: int, category_id: int, title: str,
                          description: str, price: float, stock_qty: int) -> int:
    return await _execute(
        """INSERT INTO products (seller_id, category_id, title, description, price, stock_qty)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (seller_id, category_id, title, description, price, stock_qty),
    )


async def get_product(product_id: int):
    return await _fetchone("SELECT * FROM products WHERE product_id=?", (product_id,))


async def add_product_photo(product_id: int, file_id: str):
    await _execute(
        "INSERT INTO product_photos (product_id, file_id) VALUES (?, ?)",
        (product_id, file_id),
    )


async def get_product_photos(product_id: int):
    return await _fetchall(
        "SELECT * FROM product_photos WHERE product_id=?", (product_id,)
    )


async def list_products_by_category(category_id: int, limit: int = 20, offset: int = 0):
    return await _fetchall(
        """SELECT * FROM products WHERE category_id=? AND status='active'
           ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        (category_id, limit, offset),
    )


async def list_products_by_category_all(category_id: int, limit: int = 50):
    """Like list_products_by_category, but for admin use: includes every
    status (active/removed), not just active ones."""
    return await _fetchall(
        "SELECT * FROM products WHERE category_id=? ORDER BY created_at DESC LIMIT ?",
        (category_id, limit),
    )


_EDITABLE_PRODUCT_FIELDS = {"title", "description", "price", "stock_qty"}


async def update_product_field(product_id: int, field: str, value):
    if field not in _EDITABLE_PRODUCT_FIELDS:
        raise ValueError(f"Field '{field}' is not editable.")
    await _execute(f"UPDATE products SET {field}=? WHERE product_id=?", (value, product_id))


async def list_products_by_seller(seller_id: int):
    return await _fetchall(
        "SELECT * FROM products WHERE seller_id=? ORDER BY created_at DESC", (seller_id,)
    )


async def remove_product(product_id: int, seller_id: int) -> bool:
    """Soft-deletes a product (marks it removed instead of deleting the row)
    so past orders/reviews that reference it stay intact. Only works if the
    product actually belongs to this seller. Returns True if it removed
    something."""
    row = await _fetchone(
        "SELECT product_id FROM products WHERE product_id=? AND seller_id=?",
        (product_id, seller_id),
    )
    if not row:
        return False
    await _execute("UPDATE products SET status='removed' WHERE product_id=?", (product_id,))
    return True


async def search_products(query: str, limit: int = 20):
    like = f"%{query}%"
    return await _fetchall(
        """SELECT * FROM products
           WHERE status='active' AND (title LIKE ? OR description LIKE ?)
           ORDER BY created_at DESC LIMIT ?""",
        (like, like, limit),
    )


async def toggle_favourite(user_id: int, product_id: int) -> bool:
    exists = await _fetchone(
        "SELECT 1 FROM favourites WHERE user_id=? AND product_id=?", (user_id, product_id)
    )
    if exists:
        await _execute(
            "DELETE FROM favourites WHERE user_id=? AND product_id=?", (user_id, product_id)
        )
        return False
    else:
        await _execute(
            "INSERT INTO favourites (user_id, product_id) VALUES (?, ?)", (user_id, product_id)
        )
        return True


async def list_favourites(user_id: int):
    return await _fetchall(
        """SELECT p.* FROM favourites f
           JOIN products p ON p.product_id = f.product_id
           WHERE f.user_id=? ORDER BY p.created_at DESC""",
        (user_id,),
    )


async def list_favouriters(product_id: int):
    """User ids of everyone who has favourited this product — used to send
    back-in-stock alerts."""
    rows = await _fetchall(
        "SELECT user_id FROM favourites WHERE product_id=?", (product_id,)
    )
    return [r["user_id"] for r in rows]


# ---------- Orders ----------

async def create_order(buyer_id: int, product_id: int, seller_id: int,
                        quantity: int, total_price: float) -> int:
    return await _execute(
        """INSERT INTO orders (buyer_id, product_id, seller_id, quantity, total_price, status)
           VALUES (?, ?, ?, ?, ?, 'awaiting_payment')""",
        (buyer_id, product_id, seller_id, quantity, total_price),
    )


async def attach_order_screenshot(order_id: int, file_id: str):
    await _execute(
        "UPDATE orders SET screenshot_file_id=?, status='awaiting_approval' WHERE order_id=?",
        (file_id, order_id),
    )


async def set_order_payment_method(order_id: int, method_key: str):
    await _execute(
        "UPDATE orders SET payment_method=? WHERE order_id=?", (method_key, order_id)
    )


async def get_order(order_id: int):
    return await _fetchone("SELECT * FROM orders WHERE order_id=?", (order_id,))


async def set_order_status(order_id: int, status: str):
    await _execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))


async def list_stale_awaiting_payment_orders(cutoff: str):
    """Orders still at 'awaiting_payment' (no screenshot uploaded yet)
    created before `cutoff` (a 'YYYY-MM-DD HH:MM:SS' UTC string, matching
    the DB's CURRENT_TIMESTAMP format)."""
    return await _fetchall(
        "SELECT * FROM orders WHERE status='awaiting_payment' AND created_at < ?",
        (cutoff,),
    )


def _time_left_line(order) -> str:
    """Builds a '⏳ Time left to pay: MM:SS' line for the buyer, based on
    the order's creation time and ORDER_EXPIRY_MINUTES. Falls back to the
    full window if the created_at timestamp can't be parsed."""
    seconds_left = ORDER_EXPIRY_MINUTES * 60
    created_at = (order.get("created_at") if order else None) or ""
    created_at = created_at.strip()
    if created_at:
        # Normalize timestamp variations (Turso/libsql may return "T"
        # separators, trailing "Z", or fractional seconds instead of
        # plain SQLite-style "YYYY-MM-DD HH:MM:SS").
        cleaned = created_at.replace("T", " ").rstrip("Z").strip()
        if "." in cleaned:
            cleaned = cleaned.split(".", 1)[0]
        try:
            created_dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
            deadline = created_dt + timedelta(minutes=ORDER_EXPIRY_MINUTES)
            seconds_left = (deadline - datetime.utcnow()).total_seconds()
        except ValueError:
            logging.warning("Couldn't parse order created_at for countdown: %r", created_at)
    seconds_left = max(0, round(seconds_left))
    if seconds_left <= 0:
        return "⏳ This order is about to expire — pay and upload your screenshot right away."
    minutes, seconds = divmod(seconds_left, 60)
    return (
        f"⏳ Time left to pay: {minutes:02d}:{seconds:02d} — "
        f"after that this order expires automatically."
    )


async def decrement_stock(product_id: int, quantity: int):
    await _execute(
        "UPDATE products SET stock_qty = MAX(0, stock_qty - ?) WHERE product_id=?",
        (quantity, product_id),
    )


async def _notify_if_low_stock(bot: Bot, product_id: int):
    """Checks a product's current stock and, if it's out or running low,
    pings the seller and all admins. Called right after any stock change."""
    product = await get_product(product_id)
    if not product:
        return
    stock = product["stock_qty"]
    if stock <= 0:
        text = f"⚠️ OUT OF STOCK: \"{product['title']}\" now has 0 units left."
    elif stock <= LOW_STOCK_THRESHOLD:
        text = f"⚠️ Low stock: \"{product['title']}\" has only {stock} unit(s) left."
    else:
        return

    recipients = set(ADMIN_IDS) | {product["seller_id"]}
    for user_id in recipients:
        try:
            await bot.send_message(user_id, text)
        except Exception:
            pass  # user may have blocked the bot or deleted their account


async def _notify_if_back_in_stock(bot: Bot, product_id: int, previous_stock: int):
    """If a product's stock went from 0 (or below) to a positive number,
    alerts everyone who favourited it. Compares against the stock level
    *before* the update, so it only fires on the 0 -> positive transition,
    not on every stock edit."""
    if previous_stock > 0:
        return
    product = await get_product(product_id)
    if not product or product["stock_qty"] <= 0:
        return
    favouriters = await list_favouriters(product_id)
    text = (
        f"🎉 Good news! \"{product['title']}\" is back in stock "
        f"({product['stock_qty']} available) — grab it before it's gone."
    )
    for user_id in favouriters:
        try:
            await bot.send_message(user_id, text)
        except Exception:
            pass  # user may have blocked the bot or deleted their account


async def list_orders_by_buyer(buyer_id: int):
    return await _fetchall(
        "SELECT * FROM orders WHERE buyer_id=? ORDER BY created_at DESC", (buyer_id,)
    )


async def list_orders_for_seller(seller_id: int, status: str = None):
    if status:
        return await _fetchall(
            "SELECT * FROM orders WHERE seller_id=? AND status=? ORDER BY created_at DESC",
            (seller_id, status),
        )
    return await _fetchall(
        "SELECT * FROM orders WHERE seller_id=? ORDER BY created_at DESC", (seller_id,)
    )


async def list_orders_awaiting_approval():
    return await _fetchall(
        "SELECT * FROM orders WHERE status='awaiting_approval' ORDER BY created_at ASC"
    )


# ---------- Reviews ----------

async def add_review(order_id: int, buyer_id: int, product_id: int, rating: int, comment: str):
    await _execute(
        """INSERT INTO reviews (order_id, buyer_id, product_id, rating, comment)
           VALUES (?, ?, ?, ?, ?)""",
        (order_id, buyer_id, product_id, rating, comment),
    )


async def has_review(order_id: int) -> bool:
    row = await _fetchone("SELECT 1 FROM reviews WHERE order_id=?", (order_id,))
    return row is not None


async def list_reviews_for_product(product_id: int):
    return await _fetchall(
        "SELECT * FROM reviews WHERE product_id=? ORDER BY created_at DESC", (product_id,)
    )

# =========================================================
# KEYBOARDS
# =========================================================

def main_menu(is_seller: bool, is_admin_user: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🔎 Search"), KeyboardButton(text="📂 Products List")],
        [KeyboardButton(text="⭐ Favourites"), KeyboardButton(text="📦 My Orders")],
        [KeyboardButton(text="👤 My Account"), KeyboardButton(text="🆘 Support")],
        [KeyboardButton(text="🔗 Referrals")],
    ]
    if is_seller:
        rows.append([KeyboardButton(text="🛍 My Products"), KeyboardButton(text="➕ Add Product")])
        rows.append([KeyboardButton(text="📥 Orders to Fulfil")])
    if is_admin_user:
        rows.append([KeyboardButton(text="👥 Pending Sellers"), KeyboardButton(text="📂 Add Category")])
        rows.append([KeyboardButton(text="✏️ Edit Category"), KeyboardButton(text="🗑 Remove Category")])
        rows.append([KeyboardButton(text="✏️ Edit Products"), KeyboardButton(text="💳 Add Payment Method")])
        rows.append([KeyboardButton(text="🗂 My Chats"), KeyboardButton(text="📊 Stats")])
        rows.append([KeyboardButton(text="📢 Broadcast")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def categories_kb(categories, page: int = 0, pages: int = 3) -> InlineKeyboardMarkup:
    """Splits the category list into `pages` roughly-equal chunks (default 3)
    and lays each chunk out two-per-row, so the menu doesn't turn into one
    long vertical wall of buttons. `page` is 0-indexed."""
    total = len(categories)
    page_size = max(1, -(-total // pages))  # ceil division
    start = page * page_size
    chunk = categories[start:start + page_size]

    buttons = []
    for i in range(0, len(chunk), 2):
        row = [
            InlineKeyboardButton(text=f"{c['emoji']} {c['name']}", callback_data=f"cat:{c['category_id']}")
            for c in chunk[i:i + 2]
        ]
        buttons.append(row)

    total_pages = -(-total // page_size)
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Back", callback_data=f"catpage:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"catpage:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def product_card_kb(product_id: int, is_fav: bool) -> InlineKeyboardMarkup:
    fav_label = "💔 Remove favourite" if is_fav else "❤️ Add to favourites"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Buy", callback_data=f"buy:{product_id}")],
        [InlineKeyboardButton(text=fav_label, callback_data=f"fav:{product_id}")],
    ])


def order_approval_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"order_ok:{order_id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"order_no:{order_id}"),
        ]
    ])


def review_rating_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐" * n, callback_data=f"rate:{order_id}:{n}") for n in range(1, 4)],
        [InlineKeyboardButton(text="⭐" * n, callback_data=f"rate:{order_id}:{n}") for n in range(4, 6)],
    ])


def payment_method_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=info["label"], callback_data=f"pay_method:{key}")]
        for key, info in PAYMENT_METHODS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def copy_value_kb(copy_value: str, is_wallet: bool) -> InlineKeyboardMarkup | None:
    """A single button that copies just the account number / wallet address
    to the buyer's clipboard — nothing else from the message. Returns None
    if there's no copy value configured for this method yet."""
    if not copy_value:
        return None
    label = "📋 Copy wallet address" if is_wallet else "📋 Copy account number"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, copy_text=CopyTextButton(text=copy_value))]
    ])


def seller_decision_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"seller_ok:{user_id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"seller_no:{user_id}"),
        ]
    ])


# =========================================================
# FSM STATES
# =========================================================

class BecomeSeller(StatesGroup):
    waiting_shop_name = State()


class AddProduct(StatesGroup):
    choosing_category = State()
    waiting_title = State()
    waiting_description = State()
    waiting_price = State()
    waiting_stock = State()
    waiting_photos = State()


class AddCategory(StatesGroup):
    waiting_name = State()


class EditCategory(StatesGroup):
    waiting_value = State()


class EditProduct(StatesGroup):
    waiting_value = State()


class SetPaymentMethod(StatesGroup):
    waiting_details = State()
    waiting_copy_value = State()


class Broadcast(StatesGroup):
    waiting_message = State()
    waiting_confirm = State()


class Search(StatesGroup):
    waiting_query = State()


class RequestProduct(StatesGroup):
    waiting_details = State()


class BuyProduct(StatesGroup):
    waiting_quantity = State()
    waiting_payment_method = State()
    waiting_screenshot = State()


class LeaveReview(StatesGroup):
    waiting_comment = State()


# =========================================================
# ROUTER: START / ACCOUNT
# =========================================================

start_router = Router()


async def _menu_for(user_id: int):
    user = await get_user(user_id)
    return main_menu(is_seller=(user["role"] == "seller"), is_admin_user=is_admin(user_id))


@start_router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    existing_user = await get_user(message.from_user.id)
    is_new_user = existing_user is None
    await upsert_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name or "")

    if is_new_user:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2:
            payload = parts[1].strip()
            if payload.isdigit() and int(payload) != message.from_user.id:
                referrer_id = int(payload)
                referrer = await get_user(referrer_id)
                if referrer:
                    await set_referrer(message.from_user.id, referrer_id)
                    try:
                        await bot.send_message(
                            referrer_id,
                            f"🎉 {message.from_user.full_name} joined using your referral link!",
                        )
                    except Exception:
                        pass

    menu = await _menu_for(message.from_user.id)
    await message.answer(
        "Welcome 👋 to @all toolz market place ⚒️ we sell all toolz, like all "
        "social media account,VPN, working update 💼, hacking toolz📱etc,"
        "explore are market bot to have better understanding🧑‍💻 trusted 💯📌\n\n"
        "Bot powered by_survivor💪",
    )
    await message.answer(
        "Here is the list for what we have not all is here but you can still "
        "request what you want if will forget to add,we willbe glad to assist "
        "you📌\n\n"
        "List \n"
        "( Social media) Facebook, tiktok, Instagram,x,etc...\n\n"
        "( International number for verification) Like WhatsApp, Facebook, "
        "tiktok, and other websites \n\n"
        "(Boosting of social media) All\n\n"
        " (VPN)..................all is available just ask about anyone you need \n\n"
        "(Esim)................ available_etc\n\n"
        "(Update).................. available_etc\n\n"
        "(Format)all................ available_etc\n\n"
        "(Working picture and videos)_etc\n\n"
        "And many more\n\n"
        "Just ask a question ❓ \n\n"
        "We are active waiting for your deal/247\n\n"
        "Bot powered by_Survivor💪",
        reply_markup=menu,
    )


@start_router.message(F.text == "👤 My Account")
async def my_account(message: Message):
    user = await get_user(message.from_user.id)
    lines = [f"👤 Role: {user['role'].capitalize()}"]
    if user["seller_status"] == "pending":
        lines.append("🕓 Seller application: pending approval")
    elif user["seller_status"] == "rejected":
        lines.append("❌ Seller application: rejected")
    if user["shop_name"]:
        lines.append(f"🏪 Shop: {user['shop_name']}")
    await message.answer("\n".join(lines))


@start_router.message(F.text == "🔗 Referrals")
async def my_referrals(message: Message, bot: Bot):
    username = BOT_USERNAME or (await bot.get_me()).username
    link = f"https://t.me/{username}?start={message.from_user.id}"
    count = await count_referrals(message.from_user.id)
    referrals = await list_referrals(message.from_user.id)
    lines = [
        "🔗 Your referral link:",
        link,
        "",
        f"👥 People you've brought in: {count}",
    ]
    if referrals:
        lines.append("")
        lines.append("Recent:")
        for r in referrals[:10]:
            name = r["full_name"] or (f"@{r['username']}" if r["username"] else f"User {r['user_id']}")
            lines.append(f"• {name}")
    await message.answer("\n".join(lines))


@start_router.message(F.text == "🏪 Become a Seller")
async def become_seller_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if user["role"] == "seller":
        await message.answer("You're already an approved seller ✅")
        return
    if user["seller_status"] == "pending":
        await message.answer("Your seller application is already pending review 🕓")
        return
    await message.answer("What's your shop name?")
    await state.set_state(BecomeSeller.waiting_shop_name)


@start_router.message(BecomeSeller.waiting_shop_name)
async def become_seller_finish(message: Message, state: FSMContext, bot: Bot):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    shop_name = message.text.strip()
    await request_seller_status(message.from_user.id, shop_name)
    await state.clear()
    await message.answer("✅ Application submitted! We'll notify you once an admin reviews it.")
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🆕 Seller application\nUser: {message.from_user.full_name} (@{message.from_user.username})\n"
                f"Shop name: {shop_name}",
                reply_markup=seller_decision_kb(message.from_user.id),
            )
        except Exception:
            pass


# =========================================================
# ROUTER: SELLER
# =========================================================

seller_router = Router()


async def _require_seller(message: Message) -> bool:
    user = await get_user(message.from_user.id)
    if user["role"] != "seller":
        await message.answer("Only approved sellers can do this. Tap '🏪 Become a Seller' first.")
        return False
    return True


@seller_router.message(F.text == "➕ Add Product")
async def add_product_start(message: Message, state: FSMContext):
    if not await _require_seller(message):
        return
    categories = await list_categories()
    if not categories:
        await message.answer("No categories exist yet — ask an admin to add one first.")
        return
    await state.set_state(AddProduct.choosing_category)
    await message.answer("Choose a category for your product:", reply_markup=categories_kb(categories))


@seller_router.callback_query(AddProduct.choosing_category, F.data.startswith("cat:"))
async def add_product_category_chosen(callback: CallbackQuery, state: FSMContext):
    category_id = int(callback.data.split(":")[1])
    await state.update_data(category_id=category_id)
    await state.set_state(AddProduct.waiting_title)
    await callback.message.answer("What's the product title?")
    await callback.answer()


@seller_router.message(AddProduct.waiting_title)
async def add_product_title(message: Message, state: FSMContext):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    await state.update_data(title=message.text.strip())
    await state.set_state(AddProduct.waiting_description)
    await message.answer("Give a short description:")


@seller_router.message(AddProduct.waiting_description)
async def add_product_description(message: Message, state: FSMContext):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    await state.update_data(description=message.text.strip())
    await state.set_state(AddProduct.waiting_price)
    await message.answer("Price (numbers only, e.g. 2500):")


@seller_router.message(AddProduct.waiting_price)
async def add_product_price(message: Message, state: FSMContext):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    try:
        price = float(message.text.strip())
    except ValueError:
        await message.answer("Please send a valid number for price.")
        return
    await state.update_data(price=price)
    await state.set_state(AddProduct.waiting_stock)
    await message.answer("Stock quantity available:")


@seller_router.message(AddProduct.waiting_stock)
async def add_product_stock(message: Message, state: FSMContext):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    try:
        stock = int(message.text.strip())
    except ValueError:
        await message.answer("Please send a whole number for stock quantity.")
        return
    data = await state.get_data()
    product_id = await create_product(
        seller_id=message.from_user.id,
        category_id=data["category_id"],
        title=data["title"],
        description=data["description"],
        price=data["price"],
        stock_qty=stock,
    )
    await state.update_data(product_id=product_id, photo_count=0)
    await state.set_state(AddProduct.waiting_photos)
    await message.answer("Now send product photos, one at a time. Send /done when finished (at least 1 photo).")


@seller_router.message(AddProduct.waiting_photos, F.photo)
async def add_product_photo_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    file_id = message.photo[-1].file_id
    await add_product_photo(data["product_id"], file_id)
    count = data.get("photo_count", 0) + 1
    await state.update_data(photo_count=count)
    await message.answer(f"📸 Photo {count} saved. Send another or /done to finish.")


@seller_router.message(AddProduct.waiting_photos, F.text == "/done")
async def add_product_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("photo_count", 0) == 0:
        await message.answer("Please send at least one photo before finishing.")
        return
    await state.clear()
    await message.answer(f"✅ Product '{data['title']}' published!")


def my_product_kb(product_id: int, status: str) -> InlineKeyboardMarkup:
    if status == "removed":
        return InlineKeyboardMarkup(inline_keyboard=[])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Remove", callback_data=f"remove_product:{product_id}")]
    ])


@seller_router.message(F.text == "🛍 My Products")
async def my_products(message: Message):
    if not await _require_seller(message):
        return
    products = await list_products_by_seller(message.from_user.id)
    if not products:
        await message.answer("You haven't listed any products yet.")
        return
    for p in products:
        await message.answer(
            f"🛍 {p['title']}\n💰 ₦{p['price']:.2f}\n📦 Stock: {p['stock_qty']}\n"
            f"Status: {p['status']}\nID: {p['product_id']}",
            reply_markup=my_product_kb(p["product_id"], p["status"]),
        )


@seller_router.callback_query(F.data.startswith("remove_product:"))
async def remove_product_confirm(callback: CallbackQuery):
    product_id = int(callback.data.split(":", 1)[1])
    product = await get_product(product_id)
    if not product or product["seller_id"] != callback.from_user.id:
        await callback.answer("Not found.")
        return
    await callback.answer()
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Yes, remove it", callback_data=f"remove_yes:{product_id}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="remove_no"),
        ]
    ])
    await callback.message.answer(
        f"Remove \"{product['title']}\"? Buyers won't be able to find or buy it "
        f"anymore, but past orders/reviews for it are kept.",
        reply_markup=confirm_kb,
    )


@seller_router.callback_query(F.data.startswith("remove_yes:"))
async def remove_product_do(callback: CallbackQuery):
    product_id = int(callback.data.split(":", 1)[1])
    ok = await remove_product(product_id, callback.from_user.id)
    await callback.answer()
    if ok:
        await callback.message.answer("🗑 Product removed.")
    else:
        await callback.message.answer("Couldn't remove that — it may not be yours.")


@seller_router.callback_query(F.data == "remove_no")
async def remove_product_cancel(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Cancelled — product was not removed.")


# =========================================================
# ROUTER: BUYER (browse / search / favourites)
# =========================================================

buyer_router = Router()


async def _send_product_card(target_message: Message, product, user_id: int):
    photos = await get_product_photos(product["product_id"])
    favs = await list_favourites(user_id)
    is_fav = any(f["product_id"] == product["product_id"] for f in favs)
    caption = (
        f"🛍 <b>{product['title']}</b>\n{product['description']}\n\n"
        f"💰 ₦{product['price']:.2f}\n📦 Stock: {product['stock_qty']}"
    )
    markup = product_card_kb(product["product_id"], is_fav)
    if photos:
        await target_message.answer_photo(photos[0]["file_id"], caption=caption, parse_mode="HTML", reply_markup=markup)
    else:
        await target_message.answer(caption, parse_mode="HTML", reply_markup=markup)


def request_product_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋 Request this product", callback_data="request_product")]
    ])


@buyer_router.message(F.text == "📂 Products List")
async def show_categories(message: Message):
    categories = await list_categories()
    if not categories:
        await message.answer("No categories yet.")
        return
    await message.answer("Products List — select your choice:", reply_markup=categories_kb(categories, page=0))


@buyer_router.callback_query(F.data.startswith("catpage:"))
async def categories_page(callback: CallbackQuery):
    page = int(callback.data.split(":", 1)[1])
    categories = await list_categories()
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=categories_kb(categories, page=page))
    except Exception:
        await callback.message.answer("Products List — select your choice:", reply_markup=categories_kb(categories, page=page))


@buyer_router.callback_query(F.data.startswith("cat:"))
async def browse_category(callback: CallbackQuery):
    category_id = int(callback.data.split(":")[1])
    products = await list_products_by_category(category_id)
    await callback.answer()
    if not products:
        await callback.message.answer(
            "No products in this category yet.",
            reply_markup=request_product_kb(),
        )
        return
    for p in products:
        await _send_product_card(callback.message, p, callback.from_user.id)
    await callback.message.answer(
        "Can't find what you're looking for?",
        reply_markup=request_product_kb(),
    )


@buyer_router.callback_query(F.data == "request_product")
async def request_product_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(RequestProduct.waiting_details)
    await callback.message.answer(
        "🙋 Tell us what you're looking for (item name, and any details like "
        "size/color/budget). We'll pass it on to sellers/admins."
    )


@buyer_router.message(RequestProduct.waiting_details)
async def request_product_finish(message: Message, state: FSMContext, bot: Bot):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    await state.clear()
    requester = message.from_user
    text = (
        f"🙋 <b>Product request</b>\n"
        f"From: {requester.full_name} (@{requester.username or 'no username'}, id {requester.id})\n\n"
        f"\"{message.text.strip()}\""
    )
    reply_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Reply to buyer", callback_data=f"reply_buyer:{requester.id}")]
    ])
    sent = False
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=reply_kb)
            sent = True
        except Exception:
            pass
    if sent:
        await message.answer("✅ Your request has been sent! We'll let you know if it becomes available.")
    else:
        await message.answer("Sorry, we couldn't send your request right now — please try again later.")


@buyer_router.message(F.text == "🆘 Support")
async def support_start(message: Message):
    contact_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Chat with Admin", url="https://t.me/Survivor_1r")]
    ])
    await message.answer("Need help? Tap below to chat with us directly:", reply_markup=contact_kb)


@buyer_router.message(F.text == "🔎 Search")
async def search_start(message: Message, state: FSMContext):
    await state.set_state(Search.waiting_query)
    await message.answer("What are you looking for?")


@buyer_router.message(Search.waiting_query)
async def search_run(message: Message, state: FSMContext):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    await state.clear()
    results = await search_products(message.text.strip())
    if not results:
        await message.answer("No matching products found.", reply_markup=request_product_kb())
        return
    for p in results:
        await _send_product_card(message, p, message.from_user.id)


@buyer_router.message(F.text == "⭐ Favourites")
async def show_favourites(message: Message):
    favs = await list_favourites(message.from_user.id)
    if not favs:
        await message.answer("You have no favourites yet.")
        return
    for p in favs:
        await _send_product_card(message, p, message.from_user.id)


@buyer_router.callback_query(F.data.startswith("fav:"))
async def toggle_fav_handler(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    now_fav = await toggle_favourite(callback.from_user.id, product_id)
    await callback.answer("Added to favourites ❤️" if now_fav else "Removed from favourites 💔")


# =========================================================
# ROUTER: ORDERS (Phase 4)
# =========================================================

orders_router = Router()


@orders_router.callback_query(F.data.startswith("buy:"))
async def buy_start(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    product = await get_product(product_id)
    await callback.answer()
    if not product or product["status"] != "active":
        await callback.message.answer("This product isn't available anymore.")
        return
    if product["stock_qty"] <= 0:
        await callback.message.answer("This product is out of stock.")
        return
    if product["seller_id"] == callback.from_user.id:
        await callback.message.answer("You can't buy your own product.")
        return
    await state.update_data(product_id=product_id)
    await state.set_state(BuyProduct.waiting_quantity)
    await callback.message.answer(
        f"How many units of '{product['title']}' would you like? (in stock: {product['stock_qty']})"
    )


@orders_router.message(BuyProduct.waiting_quantity)
async def buy_quantity(message: Message, state: FSMContext):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    try:
        qty = int(message.text.strip())
        if qty <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Please send a valid positive whole number.")
        return

    data = await state.get_data()
    product = await get_product(data["product_id"])
    if not product or product["stock_qty"] < qty:
        await message.answer("Sorry, that's more than what's in stock. Try a smaller quantity.")
        return

    total = product["price"] * qty
    order_id = await create_order(
        buyer_id=message.from_user.id,
        product_id=product["product_id"],
        seller_id=product["seller_id"],
        quantity=qty,
        total_price=total,
    )
    await state.update_data(order_id=order_id)
    await state.set_state(BuyProduct.waiting_payment_method)
    await message.answer(
        f"🧾 Order #{order_id} created — {qty} × {product['title']} = ₦{total:.2f}\n\n"
        f"Choose how you'd like to pay:",
        reply_markup=payment_method_kb(),
    )


@orders_router.callback_query(BuyProduct.waiting_payment_method, F.data.startswith("pay_method:"))
async def buy_choose_payment_method(callback: CallbackQuery, state: FSMContext, bot: Bot):
    method_key = callback.data.split(":", 1)[1]
    method = PAYMENT_METHODS.get(method_key)
    if not method:
        await callback.answer("Unknown payment method.")
        return

    data = await state.get_data()
    order = await get_order(data["order_id"])
    await set_order_payment_method(data["order_id"], method_key)
    await state.set_state(BuyProduct.waiting_screenshot)
    await callback.answer()
    saved_details = await get_payment_method_details(method_key)
    details = saved_details or method["details"]
    saved_copy_value = await get_payment_method_copy_value(method_key)
    copy_value = saved_copy_value or method.get("copy_value") or ""
    is_wallet = method_key in CRYPTO_COINGECKO_IDS

    if copy_value:
        label = "Wallet Address" if is_wallet else "Account Number"
        details = f"{details}\n{label}: 👇 tap the button below to copy"
    else:
        logging.warning(
            "No copy_value configured for payment method %r — buyer sees no number/address at all. "
            "An admin needs to set it via '💳 Add Payment Method'.", method_key
        )
        details = f"{details}\n⚠️ Account number/wallet address not set up yet — contact support."

    total_ngn = order["total_price"] if order else None
    amount_line = f"💰 Amount to pay: ₦{total_ngn:.2f}" if total_ngn is not None else ""

    coin_id = CRYPTO_COINGECKO_IDS.get(method_key)
    if coin_id and total_ngn is not None:
        rate = await get_ngn_rate(coin_id)
        symbol = CRYPTO_SYMBOLS.get(method_key, "")
        if rate:
            crypto_amount = total_ngn / rate
            decimals = CRYPTO_DECIMALS.get(method_key, 6)
            amount_line = f"💰 Amount to pay: ₦{total_ngn:.2f} ≈ {crypto_amount:.{decimals}f} {symbol}"
        else:
            amount_line = (
                f"💰 Amount to pay: ₦{total_ngn:.2f}\n"
                f"⚠️ Couldn't fetch a live {symbol} rate right now — check the current "
                f"{symbol} price yourself and convert before sending."
            )

    header = f"💳 Pay via {method['label']}:\n\n{amount_line}\n\n{details}"
    sent = await callback.message.answer(
        f"{header}\n\n{_time_left_line(order)}\n\n"
        f"After paying, send a screenshot of the payment here.",
        reply_markup=copy_value_kb(copy_value, is_wallet),
    )
    asyncio.create_task(
        _countdown_message_updater(bot, sent.chat.id, sent.message_id, data["order_id"], header)
    )


async def _countdown_message_updater(bot: Bot, chat_id: int, message_id: int, order_id: int, header: str):
    """Edits the payment-instructions message every few seconds so the
    buyer sees the 'time left to pay' line visibly tick down in MM:SS,
    instead of a number that's frozen at the moment they picked a payment
    method. Stops as soon as the order leaves 'awaiting_payment' (screenshot
    uploaded, or it expired) or once time runs out.

    Ticks every 5 seconds rather than every 1 — editing a Telegram message
    once per second risks hitting Telegram's rate limits, especially with
    several buyers checking out at once. 5s still reads as "live" to a
    buyer watching the screen."""
    while True:
        await asyncio.sleep(5)
        order = await get_order(order_id)
        if not order or order["status"] != "awaiting_payment":
            return  # buyer already paid, or the order expired — nothing more to update
        time_line = _time_left_line(order)
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"{header}\n\n{time_line}\n\nAfter paying, send a screenshot of the payment here.",
            )
        except Exception:
            pass  # message may have been deleted, or text is unchanged — either way, keep going
        if time_line.startswith("⏳ This order is about to expire"):
            return


@orders_router.message(BuyProduct.waiting_screenshot, F.photo)
async def buy_screenshot(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    order_id = data["order_id"]
    order = await get_order(order_id)
    if not order or order["status"] != "awaiting_payment":
        await state.clear()
        await message.answer(
            "This order has expired or was already handled — please place a new order if you still want it."
        )
        return
    file_id = message.photo[-1].file_id
    await attach_order_screenshot(order_id, file_id)
    order = await get_order(order_id)
    product = await get_product(order["product_id"])
    await state.clear()
    await message.answer(
        f"✅ Payment screenshot received for order #{order_id}. "
        f"Waiting for seller/admin approval — you'll be notified."
    )

    notify_text = (
        f"💰 New payment to review — Order #{order_id}\n"
        f"Product: {product['title']}\n"
        f"Qty: {order['quantity']} — Total: ₦{order['total_price']:.2f}\n"
        f"Paid via: {PAYMENT_METHODS.get(order['payment_method'], {}).get('label', order['payment_method'])}\n"
        f"Buyer: {message.from_user.full_name} (@{message.from_user.username})"
    )
    recipients = {order["seller_id"], *ADMIN_IDS}
    for uid in recipients:
        try:
            await bot.send_photo(
                uid, file_id, caption=notify_text, reply_markup=order_approval_kb(order_id)
            )
        except Exception:
            pass


@orders_router.message(BuyProduct.waiting_screenshot)
async def buy_screenshot_wrong_type(message: Message):
    await message.answer("Please send a photo of your payment receipt/screenshot.")


@orders_router.message(F.text == "📦 My Orders")
async def my_orders(message: Message):
    orders = await list_orders_by_buyer(message.from_user.id)
    if not orders:
        await message.answer("You haven't placed any orders yet.")
        return
    for o in orders:
        product = await get_product(o["product_id"])
        title = product["title"] if product else "(deleted product)"
        text = (
            f"🧾 Order #{o['order_id']} — {title}\n"
            f"Qty: {o['quantity']} — Total: ₦{o['total_price']:.2f}\n"
            f"Status: {o['status'].replace('_', ' ').title()}"
        )
        if o["status"] == "awaiting_payment":
            text += f"\n{_time_left_line(o)}"
        buttons = [[InlineKeyboardButton(text="🧾 Full Receipt", callback_data=f"receipt:{o['order_id']}")]]
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        if o["status"] == "completed" and not await has_review(o["order_id"]):
            await message.answer("How would you rate this purchase?", reply_markup=review_rating_kb(o["order_id"]))


@orders_router.callback_query(F.data.startswith("receipt:"))
async def order_receipt(callback: CallbackQuery):
    order_id = int(callback.data.split(":", 1)[1])
    order = await get_order(order_id)
    await callback.answer()
    if not order or order["buyer_id"] != callback.from_user.id:
        await callback.message.answer("Receipt not found.")
        return
    product = await get_product(order["product_id"])
    title = product["title"] if product else "(deleted product)"
    seller = await get_user(order["seller_id"])
    seller_name = seller["full_name"] if seller else "Unknown seller"
    seller_username = f"@{seller['username']}" if seller and seller.get("username") else "no username"
    unit_price = order["total_price"] / order["quantity"] if order["quantity"] else order["total_price"]
    method_key = order.get("payment_method")
    method_label = PAYMENT_METHODS.get(method_key, {}).get("label", method_key or "Not yet chosen")
    created = order.get("created_at") or "—"

    receipt = (
        f"🧾 RECEIPT — Order #{order['order_id']}\n"
        f"────────────────────\n"
        f"Date: {created}\n"
        f"Item: {title}\n"
        f"Unit price: ₦{unit_price:.2f}\n"
        f"Quantity: {order['quantity']}\n"
        f"Total: ₦{order['total_price']:.2f}\n"
        f"Payment method: {method_label}\n"
        f"Sold by: {seller_name} ({seller_username})\n"
        f"Status: {order['status'].replace('_', ' ').title()}\n"
        f"────────────────────\n"
        f"Keep this receipt for your records."
    )
    await callback.message.answer(receipt)


@orders_router.message(F.text == "📥 Orders to Fulfil")
async def orders_to_fulfil(message: Message):
    user = await get_user(message.from_user.id)
    if user["role"] != "seller":
        await message.answer("Only approved sellers can view this.")
        return
    orders = await list_orders_for_seller(message.from_user.id, status="awaiting_approval")
    if not orders:
        await message.answer("No orders awaiting your approval right now.")
        return
    for o in orders:
        product = await get_product(o["product_id"])
        text = (
            f"🧾 Order #{o['order_id']} — {product['title'] if product else '(deleted)'}\n"
            f"Qty: {o['quantity']} — Total: ₦{o['total_price']:.2f}"
        )
        if o["screenshot_file_id"]:
            await message.answer_photo(
                o["screenshot_file_id"], caption=text, reply_markup=order_approval_kb(o["order_id"])
            )
        else:
            await message.answer(text, reply_markup=order_approval_kb(o["order_id"]))


async def _can_manage_order(user_id: int, order) -> bool:
    return is_admin(user_id) or user_id == order["seller_id"]


@orders_router.callback_query(F.data.startswith("order_ok:"))
async def approve_order(callback: CallbackQuery, bot: Bot):
    order_id = int(callback.data.split(":")[1])
    order = await get_order(order_id)
    if not order:
        await callback.answer("Order not found.")
        return
    if not await _can_manage_order(callback.from_user.id, order):
        await callback.answer("Not authorized.")
        return
    if order["status"] != "awaiting_approval":
        await callback.answer("Already handled.")
        return

    await decrement_stock(order["product_id"], order["quantity"])
    await _notify_if_low_stock(bot, order["product_id"])
    await set_order_status(order_id, "completed")
    await callback.answer("Approved ✅")
    deliver_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Deliver to buyer", callback_data=f"reply_buyer:{order['buyer_id']}")]
    ])
    await callback.message.answer(
        f"Order #{order_id} approved and marked completed.", reply_markup=deliver_kb
    )

    product = await get_product(order["product_id"])
    try:
        await bot.send_message(
            order["buyer_id"],
            f"🎉 Your payment for order #{order_id} ({product['title'] if product else ''}) "
            f"was approved, hold for your details just a minute\n"
            f"How would you rate this purchase?",
            reply_markup=review_rating_kb(order_id),
        )
    except Exception:
        pass


@orders_router.callback_query(F.data.startswith("order_no:"))
async def reject_order(callback: CallbackQuery, bot: Bot):
    order_id = int(callback.data.split(":")[1])
    order = await get_order(order_id)
    if not order:
        await callback.answer("Order not found.")
        return
    if not await _can_manage_order(callback.from_user.id, order):
        await callback.answer("Not authorized.")
        return
    if order["status"] != "awaiting_approval":
        await callback.answer("Already handled.")
        return

    await set_order_status(order_id, "rejected")
    await callback.answer("Rejected ❌")
    await callback.message.answer(f"Order #{order_id} rejected.")
    try:
        await bot.send_message(
            order["buyer_id"],
            f"❌ Your payment for order #{order_id} could not be verified. "
            f"Please contact support or try again."
        )
    except Exception:
        pass


@orders_router.callback_query(F.data.startswith("rate:"))
async def rate_order(callback: CallbackQuery, state: FSMContext):
    _, order_id_str, rating_str = callback.data.split(":")
    order_id, rating = int(order_id_str), int(rating_str)
    order = await get_order(order_id)
    await callback.answer()
    if not order or order["buyer_id"] != callback.from_user.id:
        return
    if await has_review(order_id):
        await callback.message.answer("You've already reviewed this order.")
        return
    await state.update_data(order_id=order_id, product_id=order["product_id"], rating=rating)
    await state.set_state(LeaveReview.waiting_comment)
    await callback.message.answer("Thanks! Add a short comment for your review (or send '-' to skip):")


@orders_router.message(LeaveReview.waiting_comment)
async def rate_order_comment(message: Message, state: FSMContext):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    data = await state.get_data()
    comment = "" if message.text.strip() == "-" else message.text.strip()
    await add_review(data["order_id"], message.from_user.id, data["product_id"], data["rating"], comment)
    await state.clear()
    await message.answer(f"✅ Review submitted: {'⭐' * data['rating']}")


# =========================================================
# ROUTER: ADMIN
# =========================================================

admin_router = Router()
relay_router = Router()


@admin_router.message(F.text == "👥 Pending Sellers")
async def pending_sellers(message: Message):
    if not is_admin(message.from_user.id):
        return
    pending = await get_pending_sellers()
    if not pending:
        await message.answer("No pending seller applications.")
        return
    for u in pending:
        await message.answer(
            f"👤 {u['full_name']} (@{u['username']})\n🏪 Shop: {u['shop_name']}",
            reply_markup=seller_decision_kb(u["user_id"]),
        )


@admin_router.callback_query(F.data.startswith("seller_ok:"))
async def approve_seller(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    user_id = int(callback.data.split(":")[1])
    await set_seller_decision(user_id, approved=True)
    await callback.answer("Approved ✅")
    await callback.message.answer(f"Seller {user_id} approved.")
    try:
        new_menu = await _menu_for(user_id)
        await bot.send_message(
            user_id,
            "🎉 Your seller application was approved! You can now add products.",
            reply_markup=new_menu,
        )
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("seller_no:"))
async def reject_seller(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    user_id = int(callback.data.split(":")[1])
    await set_seller_decision(user_id, approved=False)
    await callback.answer("Rejected ❌")
    await callback.message.answer(f"Seller {user_id} rejected.")
    try:
        await bot.send_message(user_id, "Your seller application was not approved this time.")
    except Exception:
        pass


@admin_router.message(F.text == "📂 Add Category")
async def add_category_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AddCategory.waiting_name)
    await message.answer("Send the category name (optionally start with an emoji, e.g. '📱 Phones'):")


@admin_router.message(AddCategory.waiting_name)
async def add_category_finish(message: Message, state: FSMContext):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    text = message.text.strip()
    parts = text.split(" ", 1)
    if len(parts) == 2 and len(parts[0]) <= 2:
        emoji, name = parts[0], parts[1]
    else:
        emoji, name = "📦", text
    await add_category(name, emoji)
    await state.clear()
    await message.answer(f"✅ Category added: {emoji} {name}")


@admin_router.message(F.text == "🗑 Remove Category")
async def remove_category_start(message: Message):
    if not is_admin(message.from_user.id):
        return
    categories = await list_categories()
    if not categories:
        await message.answer("No categories to remove.")
        return
    rows = [
        [InlineKeyboardButton(text=f"{c['emoji']} {c['name']}", callback_data=f"rmcat_pick:{c['category_id']}")]
        for c in categories
    ]
    await message.answer("Pick a category to remove:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data.startswith("rmcat_pick:"))
async def remove_category_pick(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    category_id = int(callback.data.split(":", 1)[1])
    await callback.answer()
    product_count = await count_active_products_in_category(category_id)
    warning = (
        f"⚠️ This category still has {product_count} active product(s) in it. "
        f"Removing it won't delete those products, but buyers won't be able to "
        f"browse to them by category anymore.\n\n"
        if product_count
        else ""
    )
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Yes, remove it", callback_data=f"rmcat_yes:{category_id}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="rmcat_no"),
        ]
    ])
    await callback.message.answer(f"{warning}Remove this category?", reply_markup=confirm_kb)


@admin_router.callback_query(F.data.startswith("rmcat_yes:"))
async def remove_category_do(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    category_id = int(callback.data.split(":", 1)[1])
    await callback.answer()
    try:
        await remove_category(category_id)
        await callback.message.answer("🗑 Category removed.")
    except Exception:
        logging.exception("Failed to remove category %s", category_id)
        await callback.message.answer("⚠️ Something went wrong removing that category — please try again.")


@admin_router.callback_query(F.data == "rmcat_no")
async def remove_category_cancel(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Cancelled — category was not removed.")


@admin_router.message(F.text == "✏️ Edit Category")
async def edit_category_start(message: Message):
    if not is_admin(message.from_user.id):
        return
    categories = await list_categories()
    if not categories:
        await message.answer("No categories to edit.")
        return
    rows = [
        [InlineKeyboardButton(text=f"{c['emoji']} {c['name']}", callback_data=f"editcat_pick:{c['category_id']}")]
        for c in categories
    ]
    await message.answer("Pick a category to edit:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data.startswith("editcat_pick:"))
async def edit_category_pick(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    category_id = int(callback.data.split(":", 1)[1])
    await state.set_state(EditCategory.waiting_value)
    await state.update_data(category_id=category_id)
    await callback.answer()
    await callback.message.answer(
        "Send the new name (optionally start with an emoji, e.g. '📱 Phones'). "
        "This replaces the current name and emoji."
    )


@admin_router.message(EditCategory.waiting_value)
async def edit_category_finish(message: Message, state: FSMContext):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    data = await state.get_data()
    category_id = data.get("category_id")
    text = message.text.strip()
    parts = text.split(" ", 1)
    if len(parts) == 2 and len(parts[0]) <= 2:
        emoji, name = parts[0], parts[1]
    else:
        emoji, name = "📦", text
    await update_category(category_id, name, emoji)
    await state.clear()
    await message.answer(f"✅ Category updated: {emoji} {name}")


def payment_methods_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=info["label"], callback_data=f"admin_paymeth:{key}")]
        for key, info in PAYMENT_METHODS.items()
    ])


@admin_router.message(F.text == "💳 Add Payment Method")
async def payment_methods_start(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "Select the payment method you want to add/update:",
        reply_markup=payment_methods_admin_kb(),
    )


@admin_router.callback_query(F.data.startswith("admin_paymeth:"))
async def payment_method_chosen(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    method_key = callback.data.split(":", 1)[1]
    method = PAYMENT_METHODS.get(method_key)
    if not method:
        await callback.answer("Unknown method.")
        return
    await callback.answer()
    await state.set_state(SetPaymentMethod.waiting_details)
    await state.update_data(method_key=method_key)
    await callback.message.answer(
        f"Send your account details for {method['label']} as ONE message — e.g. bank name "
        f"and account name (for Nigerian methods) or the coin name (for crypto).\n\n"
        f"⚠️ Do NOT include the account number or wallet address here — that goes in the "
        f"next step as its own copy button, not as regular text buyers could otherwise "
        f"copy from the message."
    )


@admin_router.message(SetPaymentMethod.waiting_details)
async def payment_method_details_received(message: Message, state: FSMContext):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    data = await state.get_data()
    method_key = data.get("method_key")
    method = PAYMENT_METHODS.get(method_key)
    if not method:
        await state.clear()
        await message.answer("Something went wrong — please tap \"💳 Add Payment Method\" again.")
        return
    await set_payment_method_details(method_key, message.text.strip())
    is_wallet = method_key in CRYPTO_COINGECKO_IDS
    await state.set_state(SetPaymentMethod.waiting_copy_value)
    await message.answer(
        f"✅ Details saved. Now send JUST the {'wallet address' if is_wallet else 'account number'} "
        f"on its own — this is the exact text buyers will copy with one tap, so don't include "
        f"anything else (no bank name, no label). Send /skip to leave it without a copy button."
    )


@admin_router.message(SetPaymentMethod.waiting_copy_value)
async def payment_method_copy_value_received(message: Message, state: FSMContext):
    data = await state.get_data()
    method_key = data.get("method_key")
    method = PAYMENT_METHODS.get(method_key)
    await state.clear()
    if not method:
        await message.answer("Something went wrong — please tap \"💳 Add Payment Method\" again.")
        return
    if message.text.strip().lower() == "/skip":
        await message.answer(f"Okay — {method['label']} won't have a copy button for now.")
        return
    await set_payment_method_copy_value(method_key, message.text.strip())
    await message.answer(f"✅ {method['label']} copy button is now set to: {message.text.strip()}")



def edit_product_field_kb(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Title", callback_data=f"editprodfield:{product_id}:title"),
            InlineKeyboardButton(text="📄 Description", callback_data=f"editprodfield:{product_id}:description"),
        ],
        [
            InlineKeyboardButton(text="💰 Price", callback_data=f"editprodfield:{product_id}:price"),
            InlineKeyboardButton(text="📦 Stock", callback_data=f"editprodfield:{product_id}:stock_qty"),
        ],
    ])


@admin_router.message(F.text == "✏️ Edit Products")
async def edit_products_start(message: Message):
    if not is_admin(message.from_user.id):
        return
    categories = await list_categories()
    if not categories:
        await message.answer("No categories exist yet.")
        return
    rows = [
        [InlineKeyboardButton(text=f"{c['emoji']} {c['name']}", callback_data=f"editprodcat:{c['category_id']}")]
        for c in categories
    ]
    await message.answer(
        "Pick a category to see its products:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@admin_router.callback_query(F.data.startswith("editprodcat:"))
async def edit_products_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    category_id = int(callback.data.split(":", 1)[1])
    products = await list_products_by_category_all(category_id)
    await callback.answer()
    if not products:
        await callback.message.answer("No products in this category.")
        return
    for p in products:
        await callback.message.answer(
            f"🛍 {p['title']}\n💰 ₦{p['price']:.2f}\n📦 Stock: {p['stock_qty']}\n"
            f"Status: {p['status']}\nID: {p['product_id']}",
            reply_markup=edit_product_field_kb(p["product_id"]),
        )


@admin_router.callback_query(F.data.startswith("editprodfield:"))
async def edit_product_field_chosen(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, product_id_str, field = callback.data.split(":")
    product = await get_product(int(product_id_str))
    if not product:
        await callback.answer("Product not found.")
        return
    await state.set_state(EditProduct.waiting_value)
    await state.update_data(product_id=int(product_id_str), field=field)
    await callback.answer()
    prompts = {
        "title": "Send the new title:",
        "description": "Send the new description:",
        "price": "Send the new price (numbers only, e.g. 2500):",
        "stock_qty": "Send the new stock quantity (whole number):",
    }
    await callback.message.answer(prompts[field])


@admin_router.message(EditProduct.waiting_value)
async def edit_product_value_received(message: Message, state: FSMContext, bot: Bot):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    data = await state.get_data()
    product_id = data.get("product_id")
    field = data.get("field")
    raw = message.text.strip()
    previous_stock = None
    if field == "price":
        try:
            value = float(raw)
        except ValueError:
            await message.answer("Please send a valid number for price.")
            return
    elif field == "stock_qty":
        try:
            value = int(raw)
        except ValueError:
            await message.answer("Please send a whole number for stock quantity.")
            return
        existing = await get_product(product_id)
        previous_stock = existing["stock_qty"] if existing else None
    else:
        value = raw
    await update_product_field(product_id, field, value)
    await state.clear()
    labels = {"title": "Title", "description": "Description", "price": "Price", "stock_qty": "Stock"}
    await message.answer(f"✅ {labels[field]} updated.")
    if field == "stock_qty":
        await _notify_if_low_stock(bot, product_id)
        if previous_stock is not None:
            await _notify_if_back_in_stock(bot, product_id, previous_stock)


@admin_router.callback_query(F.data.startswith("reply_buyer:"))
async def reply_buyer_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    buyer_id = int(callback.data.split(":", 1)[1])
    await start_chat(buyer_id, callback.from_user.id)
    await callback.answer()
    other_chats = [c for c in await list_admin_chats(callback.from_user.id) if c["buyer_id"] != buyer_id]
    note = f"\n\n(You have {len(other_chats)} other open chat(s) — tap \"🗂 My Chats\" to switch.)" if other_chats else ""
    end_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔚 End chat", callback_data=f"end_chat:{buyer_id}")]
    ])
    await callback.message.answer(
        "💬 Chat started with this buyer — now your focused chat. Just type "
        "normally (text, photos, or files all work) — I'll forward everything "
        "both ways until you tap \"End chat\"." + note,
        reply_markup=end_kb,
    )


@admin_router.callback_query(F.data.startswith("end_chat:"))
async def end_chat_handler(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    buyer_id = int(callback.data.split(":", 1)[1])
    await end_chat(buyer_id)
    await callback.answer("Chat ended")
    new_focus = await get_focus(callback.from_user.id)
    note = f"\n\n🔀 Switched focus to another open chat." if new_focus else ""
    await callback.message.answer("🔚 Chat ended." + note)
    try:
        await bot.send_message(buyer_id, "🔚 This chat has been closed by the seller/admin.")
    except Exception:
        pass


@admin_router.callback_query(F.data.startswith("focus_chat:"))
async def focus_chat_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    buyer_id = int(callback.data.split(":", 1)[1])
    await set_focus(callback.from_user.id, buyer_id)
    await callback.answer("Switched")
    await callback.message.answer(f"🔀 Now focused on chat with user {buyer_id}. Type normally to reply.")


@admin_router.message(F.text == "🗂 My Chats")
async def my_chats_handler(message: Message):
    if not is_admin(message.from_user.id):
        return
    chats = await list_admin_chats(message.from_user.id)
    if not chats:
        await message.answer("You have no open chats right now.")
        return
    focus = await get_focus(message.from_user.id)
    rows = []
    for c in chats:
        buyer = await get_user(c["buyer_id"])
        name = buyer["full_name"] if buyer else str(c["buyer_id"])
        label = f"{'🟢 ' if c['buyer_id'] == focus else ''}{name}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"focus_chat:{c['buyer_id']}")])
    await message.answer(
        "🗂 Your open chats (🟢 = currently focused):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@admin_router.message(F.text == "📊 Stats")
async def stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    categories = await list_categories()
    await message.answer(f"📊 Categories: {len(categories)}\n(More stats land in Phase 5.)")


@admin_router.message(F.text == "📢 Broadcast")
async def broadcast_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(Broadcast.waiting_message)
    await message.answer(
        "Send the message you want to broadcast to everyone who has used the bot "
        "(text, photo, whatever — it'll be sent as-is).\n\n"
        "Send /cancel-style text to back out of this."
    )


@admin_router.message(Broadcast.waiting_message)
async def broadcast_compose(message: Message, state: FSMContext):
    if message.text and _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    users = await list_all_users()
    await state.update_data(from_chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(Broadcast.waiting_confirm)
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Send it", callback_data="broadcast_yes"),
        InlineKeyboardButton(text="❌ Cancel", callback_data="broadcast_no"),
    ]])
    await message.answer(
        f"This will be sent to {len(users)} user(s) who've used the bot. Send it?",
        reply_markup=confirm_kb,
    )


@admin_router.callback_query(Broadcast.waiting_confirm, F.data == "broadcast_yes")
async def broadcast_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    await state.clear()
    await callback.answer()
    users = await list_all_users()
    sent, failed = 0, 0
    for u in users:
        try:
            await bot.copy_message(
                chat_id=u["user_id"],
                from_chat_id=data["from_chat_id"],
                message_id=data["message_id"],
            )
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # gentle pacing to avoid Telegram rate limits
    await callback.message.answer(f"📢 Broadcast done — sent to {sent}, failed for {failed} (blocked bot/deleted account).")


@admin_router.callback_query(Broadcast.waiting_confirm, F.data == "broadcast_no")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.answer("Cancelled — nothing was sent.")


# =========================================================
# ENTRYPOINT
# =========================================================

class _HealthCheckHandler(BaseHTTPRequestHandler):
    """Bare-minimum HTTP handler so Render's free Web Service sees an open
    port. It does nothing else — the bot itself talks to Telegram, not HTTP."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running.")

    def log_message(self, format, *args):
        pass  # silence per-request logging, the bot's own logs are enough


def _start_health_check_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Health-check server listening on port {port} (for Render's free tier).")


async def _expire_stale_orders_loop(bot: Bot):
    """Runs forever in the background. Every few minutes, finds orders that
    have sat at 'awaiting_payment' (buyer never uploaded a payment
    screenshot) for longer than ORDER_EXPIRY_MINUTES, marks them 'expired',
    and lets the buyer and seller know."""
    CHECK_INTERVAL_SECONDS = 120  # check every 2 minutes, since the expiry window is short
    while True:
        try:
            cutoff_dt = datetime.utcnow() - timedelta(minutes=ORDER_EXPIRY_MINUTES)
            cutoff = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
            stale_orders = await list_stale_awaiting_payment_orders(cutoff)
            for order in stale_orders:
                await set_order_status(order["order_id"], "expired")
                product = await get_product(order["product_id"])
                title = product["title"] if product else "(deleted product)"
                try:
                    await bot.send_message(
                        order["buyer_id"],
                        f"⌛ Order #{order['order_id']} ({title}) expired — no payment "
                        f"screenshot was received within {ORDER_EXPIRY_MINUTES:.0f} minutes. "
                        f"Feel free to place a new order if you still want it.",
                    )
                except Exception:
                    pass
                if product:
                    try:
                        await bot.send_message(
                            product["seller_id"],
                            f"⌛ Order #{order['order_id']} for \"{title}\" expired — the buyer "
                            f"never uploaded a payment screenshot.",
                        )
                    except Exception:
                        pass
        except Exception:
            logging.exception("Error while expiring stale orders")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


@relay_router.message()
async def relay_active_chat(message: Message, bot: Bot):
    """Catch-all — only acts when the sender is currently in an active chat
    (started via a 'Reply to buyer' / 'Deliver to buyer' button). Forwards
    the message verbatim (text, photo, document, anything) to the other
    side. Registered last, so it never intercepts normal menu/command
    handling — it only fires when nothing more specific already matched."""
    user_id = message.from_user.id
    if is_admin(user_id):
        buyer_id = await get_focus(user_id)
        if buyer_id:
            try:
                await bot.copy_message(
                    chat_id=buyer_id, from_chat_id=message.chat.id, message_id=message.message_id
                )
            except Exception:
                await message.answer("❌ Couldn't deliver — the buyer may have blocked the bot.")
            return
    admin_id = await get_chat_admin(user_id)
    if admin_id:
        focus = await get_focus(admin_id)
        try:
            if focus != user_id:
                # This admin has other chats open too — label who this is
                # from and offer a one-tap switch, so nothing gets lost or
                # confused between concurrent conversations.
                switch_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔀 Switch to this chat", callback_data=f"focus_chat:{user_id}")]
                ])
                await bot.send_message(
                    admin_id,
                    f"📩 New message from {message.from_user.full_name} "
                    f"(@{message.from_user.username or 'no username'}):",
                    reply_markup=switch_kb,
                )
            await bot.copy_message(
                chat_id=admin_id, from_chat_id=message.chat.id, message_id=message.message_id
            )
        except Exception:
            pass


async def main():
    _start_health_check_server()
    if TURSO_DATABASE_URL:
        print("✅ Using Turso (persistent, free) for storage.")
    else:
        print(
            "⚠️  WARNING: TURSO_DATABASE_URL is not set — falling back to a "
            "local file. On Render's free tier, this gets WIPED every time "
            "the service restarts or sleeps, so seller approvals, products, "
            "and orders will periodically be lost. Set TURSO_DATABASE_URL "
            "and TURSO_AUTH_TOKEN in your Environment tab to fix this for free."
        )
    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())

    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = me.username

    dp.include_router(start_router)
    dp.include_router(seller_router)
    dp.include_router(admin_router)
    dp.include_router(buyer_router)
    dp.include_router(orders_router)
    dp.include_router(relay_router)

    print("Bot is starting...")
    asyncio.create_task(_expire_stale_orders_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

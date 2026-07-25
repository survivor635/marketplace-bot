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
from http.server import BaseHTTPRequestHandler, HTTPServer

import libsql
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
)

logging.basicConfig(level=logging.INFO)
load_dotenv()

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

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
        "details": os.getenv(
            "OPAY_DETAILS",
            "Opay\nAccount Name: <your name>\nAccount Number: <your number>",
        ),
    },
    "palmpay": {
        "label": "🇳🇬 PalmPay",
        "details": os.getenv(
            "PALMPAY_DETAILS",
            "PalmPay\nAccount Name: <your name>\nAccount Number: <your number>",
        ),
    },
    "kuda": {
        "label": "🇳🇬 Kuda",
        "details": os.getenv(
            "KUDA_DETAILS",
            "Kuda Bank\nAccount Name: <your name>\nAccount Number: <your number>",
        ),
    },
    "moniepoint": {
        "label": "🇳🇬 Moniepoint",
        "details": os.getenv(
            "MONIEPOINT_DETAILS",
            "Moniepoint\nAccount Name: <your name>\nAccount Number: <your number>",
        ),
    },
    "bank_transfer": {
        "label": "🏦 Bank Transfer",
        "details": os.getenv(
            "BANK_TRANSFER_DETAILS",
            "Bank: <your bank>\nAccount Name: <your name>\nAccount Number: <your number>",
        ),
    },
    "usdt_trc20": {
        "label": "🪙 USDT (TRC20)",
        "details": os.getenv(
            "USDT_TRC20_DETAILS",
            "USDT (TRC20)\nWallet Address: <your TRC20 wallet address>",
        ),
    },
    "bitcoin": {
        "label": "🪙 Bitcoin",
        "details": os.getenv(
            "BITCOIN_DETAILS",
            "Bitcoin (BTC)\nWallet Address: <your BTC wallet address>",
        ),
    },
    "ethereum": {
        "label": "🪙 Ethereum",
        "details": os.getenv(
            "ETHEREUM_DETAILS",
            "Ethereum (ETH)\nWallet Address: <your ETH wallet address>",
        ),
    },
    "bnb": {
        "label": "🪙 BNB",
        "details": os.getenv(
            "BNB_DETAILS",
            "BNB (BEP20)\nWallet Address: <your BNB wallet address>",
        ),
    },
}


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
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payment_method_settings (
    method_key   TEXT PRIMARY KEY,
    details      TEXT NOT NULL
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
    -- status: awaiting_payment | awaiting_approval | approved | rejected | completed
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


async def set_payment_method_details(method_key: str, details: str):
    await _execute(
        """INSERT INTO payment_method_settings (method_key, details) VALUES (?, ?)
           ON CONFLICT(method_key) DO UPDATE SET details=excluded.details""",
        (method_key, details),
    )


async def get_payment_method_details(method_key: str):
    row = await _fetchone(
        "SELECT details FROM payment_method_settings WHERE method_key=?", (method_key,)
    )
    return row["details"] if row else None


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


async def list_products_by_seller(seller_id: int):
    return await _fetchall(
        "SELECT * FROM products WHERE seller_id=? ORDER BY created_at DESC", (seller_id,)
    )


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


async def decrement_stock(product_id: int, quantity: int):
    await _execute(
        "UPDATE products SET stock_qty = MAX(0, stock_qty - ?) WHERE product_id=?",
        (quantity, product_id),
    )


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
        [KeyboardButton(text="👤 My Account")],
    ]
    if is_seller:
        rows.append([KeyboardButton(text="🛍 My Products"), KeyboardButton(text="➕ Add Product")])
        rows.append([KeyboardButton(text="📥 Orders to Fulfil")])
    else:
        rows.append([KeyboardButton(text="🏪 Become a Seller")])
    if is_admin_user:
        rows.append([KeyboardButton(text="🛠 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def categories_kb(categories) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=f"{c['emoji']} {c['name']}", callback_data=f"cat:{c['category_id']}")]
        for c in categories
    ]
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


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Pending Sellers", callback_data="admin:pending_sellers")],
        [InlineKeyboardButton(text="📂 Add Category", callback_data="admin:add_category")],
        [InlineKeyboardButton(text="💳 Add Payment Method", callback_data="admin:payment_methods")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="admin:stats")],
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


class SetPaymentMethod(StatesGroup):
    waiting_details = State()


class AdminReply(StatesGroup):
    waiting_message = State()


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
async def cmd_start(message: Message):
    await upsert_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name or "")
    menu = await _menu_for(message.from_user.id)
    await message.answer(
        "👋 Welcome to the Marketplace!\n\nBrowse products, save favourites, or sell your own items.",
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
            f"Status: {p['status']}\nID: {p['product_id']}"
        )


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
    await message.answer("Products List — select your choice:", reply_markup=categories_kb(categories))


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
async def buy_choose_payment_method(callback: CallbackQuery, state: FSMContext):
    method_key = callback.data.split(":", 1)[1]
    method = PAYMENT_METHODS.get(method_key)
    if not method:
        await callback.answer("Unknown payment method.")
        return

    data = await state.get_data()
    await set_order_payment_method(data["order_id"], method_key)
    await state.set_state(BuyProduct.waiting_screenshot)
    await callback.answer()
    saved_details = await get_payment_method_details(method_key)
    details = saved_details or method["details"]
    await callback.message.answer(
        f"💳 Pay via {method['label']}:\n\n{details}\n\n"
        f"After paying, send a screenshot of the payment here."
    )


@orders_router.message(BuyProduct.waiting_screenshot, F.photo)
async def buy_screenshot(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    order_id = data["order_id"]
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
        if o["status"] == "completed" and not await has_review(o["order_id"]):
            await message.answer(text, reply_markup=review_rating_kb(o["order_id"]))
        else:
            await message.answer(text)


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
    await set_order_status(order_id, "completed")
    await callback.answer("Approved ✅")
    await callback.message.answer(f"Order #{order_id} approved and marked completed.")

    product = await get_product(order["product_id"])
    try:
        await bot.send_message(
            order["buyer_id"],
            f"🎉 Your payment for order #{order_id} ({product['title'] if product else ''}) was approved!\n"
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


@admin_router.message(F.text == "🛠 Admin Panel")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Admin Panel:", reply_markup=admin_panel_kb())


@admin_router.callback_query(F.data == "admin:pending_sellers")
async def pending_sellers(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    pending = await get_pending_sellers()
    await callback.answer()
    if not pending:
        await callback.message.answer("No pending seller applications.")
        return
    for u in pending:
        await callback.message.answer(
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


@admin_router.callback_query(F.data == "admin:add_category")
async def add_category_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    await state.set_state(AddCategory.waiting_name)
    await callback.message.answer("Send the category name (optionally start with an emoji, e.g. '📱 Phones'):")


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


def payment_methods_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=info["label"], callback_data=f"admin_paymeth:{key}")]
        for key, info in PAYMENT_METHODS.items()
    ])


@admin_router.callback_query(F.data == "admin:payment_methods")
async def payment_methods_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    await callback.message.answer(
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
        f"Send your account details for {method['label']} as ONE message — this is "
        f"exactly what buyers will see (e.g. account name and account number, or "
        f"wallet address). Use line breaks for multiple lines if you like."
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
        await message.answer("Something went wrong — please start again from Admin Panel.")
        return
    await set_payment_method_details(method_key, message.text.strip())
    await state.clear()
    await message.answer(
        f"✅ {method['label']} details saved. Buyers choosing this method will now see:\n\n"
        f"{message.text.strip()}"
    )


@admin_router.callback_query(F.data.startswith("reply_buyer:"))
async def reply_buyer_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    buyer_id = int(callback.data.split(":", 1)[1])
    await callback.answer()
    await state.set_state(AdminReply.waiting_message)
    await state.update_data(buyer_id=buyer_id)
    await callback.message.answer("Type your reply — it'll be sent to this buyer as one message.")


@admin_router.message(AdminReply.waiting_message)
async def reply_buyer_finish(message: Message, state: FSMContext, bot: Bot):
    if _looks_like_command(message.text):
        await state.clear()
        await message.answer("Cancelled — please use the menu buttons below.")
        return
    data = await state.get_data()
    buyer_id = data.get("buyer_id")
    if not buyer_id:
        await state.clear()
        await message.answer("Something went wrong — please tap Reply again from the request message.")
        return
    await state.clear()
    try:
        await bot.send_message(buyer_id, f"💬 Message from the seller/admin:\n\n{message.text.strip()}")
        await message.answer("✅ Sent to the buyer.")
    except Exception:
        await message.answer("❌ Couldn't reach that buyer — they may have blocked the bot.")


@admin_router.callback_query(F.data == "admin:stats")
async def stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    categories = await list_categories()
    await callback.message.answer(f"📊 Categories: {len(categories)}\n(More stats land in Phase 5.)")


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

    dp.include_router(start_router)
    dp.include_router(seller_router)
    dp.include_router(admin_router)
    dp.include_router(buyer_router)
    dp.include_router(orders_router)

    print("Bot is starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

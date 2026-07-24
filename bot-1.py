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
import asyncio
import logging

import aiosqlite
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
DB_PATH = os.getenv("DB_PATH", "marketplace.db")

_raw_admins = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(x.strip()) for x in _raw_admins.split(",") if x.strip().isdigit()}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Set it as an env var or in a .env file.")

# Shown to buyers when they check out. Replace with your real bank/crypto details,
# or wire this up to Phase 2 gateway integrations later.
PAYMENT_INSTRUCTIONS = os.getenv(
    "PAYMENT_INSTRUCTIONS",
    "Bank Transfer:\n"
    "  Bank: <your bank>\n"
    "  Account Name: <your name>\n"
    "  Account Number: <your number>\n\n"
    "Or Crypto (USDT TRC20 / BTC / ETH / BNB):\n"
    "  Wallet: <your wallet address>\n\n"
    "After paying, send a screenshot of the payment here."
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def upsert_user(user_id: int, username: str, full_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO users (user_id, username, full_name)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
            (user_id, username, full_name),
        )
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


async def request_seller_status(user_id: int, shop_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET seller_status='pending', shop_name=? WHERE user_id=?",
            (shop_name, user_id),
        )
        await db.commit()


async def set_seller_decision(user_id: int, approved: bool):
    status = "approved" if approved else "rejected"
    role = "seller" if approved else "buyer"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET seller_status=?, role=? WHERE user_id=?",
            (status, role, user_id),
        )
        await db.commit()


async def get_pending_sellers():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE seller_status='pending'")
        return await cur.fetchall()


async def add_category(name: str, emoji: str = "📦"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO categories (name, emoji) VALUES (?, ?)", (name, emoji)
        )
        await db.commit()


async def list_categories():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM categories ORDER BY name")
        return await cur.fetchall()


async def create_product(seller_id: int, category_id: int, title: str,
                          description: str, price: float, stock_qty: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO products (seller_id, category_id, title, description, price, stock_qty)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (seller_id, category_id, title, description, price, stock_qty),
        )
        await db.commit()
        return cur.lastrowid


async def get_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM products WHERE product_id=?", (product_id,))
        return await cur.fetchone()


async def add_product_photo(product_id: int, file_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO product_photos (product_id, file_id) VALUES (?, ?)",
            (product_id, file_id),
        )
        await db.commit()


async def get_product_photos(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM product_photos WHERE product_id=?", (product_id,)
        )
        return await cur.fetchall()


async def list_products_by_category(category_id: int, limit: int = 20, offset: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM products WHERE category_id=? AND status='active'
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (category_id, limit, offset),
        )
        return await cur.fetchall()


async def list_products_by_seller(seller_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM products WHERE seller_id=? ORDER BY created_at DESC", (seller_id,)
        )
        return await cur.fetchall()


async def search_products(query: str, limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        like = f"%{query}%"
        cur = await db.execute(
            """SELECT * FROM products
               WHERE status='active' AND (title LIKE ? OR description LIKE ?)
               ORDER BY created_at DESC LIMIT ?""",
            (like, like, limit),
        )
        return await cur.fetchall()


async def toggle_favourite(user_id: int, product_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM favourites WHERE user_id=? AND product_id=?", (user_id, product_id)
        )
        exists = await cur.fetchone()
        if exists:
            await db.execute(
                "DELETE FROM favourites WHERE user_id=? AND product_id=?", (user_id, product_id)
            )
            await db.commit()
            return False
        else:
            await db.execute(
                "INSERT INTO favourites (user_id, product_id) VALUES (?, ?)", (user_id, product_id)
            )
            await db.commit()
            return True


async def list_favourites(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT p.* FROM favourites f
               JOIN products p ON p.product_id = f.product_id
               WHERE f.user_id=? ORDER BY p.created_at DESC""",
            (user_id,),
        )
        return await cur.fetchall()


# ---------- Orders ----------

async def create_order(buyer_id: int, product_id: int, seller_id: int,
                        quantity: int, total_price: float) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO orders (buyer_id, product_id, seller_id, quantity, total_price, status)
               VALUES (?, ?, ?, ?, ?, 'awaiting_payment')""",
            (buyer_id, product_id, seller_id, quantity, total_price),
        )
        await db.commit()
        return cur.lastrowid


async def attach_order_screenshot(order_id: int, file_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET screenshot_file_id=?, status='awaiting_approval' WHERE order_id=?",
            (file_id, order_id),
        )
        await db.commit()


async def get_order(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,))
        return await cur.fetchone()


async def set_order_status(order_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET status=? WHERE order_id=?", (status, order_id))
        await db.commit()


async def decrement_stock(product_id: int, quantity: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE products SET stock_qty = MAX(0, stock_qty - ?) WHERE product_id=?",
            (quantity, product_id),
        )
        await db.commit()


async def list_orders_by_buyer(buyer_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE buyer_id=? ORDER BY created_at DESC", (buyer_id,)
        )
        return await cur.fetchall()


async def list_orders_for_seller(seller_id: int, status: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cur = await db.execute(
                "SELECT * FROM orders WHERE seller_id=? AND status=? ORDER BY created_at DESC",
                (seller_id, status),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM orders WHERE seller_id=? ORDER BY created_at DESC", (seller_id,)
            )
        return await cur.fetchall()


async def list_orders_awaiting_approval():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM orders WHERE status='awaiting_approval' ORDER BY created_at ASC"
        )
        return await cur.fetchall()


# ---------- Reviews ----------

async def add_review(order_id: int, buyer_id: int, product_id: int, rating: int, comment: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO reviews (order_id, buyer_id, product_id, rating, comment)
               VALUES (?, ?, ?, ?, ?)""",
            (order_id, buyer_id, product_id, rating, comment),
        )
        await db.commit()


async def has_review(order_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM reviews WHERE order_id=?", (order_id,))
        return (await cur.fetchone()) is not None


async def list_reviews_for_product(product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM reviews WHERE product_id=? ORDER BY created_at DESC", (product_id,)
        )
        return await cur.fetchall()


# =========================================================
# KEYBOARDS
# =========================================================

def main_menu(is_seller: bool, is_admin_user: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🔎 Search"), KeyboardButton(text="📂 Categories")],
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


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Pending Sellers", callback_data="admin:pending_sellers")],
        [InlineKeyboardButton(text="📂 Add Category", callback_data="admin:add_category")],
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


class Search(StatesGroup):
    waiting_query = State()


class BuyProduct(StatesGroup):
    waiting_quantity = State()
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
    shop_name = message.text.strip()
    await request_seller_status(message.from_user.id, shop_name)
    await state.clear()

await message.answer(f"✓ Category added:

@admin_router.callback_query(F.data == "admin:

async def stats (callback: CallbackQuery):

if not is_admin(callback.from_user.id):

await callback.answer()

return

await callback.answer()

categories = await list_categories()

await callback.message.answer (f" Categor

# =

# ENTRYPOINT

#

async def main():

await init_db()

bot Bot(token=BOT_TOKEN, default=Default

dp = Dispatcher(storage=MemoryStorage())

dp.include_router(start_router)

dp.include_router (seller_router)

dp.include_router (admin_router)

dp.include_router (buyer_router)

dp.include_router (orders_router)

print("Bot is starting...")

await dp.start_polling(bot)

if _name_ " == _main__":

asyncio.run(main())

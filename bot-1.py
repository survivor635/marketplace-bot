import logging
import os
import sqlite3
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
OWNER_ID = int(os.environ.get("OWNER_ID", "5667943463"))
DB_PATH = "marketplace.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

NAME, PRICE, PHOTO = range(3)

def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price TEXT NOT NULL,
                photo_file_id TEXT,
                reserved INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()

def add_listing(name, price, photo_file_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "INSERT INTO listings (name, price, photo_file_id) VALUES (?, ?, ?)",
            (name, price, photo_file_id),
        )
        conn.commit()
        return cur.lastrowid

def get_available_listings(limit=10):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM listings WHERE reserved = 0 ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

def get_listing(listing_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()

def reserve_listing(listing_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "UPDATE listings SET reserved = 1 WHERE id = ? AND reserved = 0", (listing_id,)
        )
        conn.commit()
        return cur.rowcount > 0

def delete_listing(listing_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
        conn.commit()
        return cur.rowcount > 0

def is_owner(update: Update) -> bool:
    return update.effective_user.id == OWNER_ID

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_owner(update):
        await update.message.reply_text(
            "Welcome back! You're the shop owner.\n\n"
            "/sell - Add a new item for sale\n"
            "/browse - See your active listings\n"
            "/cancel - Cancel the current action"
        )
    else:
        await update.message.reply_text(
            "Welcome! Browse what's for sale and request to buy.\n\n"
            "/buy - See items available to buy"
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ---------- Owner: add listing ----------
async def sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("Only the shop owner can add items. Try /buy instead.")
        return ConversationHandler.END
    await update.message.reply_text("What are you selling? (item name)")
    return NAME

async def sell_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("What's the price?")
    return PRICE

async def sell_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["price"] = update.message.text
    await update.message.reply_text("Send a photo of the item (or type /skip to post without one).")
    return PHOTO

async def sell_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id
    await finish_listing(update, context, photo_file_id)
    return ConversationHandler.END

async def sell_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await finish_listing(update, context, None)
    return ConversationHandler.END

async def finish_listing(update, context, photo_file_id):
    listing_id = add_listing(
        context.user_data["name"], context.user_data["price"], photo_file_id
    )
    await update.message.reply_text(
        f"Added! #{listing_id}: {context.user_data['name']} - {context.user_data['price']}\n"
        "Buyers can now find it with /buy."
    )
    context.user_data.clear()

# ---------- Clients: browse & request to buy ----------
async def browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_available_listings(limit=10)
    if not rows:
        await update.message.reply_text("Nothing available right now. Check back soon!")
        return
    for row in rows:
        caption = f"{row['name']} - {row['price']}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("I want this", callback_data=f"want:{row['id']}")]]
        )
        if row["photo_file_id"]:
            await update.message.reply_photo(row["photo_file_id"], caption=caption, reply_markup=keyboard)
        else:
            await update.message.reply_text(caption, reply_markup=keyboard)

async def want_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    listing_id = int(query.data.split(":")[1])
    row = get_listing(listing_id)
    if not row or row["reserved"]:
        await query.message.reply_text("Sorry, that item is no longer available.")
        return

    if not reserve_listing(listing_id):
        await query.message.reply_text("Sorry, someone just grabbed that item.")
        return

    buyer = update.effective_user
    await query.edit_message_caption(caption=f"{row['name']} - {row['price']}\n\n✅ Reserved") \
        if row["photo_file_id"] else \
        await query.edit_message_text(f"{row['name']} - {row['price']}\n\n✅ Reserved")

    await query.message.reply_text(
        f"Nice! I've reserved \"{row['name']}\" for you. The seller will message you shortly."
    )

    buyer_name = f"@{buyer.username}" if buyer.username else buyer.first_name
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"🛒 New buyer request!\n"
                f"Item: {row['name']} - {row['price']}\n"
                f"Buyer: {buyer_name} (id: {buyer.id})"
            ),
        )
    except Exception as e:
        logger.error(f"Could not notify owner: {e}")

class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        pass


def _run_ping_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _PingHandler)
    server.serve_forever()


def main():
    threading.Thread(target=_run_ping_server, daemon=True).start()

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    sell_conv = ConversationHandler(
        entry_points=[CommandHandler("sell", sell_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_name)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_price)],
            PHOTO: [
                MessageHandler(filters.PHOTO, sell_photo),
                CommandHandler("skip", sell_skip_photo),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(sell_conv)
    app.add_handler(CommandHandler("buy", browse))
    app.add_handler(CommandHandler("browse", browse))
    app.add_handler(CallbackQueryHandler(want_callback, pattern=r"^want:"))

    logger.info("Bot starting...")
    app.run_polling(stop_signals=None)

if __name__ == "__main__":
    main()

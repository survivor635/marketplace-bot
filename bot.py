import logging
import os
import sqlite3
from contextlib import closing

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

# ---------- Config ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
DB_PATH = os.environ.get("DB_PATH", "marketplace.db")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- Conversation states for /sell ----------
NAME, PRICE, PHOTO = range(3)

# ---------- Database ----------
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,
                seller_username TEXT,
                name TEXT NOT NULL,
                price TEXT NOT NULL,
                photo_file_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def add_listing(seller_id, seller_username, name, price, photo_file_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "INSERT INTO listings (seller_id, seller_username, name, price, photo_file_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (seller_id, seller_username, name, price, photo_file_id),
        )
        conn.commit()
        return cur.lastrowid


def get_listings(limit=10, offset=0):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM listings ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return rows


def get_listing(listing_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()


def delete_listing(listing_id, seller_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(
            "DELETE FROM listings WHERE id = ? AND seller_id = ?",
            (listing_id, seller_id),
        )
        conn.commit()
        return cur.rowcount > 0


# ---------- Handlers: basic ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to the Marketplace Bot!\n\n"
        "/sell - List an item for sale\n"
        "/browse - Browse items for sale\n"
        "/myitems - View & remove your own listings\n"
        "/cancel - Cancel the current action"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------- Handlers: /sell conversation ----------
async def sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("What are you selling? (item name)")
    return NAME


async def sell_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("What's the price?")
    return PRICE


async def sell_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["price"] = update.message.text
    await update.message.reply_text(
        "Send a photo of the item (or type /skip to post without one)."
    )
    return PHOTO


async def sell_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id
    await finish_listing(update, context, photo_file_id)
    return ConversationHandler.END


async def sell_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await finish_listing(update, context, None)
    return ConversationHandler.END


async def finish_listing(update, context, photo_file_id):
    user = update.effective_user
    listing_id = add_listing(
        user.id,
        user.username or user.first_name,
        context.user_data["name"],
        context.user_data["price"],
        photo_file_id,
    )
    await update.message.reply_text(
        f"Listed! #{listing_id}: {context.user_data['name']} - {context.user_data['price']}\n"
        "Buyers can now find it with /browse."
    )
    context.user_data.clear()


# ---------- Handlers: /browse ----------
async def browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_listings(limit=10)
    if not rows:
        await update.message.reply_text("No listings yet. Be the first with /sell!")
        return

    for row in rows:
        caption = f"#{row['id']} {row['name']} - {row['price']}\nSeller: @{row['seller_username']}"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Contact seller", callback_data=f"contact:{row['id']}")]]
        )
        if row["photo_file_id"]:
            await update.message.reply_photo(
                row["photo_file_id"], caption=caption, reply_markup=keyboard
            )
        else:
            await update.message.reply_text(caption, reply_markup=keyboard)


async def contact_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    listing_id = int(query.data.split(":")[1])
    row = get_listing(listing_id)
    if not row:
        await query.message.reply_text("That listing no longer exists.")
        return
    await query.message.reply_text(
        f"Contact the seller: @{row['seller_username']} about \"{row['name']}\""
    )


# ---------- Handlers: /myitems ----------
async def myitems(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM listings WHERE seller_id = ? ORDER BY created_at DESC",
            (user.id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("You have no active listings.")
        return

    for row in rows:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Remove listing", callback_data=f"remove:{row['id']}")]]
        )
        await update.message.reply_text(
            f"#{row['id']} {row['name']} - {row['price']}", reply_markup=keyboard
        )


async def remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    listing_id = int(query.data.split(":")[1])
    user = update.effective_user
    if delete_listing(listing_id, user.id):
        await query.edit_message_text(f"Listing #{listing_id} removed.")
    else:
        await query.edit_message_text("Couldn't remove that listing (not yours or already gone).")


def main():
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
    app.add_handler(CommandHandler("browse", browse))
    app.add_handler(CommandHandler("myitems", myitems))
    app.add_handler(CallbackQueryHandler(contact_callback, pattern=r"^contact:"))
    app.add_handler(CallbackQueryHandler(remove_callback, pattern=r"^remove:"))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

# All Toolz Market — Website

A standalone online store. It shows the same products as your Telegram bot
(same Turso database), but has its own checkout — manual bank transfer,
confirmed by you — completely separate from the bot's own order flow.

## How the payment flow works

1. Customer fills in their details and picks a quantity.
2. They see your bank account details and are asked to transfer the total,
   then upload a screenshot of the transfer.
3. The screenshot is sent straight to your Telegram admin account(s) via the
   bot token — no image is stored on the website itself.
4. You open `/admin?key=YOUR_ADMIN_PANEL_KEY` on the site, check the
   screenshot in Telegram, and click **Confirm** or **Reject**. Confirming
   deducts stock; the customer's status page updates automatically.

## What you need before deploying

1. Your existing **Turso** database URL and auth token (same ones the bot uses).
2. Your bot's **BOT_TOKEN** and **ADMIN_IDS** (same ones the bot uses) — used
   here to send you Telegram messages and the payment screenshot.
3. Your **bank details** to show customers (account name, number, bank name).
4. A long random string to use as your **admin panel key** — treat it like a
   password; anyone with the link can see pending orders.

## Deploying on Render (same account as your bot)

1. Push this `webstore` folder to a **new** GitHub repo (or a new folder in
   your existing repo — just make sure Render points at this folder).
2. In Render: **New +** → **Web Service** → connect the repo.
3. Set:
   - **Root Directory**: the folder this file is in (if it's a subfolder of
     an existing repo)
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
4. Under **Environment**, add these variables:

   | Key | Value |
   |---|---|
   | `TURSO_DATABASE_URL` | same as your bot |
   | `TURSO_AUTH_TOKEN` | same as your bot |
   | `BOT_TOKEN` | same as your bot |
   | `ADMIN_IDS` | same as your bot |
   | `ADMIN_PANEL_KEY` | any long random string — this guards `/admin` |
   | `BANK_NAME` | e.g. `GTBank` |
   | `BANK_ACCOUNT_NUMBER` | your account number |
   | `BANK_ACCOUNT_NAME` | the name on the account |
   | `FLASK_SECRET_KEY` | any random string (mash your keyboard) |

5. Click **Create Web Service**. Once it's live, bookmark
   `https://your-site.onrender.com/admin?key=YOUR_ADMIN_PANEL_KEY` for
   reviewing orders.

## What this does NOT do

- It doesn't touch your Telegram bot's `orders` table — website purchases
  are stored separately in a new `web_orders` table, so nothing about your
  bot's order history changes.
- It doesn't run the bot itself — it just sends plain Telegram messages
  (and the payment screenshot) to your admin ID(s), using the same bot token.
- It doesn't store payment screenshots anywhere — they're forwarded straight
  to Telegram and never saved on the server or in the database.
- Stock is shared and only decreases once you **confirm** an order — so
  unpaid or rejected orders never touch stock, same as the bot.

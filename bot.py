import logging
import sqlite3
import requests
import random
import string
import pandas as pd
import qrcode
import io
import os

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    InputFile, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackContext,
    CallbackQueryHandler, MessageHandler, filters
)

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS  = os.getenv("WALLET_ADDRESS")
ESIM_API_KEY    = os.getenv("ESIM_API_KEY")
TRONSCAN_API    = (
    "https://apilist.tronscanapi.com/api/transaction?"
    "sort=-timestamp&count=true&limit=20&start=0&address="
)
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

# === DATABASE SETUP ===
conn = sqlite3.connect("esim_bot.db", check_same_thread=False)
c    = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT,
    username   TEXT,
    amount     REAL,
    memo       TEXT,
    plan_id    TEXT,
    paid       INTEGER DEFAULT 0,
    esim_url   TEXT
)
""")
c.execute("""
CREATE TABLE IF NOT EXISTS balances (
    user_id TEXT PRIMARY KEY,
    balance REAL DEFAULT 0
)
""")
conn.commit()

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === UTILITIES ===
def generate_memo() -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def check_tron_payment(memo: str, expected_amount: float) -> float:
    try:
        data = requests.get(TRONSCAN_API + WALLET_ADDRESS, timeout=5).json()
        for tx in data.get("data", []):
            if tx.get("data") and memo in tx["data"]:
                amt = float(tx["tokenTransferInfo"]["amount_str"]) / 1e6
                if abs(amt - expected_amount) < 0.01:
                    return amt
        return 0.0
    except Exception as e:
        logger.error(f"TRON check error: {e}")
        return 0.0

def order_esim(user_id: int, memo: str, plan_id: str) -> str | None:
    headers = {"Authorization": f"Bearer {ESIM_API_KEY}"}
    payload = {
        "external_id": memo,
        "email":       f"botuser{user_id}@esim.bot",
        "plan_id":     plan_id
    }
    r = requests.get("https://api.esimaccess.com/v1/plans?active=true", headers=headers)  # sanity check
    r = requests.post("https://api.esimaccess.com/v1/orders",
                      headers=headers, json=payload, timeout=10)
    if r.status_code == 200:
        return r.json().get("activation_code_url")
    return None

def load_plans() -> pd.DataFrame:
    """
    Fetch current eSIM plans via API, normalize into a DataFrame
    with columns: ID, Name, Region, Price(USD).
    """
    try:
        url = "https://api.esimaccess.com/v1/plans?active=true"
        headers = {"Authorization": f"Bearer {ESIM_API_KEY}"}
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()  # expect a list of plan objects

        # normalize to flat table
        df = pd.json_normalize(data)
        # rename columns to our schema
        df = df.rename(columns={
            "id":        "ID",
            "name":      "Name",
            "region":    "Region",
            "price_usd": "Price(USD)"
        })
        # ensure price is float
        df["Price(USD)"] = df["Price(USD)"].astype(float)

        return df[["ID","Name","Region","Price(USD)"]]
    except Exception as e:
        logger.error(f"Failed to load plans via API: {e}")
        return pd.DataFrame()

def send_qr_code(text: str) -> InputFile:
    qr = qrcode.make(text)
    bio = io.BytesIO()
    bio.name = "qrcode.png"
    qr.save(bio, "PNG")
    bio.seek(0)
    return InputFile(bio, filename="qrcode.png")

# === BOT COMMANDS ===

async def help_cmd(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "\U0001F4D6 *eSIM Bot Help*\n\n"
        "*/start* – Show main menu\n"
        "*/balance* – View your balance (with your user ID)\n"
        "*/check* – Check pending payment(s)\n"
        "*/topup <amount>* – Request a top-up (get QR + memo)\n"
        "*/topup <user_id> <amount>* – Credit user immediately (admin only)\n"
        "*/admin* – Sales stats (admin only)\n\n"
        "\U0001F4B3 Tip: Use your USDT balance for instant purchases.",
        parse_mode="Markdown"
    )

async def start(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    buttons = [
        [KeyboardButton("📦 Browse eSIMs"), KeyboardButton("💰 My Balance")],
        [KeyboardButton("✅ Check Payment"), KeyboardButton("📖 Help")]
    ]
    if user_id in ADMIN_IDS:
        buttons.append([KeyboardButton("➕ Topup"), KeyboardButton("📊 Admin Stats")])
    menu = ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    await update.message.reply_text("Welcome! Use the menu below to get started:", reply_markup=menu)

async def browse_esims(update: Update, context: CallbackContext):
    df = load_plans()
    if df.empty:
        return await update.message.reply_text("No plans available right now.")
    regions = sorted(df["Region"].unique())
    kb = [[InlineKeyboardButton(r, callback_data=f"REGION_{r}")] for r in regions]
    await update.message.reply_text("🌍 Choose a country/region:", reply_markup=InlineKeyboardMarkup(kb))

async def region_selector(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    _, region = query.data.split("_",1)

    df = load_plans()
    plans = df[df["Region"] == region].sort_values("Price(USD)")
    if plans.empty:
        return await query.message.reply_text("No plans found for that region.")

    kb = []
    for _, row in plans.iterrows():
        price_tag = max(row["Price(USD)"], 5.0)
        kb.append([InlineKeyboardButton(
            f"{row['Name']} – ${row['Price(USD)']:.2f}",
            callback_data=f"PLAN_{row['ID']}_{price_tag:.2f}"
        )])
    await query.message.reply_text(f"📡 Plans in {region}:", reply_markup=InlineKeyboardMarkup(kb))

async def button_handler(update: Update, context: CallbackContext):
    query    = update.callback_query
    await query.answer()
    _, plan_id, usd_s = query.data.split("_")
    usd      = float(usd_s)
    user_id  = query.from_user.id

    c.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,))
    row      = c.fetchone()
    balance  = row[0] if row else 0.0

    if usd < 5.0:
        await query.message.reply_text("⚠️ Minimum payment is 5 USDT; extra will be credited.")
        usd = 5.0

    if balance >= usd:
        new_bal = balance - usd
        memo    = generate_memo()
        url     = order_esim(user_id, memo, plan_id)
        if url:
            c.execute("UPDATE balances SET balance=? WHERE user_id=?", (new_bal, user_id))
            conn.commit()
            await query.message.reply_photo(photo=send_qr_code(url),
                                            caption=f"✅ eSIM activated!\n{url}")
        else:
            await query.message.reply_text("❌ Ordering failed—please try again.")
    else:
        memo  = generate_memo()
        uname = query.from_user.username or str(user_id)
        c.execute(
            "INSERT INTO orders (user_id,username,amount,memo,plan_id) VALUES (?,?,?,?,?)",
            (user_id, uname, usd, memo, plan_id)
        )
        conn.commit()
        pay_txt = (
            f"Send exactly *{usd:.2f} USDT (TRC20)* to:\n"
            f"`{WALLET_ADDRESS}`\nMemo/Tag: `{memo}`"
        )
        await query.message.reply_photo(photo=send_qr_code(pay_txt),
                                        caption=pay_txt,
                                        parse_mode="Markdown")

async def check(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    c.execute("SELECT id,memo,amount FROM orders WHERE user_id=? AND paid=0", (user_id,))
    row = c.fetchone()
    if not row:
        return await update.message.reply_text("No pending orders.")
    order_id, memo, amount = row
    paid_amt = check_tron_payment(memo, amount)
    if paid_amt:
        c.execute("UPDATE orders SET paid=1 WHERE id=?", (order_id,))
        c.execute(
            "INSERT INTO balances(user_id,balance) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET balance=balance+?",
            (user_id, paid_amt, paid_amt)
        )
        conn.commit()
        await update.message.reply_text(f"✅ Received {paid_amt:.2f} USDT! Balance updated.")
    else:
        await update.message.reply_text("❌ Payment not found yet. Try later.")

async def balance(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    c.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,))
    row = c.fetchone()
    bal = row[0] if row else 0.0
    await update.message.reply_text(f"💰 Balance: {bal:.2f} USDT\n👤 Your ID: {user_id}")

async def topup(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    args    = context.args

    # Admin: immediate credit
    if user_id in ADMIN_IDS and len(args) == 2:
        tgt, amt = args[0], float(args[1])
        c.execute(
            "INSERT INTO balances(user_id,balance) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET balance=balance+?",
            (tgt, amt, amt)
        )
        conn.commit()
        return await update.message.reply_text(f"✅ Credited {amt:.2f} USDT to user {tgt}.")

    # User: request top-up
    if len(args) == 1:
        amt  = float(args[0])
        memo = generate_memo()
        uname = update.message.from_user.username or str(user_id)
        c.execute(
            "INSERT INTO orders (user_id,username,amount,memo,plan_id) VALUES(?,?,?,?,?)",
            (user_id, uname, amt, memo, "TOPUP")
        )
        conn.commit()
        pay_txt = (
            f"🔋 *Top-Up Request*\n\n"
            f"Send *{amt:.2f} USDT (TRC20)* to:\n"
            f"`{WALLET_ADDRESS}`\nMemo/Tag: `{memo}`"
        )
        return await update.message.reply_photo(photo=send_qr_code(pay_txt),
                                                caption=pay_txt,
                                                parse_mode="Markdown")

    # Fallback
    usage = "Usage: /topup <amount>" if user_id not in ADMIN_IDS else "Usage: /topup <user_id> <amount>"
    await update.message.reply_text(usage)

async def admin(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        return await update.message.reply_text("Unauthorized.")
    c.execute("SELECT COUNT(*), SUM(amount) FROM orders WHERE paid=1")
    sold, rev = c.fetchone()
    c.execute("SELECT COUNT(DISTINCT user_id) FROM orders")
    users = c.fetchone()[0]
    await update.message.reply_text(
        f"📊 Sales Report:\n"
        f"- eSIMs sold: {sold}\n"
        f"- Revenue: ${rev or 0:.2f}\n"
        f"- Active users: {users}"
    )

async def handle_main_menu(update: Update, context: CallbackContext):
    txt = update.message.text
    uid = update.message.from_user.id

    if txt == "📦 Browse eSIMs":
        return await browse_esims(update, context)
    if txt == "💰 My Balance":
        return await balance(update, context)
    if txt == "✅ Check Payment":
        return await check(update, context)
    if txt == "📖 Help":
        return await help_cmd(update, context)
    if txt == "➕ Topup" and uid in ADMIN_IDS:
        return await update.message.reply_text("Use `/topup <user_id> <amount>`", parse_mode="Markdown")
    if txt == "📊 Admin Stats" and uid in ADMIN_IDS:
        return await admin(update, context)
    await update.message.reply_text("Unknown option. Use the menu or /help.")

# === SETUP & RUN ===
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# Slash commands
app.add_handler(CommandHandler("start",  start))
app.add_handler(CommandHandler("help",   help_cmd))
app.add_handler(CommandHandler("balance",balance))
app.add_handler(CommandHandler("check",  check))
app.add_handler(CommandHandler("topup",  topup))
app.add_handler(CommandHandler("admin",  admin))

# Inline callbacks
app.add_handler(CallbackQueryHandler(region_selector, pattern="^REGION_"))
app.add_handler(CallbackQueryHandler(button_handler,   pattern="^PLAN_"))

# Main-menu text handler
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu))

if __name__ == "__main__":
    print("Bot is starting…")
    app.run_polling()

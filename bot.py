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
def generate_memo():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def check_tron_payment(memo, expected_amount):
    try:
        data = requests.get(TRONSCAN_API + WALLET_ADDRESS).json()
        for tx in data.get("data", []):
            if tx.get("data") and memo in tx["data"]:
                amt = float(tx["tokenTransferInfo"]["amount_str"]) / 1e6
                if abs(amt - expected_amount) < 0.01:
                    return amt
        return 0
    except Exception as e:
        logger.error(f"TRON check error: {e}")
        return 0

def order_esim(user_id, memo, plan_id):
    headers = {"Authorization": f"Bearer {ESIM_API_KEY}"}
    payload = {
        "external_id": memo,
        "email":       f"botuser{user_id}@esim.bot",
        "plan_id":     plan_id
    }
    r = requests.post("https://api.esimaccess.com/v1/orders",
                      headers=headers, json=payload)
    if r.status_code == 200:
        return r.json().get("activation_code_url")
    return None

def load_plans():
    try:
        # fetch the latest Price.csv from GitHub
        url = "https://raw.githubusercontent.com/Aleksa1302/esimbot/main/Price.csv"
        df = pd.read_csv(url)
        df['Price(USD)'] = df['Price(USD)'].replace(r'[\$,]', '', regex=True).astype(float)
        return df
    except Exception as e:
        logger.error(f"Failed to load Price.csv from GitHub: {e}")
        return pd.DataFrame()

def send_qr_code(text):
    qr = qrcode.make(text)
    bio = io.BytesIO()
    bio.name = "qrcode.png"
    qr.save(bio, "PNG")
    bio.seek(0)
    return InputFile(bio, filename="qrcode.png")

# === COMMANDS ===

async def help_cmd(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "\U0001F4D6 *eSIM Bot Help*\n\n"
        "*/start* – Show main menu\n"
        "*/balance* – View your balance\n"
        "*/check* – Check pending payment\n"
        "*/admin* – Sales stats (admin only)\n"
        "*/topup <user_id> <amt>* – Credit user (admin only)\n\n"
        "\U0001F4B3 Tip: You can use your USDT balance to buy plans instantly.",
        parse_mode="Markdown"
    )

async def start(update: Update, context: CallbackContext):
    """Show main menu—users get user options, admins see extra buttons."""
    user_id = update.effective_user.id
    buttons = [
        [KeyboardButton("📦 Browse eSIMs"), KeyboardButton("💰 My Balance")],
        [KeyboardButton("✅ Check Payment"), KeyboardButton("📖 Help")]
    ]
    if user_id in ADMIN_IDS:
        buttons.append([KeyboardButton("📊 Admin Stats"), KeyboardButton("➕ Topup")])

    menu = ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    await update.message.reply_text(
        "Welcome! Use the menu below to get started:",
        reply_markup=menu
    )

async def browse_esims(update: Update, context: CallbackContext):
    """Kick off the region → plan inline flow."""
    df = load_plans()
    if df.empty:
        await update.message.reply_text("No plans available. Please try again later.")
        return

    regions = sorted(df["Region"].unique())
    kb = [[InlineKeyboardButton(r, callback_data=f"REGION_{r}")] for r in regions]
    await update.message.reply_text(
        "🌍 Choose a region:", 
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def region_selector(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    region = query.data.split("_", 1)[1]

    df = load_plans()
    plans = df[df["Region"] == region].sort_values("Price(USD)")
    if plans.empty:
        await query.message.reply_text("No plans in this region.")
        return

    kb = []
    for _, row in plans.iterrows():
        label = f"{row['Name']} – ${row['Price(USD)']:.2f}"
        amt   = max(row["Price(USD)"], 5.0)
        cb    = f"PLAN_{row['ID']}_{amt:.2f}"
        kb.append([InlineKeyboardButton(label, callback_data=cb)])

    await query.message.reply_text(
        f"📡 Plans for {region}:", 
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    _, plan_id, usd_s = query.data.split("_")
    usd = float(usd_s)
    user_id = query.from_user.id

    # fetch current balance
    c.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,))
    row = c.fetchone()
    balance = row[0] if row else 0.0

    # enforce minimum
    if usd < 5.0:
        await query.message.reply_text(
            "⚠️ Minimum payment is 5 USDT.\n"
            "Any extra will be credited to your balance."
        )
        usd = 5.0

    if balance >= usd:
        # pay from balance
        new_bal = balance - usd
        memo   = generate_memo()
        url    = order_esim(user_id, memo, plan_id)
        if url:
            c.execute(
                "UPDATE balances SET balance=? WHERE user_id=?",
                (new_bal, user_id)
            )
            await query.message.reply_photo(
                photo=send_qr_code(url),
                caption=f"✅ eSIM activated!\n{url}"
            )
            conn.commit()
        else:
            await query.message.reply_text("❌ Ordering failed, please try again.")
    else:
        # request on-chain payment
        memo = generate_memo()
        uname = query.from_user.username or user_id
        c.execute(
            "INSERT INTO orders "
            "(user_id,username,amount,memo,plan_id) VALUES (?,?,?,?,?)",
            (user_id, uname, usd, memo, plan_id)
        )
        conn.commit()
        pay_txt = (
            f"Send exactly *{usd:.2f} USDT (TRC20)* to:\n"
            f"`{WALLET_ADDRESS}`\nMemo/Tag: `{memo}`"
        )
        await query.message.reply_photo(
            photo=send_qr_code(pay_txt),
            caption=pay_txt,
            parse_mode="Markdown"
        )

async def check(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    c.execute(
        "SELECT id,memo,amount FROM orders "
        "WHERE user_id=? AND paid=0", (user_id,)
    )
    row = c.fetchone()
    if not row:
        return await update.message.reply_text("No pending orders.")

    order_id, memo, amt = row
    paid_amt = check_tron_payment(memo, amt)
    if paid_amt:
        c.execute("UPDATE orders SET paid=1 WHERE id=?", (order_id,))
        c.execute(
            "INSERT INTO balances(user_id,balance) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET balance=balance+?",
            (user_id, paid_amt, paid_amt)
        )
        conn.commit()
        await update.message.reply_text(
            f"✅ Received {paid_amt:.2f} USDT! Added to your balance."
        )
    else:
        await update.message.reply_text("❌ Payment not found. Try again later.")

async def balance(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    c.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,))
    row = c.fetchone()
    bal = row[0] if row else 0.0
    await update.message.reply_text(f"💰 Your balance: {bal:.2f} USDT")

async def topup(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        return await update.message.reply_text("Unauthorized.")
    if len(context.args) != 2:
        return await update.message.reply_text("Usage: /topup <user_id> <amount>")

    tgt, amt = context.args
    amt = float(amt)
    c.execute(
        "INSERT INTO balances(user_id,balance) VALUES(?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance=balance+?",
        (tgt, amt, amt)
    )
    conn.commit()
    await update.message.reply_text(f"✅ Topped up {amt:.2f} USDT to {tgt}.")

async def admin(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        return await update.message.reply_text("Unauthorized.")

    c.execute("SELECT COUNT(*), SUM(amount) FROM orders WHERE paid=1")
    cnt, total = c.fetchone()
    c.execute("SELECT COUNT(DISTINCT user_id) FROM orders")
    users = c.fetchone()[0]

    await update.message.reply_text(
        f"📊 Sales:\n"
        f"- Total eSIMs sold: {cnt}\n"
        f"- Total Revenue: ${total or 0:.2f}\n"
        f"- Active Users: {users}"
    )

# === MAIN MENU HANDLER ===
async def handle_main_menu(update: Update, context: CallbackContext):
    text    = update.message.text
    user_id = update.message.from_user.id

    if text == "📦 Browse eSIMs":
        return await browse_esims(update, context)
    if text == "💰 My Balance":
        return await balance(update, context)
    if text == "✅ Check Payment":
        return await check(update, context)
    if text == "📖 Help":
        return await help_cmd(update, context)
    if text == "📊 Admin Stats" and user_id in ADMIN_IDS:
        return await admin(update, context)
    if text == "➕ Topup" and user_id in ADMIN_IDS:
        return await update.message.reply_text("Use `/topup <user_id> <amount>`", parse_mode="Markdown")

    # fallback
    await update.message.reply_text("Unknown option. Use the menu or /help.")

# === SETUP & RUN ===
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# Slash commands
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help",  help_cmd))
app.add_handler(CommandHandler("balance", balance))
app.add_handler(CommandHandler("check",   check))
app.add_handler(CommandHandler("topup",   topup))
app.add_handler(CommandHandler("admin",   admin))

# Inline button callbacks
app.add_handler(CallbackQueryHandler(region_selector, pattern="^REGION_"))
app.add_handler(CallbackQueryHandler(button_handler,  pattern="^PLAN_"))

# Main-menu text handler
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu))

if __name__ == "__main__":
    print("Bot is starting…")
    app.run_polling()

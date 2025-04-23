import logging
import sqlite3
import time
import requests
import random
import string
import pandas as pd
import qrcode
import io
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackContext, CallbackQueryHandler, MessageHandler, filters
import os

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
ESIM_API_KEY = os.getenv("ESIM_API_KEY")
TRONSCAN_API = 'https://apilist.tronscanapi.com/api/transaction?sort=-timestamp&count=true&limit=20&start=0&address='
PRICE_CSV = 'Price.csv'
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))  # Comma-separated admin Telegram user IDs

# === DB Setup ===
conn = sqlite3.connect("esim_bot.db", check_same_thread=False)
c = conn.cursor()
c.execute("""CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    username TEXT,
    amount REAL,
    memo TEXT,
    plan_id TEXT,
    paid INTEGER DEFAULT 0,
    esim_url TEXT
)""")
c.execute("""CREATE TABLE IF NOT EXISTS balances (
    user_id TEXT PRIMARY KEY,
    balance REAL DEFAULT 0
)""")
conn.commit()

# === LOGGING ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === UTILS ===
def generate_memo():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

def check_tron_payment(memo, expected_amount):
    try:
        url = TRONSCAN_API + WALLET_ADDRESS
        r = requests.get(url).json()
        for tx in r['data']:
            if tx.get('data') and memo in tx.get('data'):
                amount = float(tx['tokenTransferInfo']['amount_str']) / 1e6
                if abs(amount - expected_amount) < 0.01:
                    return amount
        return 0
    except Exception as e:
        logger.error(f"TRON check error: {e}")
        return 0

def order_esim(user_id, memo, plan_id):
    headers = {"Authorization": f"Bearer {ESIM_API_KEY}"}
    data = {
        "external_id": memo,
        "email": f"botuser{user_id}@esim.bot",
        "plan_id": plan_id
    }
    r = requests.post("https://api.esimaccess.com/v1/orders", headers=headers, json=data)
    if r.status_code == 200:
        return r.json().get('activation_code_url')
    return None

def load_plans():
    try:
        df = pd.read_csv(PRICE_CSV)
        df['Price(USD)'] = df['Price(USD)'].replace('[\$,]', '', regex=True).astype(float)
        return df
    except Exception as e:
        logger.error(f"Failed to load Price.csv: {e}")
        return pd.DataFrame()

# === QR Code Generator ===
def send_qr_code(update, text):
    qr = qrcode.make(text)
    bio = io.BytesIO()
    bio.name = 'qrcode.png'
    qr.save(bio, 'PNG')
    bio.seek(0)
    return InputFile(bio, filename="qrcode.png")

# === BOT COMMANDS ===

async def region_selector(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    selected_region = query.data.replace("REGION_", "")

    plans_df = load_plans()
    region_plans = plans_df[plans_df['Region'] == selected_region].sort_values(by='Price(USD)')

    if region_plans.empty:
        await query.message.reply_text("No plans available for this region.")
        return

    keyboard = []
    for _, row in region_plans.iterrows():
        label = f"{row['Name']} - ${row['Price(USD)']:.2f}"
        data = f"PLAN_{row['ID']}_{row['Price(USD)']:.2f}"
        keyboard.append([InlineKeyboardButton(label, callback_data=data)])
    await query.message.reply_text(f"📡 Available plans for {selected_region}:", reply_markup=InlineKeyboardMarkup(keyboard))
async def start(update: Update, context: CallbackContext):
    plans_df = load_plans()
    if plans_df.empty:
        await update.message.reply_text("No eSIM plans available at the moment. Please try again later.")
        return

    regions = sorted(plans_df['Region'].unique())
    keyboard = [[InlineKeyboardButton(region, callback_data=f"REGION_{region}")] for region in regions]
    await update.message.reply_text("🌍 Choose a region:", reply_markup=InlineKeyboardMarkup(keyboard))

    keyboard = []
    for _, row in plans_df.iterrows():
        label = f"{row['Region']} - {row['Name']} - ${row['Price(USD)']:.2f}"
        data = f"PLAN_{row['ID']}_{row['Price(USD)']:.2f}"
        keyboard.append([InlineKeyboardButton(label, callback_data=data)])
    await update.message.reply_text("Welcome! Select your eSIM plan:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    _, plan_id, usd = query.data.split('_')
    usd = float(usd)

    user_id = query.from_user.id
    c.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,))
    row = c.fetchone()
    balance = row[0] if row else 0.0

    if balance >= usd:
        new_balance = balance - usd
        memo = generate_memo()
        esim_url = order_esim(user_id, memo, plan_id)
        if esim_url:
            c.execute("UPDATE balances SET balance=? WHERE user_id=?", (new_balance, user_id))
            await query.message.reply_photo(photo=send_qr_code(update, esim_url), caption=f"✅ eSIM activated from balance!\n{esim_url}")
        else:
            await query.message.reply_text("❌ Failed to order eSIM.")
        conn.commit()
    else:
        memo = generate_memo()
        username = query.from_user.username
        c.execute("INSERT INTO orders (user_id, username, amount, memo, plan_id) VALUES (?, ?, ?, ?, ?)", (user_id, username, usd, memo, plan_id))
        conn.commit()
        payment_text = f"Send exactly {usd} USDT (TRC20) to:\n{WALLET_ADDRESS}\nMemo/Tag: {memo}"
        await query.message.reply_photo(photo=send_qr_code(update, payment_text), caption=payment_text)

async def check(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    c.execute("SELECT id, memo, amount, paid FROM orders WHERE user_id=? AND paid=0", (user_id,))
    row = c.fetchone()
    if not row:
        await update.message.reply_text("No unpaid orders found.")
        return

    order_id, memo, amount, paid = row
    paid_amount = check_tron_payment(memo, amount)
    if paid_amount:
        c.execute("UPDATE orders SET paid=1 WHERE id=?", (order_id,))
        c.execute("INSERT INTO balances(user_id, balance) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?", (user_id, paid_amount, paid_amount))
        conn.commit()
        await update.message.reply_text(f"✅ {paid_amount} USDT received! Added to your balance.")
    else:
        await update.message.reply_text("❌ Payment not found yet. Please wait or try again later.")

async def balance(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    c.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,))
    row = c.fetchone()
    balance = row[0] if row else 0.0
    await update.message.reply_text(f"💰 Your balance: {balance:.2f} USDT")

async def topup(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /topup <user_id> <amount>")
        return
    target_id = context.args[0]
    amount = float(context.args[1])
    c.execute("INSERT INTO balances(user_id, balance) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET balance = balance + ?", (target_id, amount, amount))
    conn.commit()
    await update.message.reply_text(f"✅ Topped up {amount} USDT to user {target_id}.")

async def upload_csv(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    if update.message.document:
        file = await update.message.document.get_file()
        await file.download_to_drive(custom_path=PRICE_CSV)
        await update.message.reply_text("✅ Price.csv updated successfully.")
    else:
        await update.message.reply_text("❌ Please send a .csv file.")

async def admin(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    c.execute("SELECT COUNT(*), SUM(amount) FROM orders WHERE paid=1")
    count, total = c.fetchone()
    c.execute("SELECT DISTINCT user_id FROM orders")
    users = c.fetchall()
    msg = f"📊 Sales Report:\n- Total Sales: {count} eSIMs\n- Total Revenue: ${total:.2f if total else 0}\n- Active Users: {len(users)}"
    await update.message.reply_text(msg)

# === MAIN ===
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(CallbackQueryHandler(region_selector, pattern="^REGION_"))
app.add_handler(CommandHandler("check", check))
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CommandHandler("balance", balance))
app.add_handler(CommandHandler("topup", topup))
app.add_handler(MessageHandler(filters.Document.FileExtension("csv"), upload_csv))

if __name__ == '__main__':
    print("Bot is running...")
    app.run_polling()


import logging
import sqlite3
import time
import requests
import random
import string
import pandas as pd
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackContext, CallbackQueryHandler
import os

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = 'TCSQBnCjaX9EDgD24V3C4dTkfi98PFfT3s'
ESIM_API_KEY = os.getenv("ESIM_API_KEY")
TRONSCAN_API = 'https://apilist.tronscanapi.com/api/transaction?sort=-timestamp&count=true&limit=20&start=0&address='
PRICE_CSV = 'Price.csv'

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
                    return True
        return False
    except Exception as e:
        logger.error(f"TRON check error: {e}")
        return False

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
    df = pd.read_csv(PRICE_CSV)
    df['Price(USD)'] = df['Price(USD)'].replace('[\$,]', '', regex=True).astype(float)
    return df

# === BOT COMMANDS ===
async def start(update: Update, context: CallbackContext):
    plans_df = load_plans()
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

    memo = generate_memo()
    user_id = query.from_user.id
    username = query.from_user.username

    # Save order
    c.execute("INSERT INTO orders (user_id, username, amount, memo, plan_id) VALUES (?, ?, ?, ?, ?)", (user_id, username, usd, memo, plan_id))
    conn.commit()

    msg = f"Send exactly {usd} USDT (TRC20) to:\n<code>{WALLET_ADDRESS}</code>\nMemo/Tag: <code>{memo}</code>\nAfter payment is confirmed, you'll receive your eSIM link."
    await query.message.reply_text(msg, parse_mode='HTML')

async def check(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    c.execute("SELECT id, memo, amount, paid, plan_id FROM orders WHERE user_id=? AND paid=0", (user_id,))
    row = c.fetchone()
    if not row:
        await update.message.reply_text("No unpaid orders found.")
        return

    order_id, memo, amount, paid, plan_id = row
    if check_tron_payment(memo, amount):
        esim_url = order_esim(user_id, memo, plan_id)
        if esim_url:
            c.execute("UPDATE orders SET paid=1, esim_url=? WHERE id=?", (esim_url, order_id))
            conn.commit()
            await update.message.reply_text(f"✅ Payment received! Here is your eSIM:\n{esim_url}")
        else:
            await update.message.reply_text("Payment received but failed to get eSIM. Contact support.")
    else:
        await update.message.reply_text("❌ Payment not found yet. Please wait or try again later.")

async def admin(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
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
app.add_handler(CommandHandler("check", check))
app.add_handler(CommandHandler("admin", admin))

if __name__ == '__main__':
    print("Bot is running...")
    app.run_polling()

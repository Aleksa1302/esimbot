
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
async def whoami(update: Update, context: CallbackContext):
    user = update.message.from_user
    user_id = user.id
    username = user.username or "(no username)"
    message = (
        f"👤 Your Telegram user ID is: `{user_id}`\n"
        f"👤 Username: @{username}"
    )
    await update.message.reply_text(message, parse_mode='Markdown')

import logging
import sqlite3
import time
import requests
import random
import string
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
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
ESIM_API_KEY   = os.getenv("ESIM_API_KEY")
ADMIN_IDS      = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

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
  order_no   TEXT
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
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))

def send_qr_code(text: str) -> InputFile:
    qr = qrcode.make(text)
    bio = io.BytesIO()
    bio.name = "qrcode.png"
    qr.save(bio, "PNG")
    bio.seek(0)
    return InputFile(bio, filename="qrcode.png")

def check_tron_payment(memo: str, expected: float) -> float:
    url = f"https://apilist.tronscanapi.com/api/transaction?sort=-timestamp&count=true&limit=20&start=0&address={WALLET_ADDRESS}"
    try:
        data = requests.get(url, timeout=5).json()
        for tx in data.get("data", []):
            if tx.get("data") and memo in tx["data"]:
                amt = float(tx["tokenTransferInfo"]["amount_str"]) / 1e6
                if abs(amt - expected) < 0.01:
                    return amt
    except Exception as e:
        logger.error(f"TRON check error: {e}")
    return 0.0

# === FETCH PACKAGES (REGION → PACKAGE) ===
def fetch_packages(location_code: str = None) -> list[dict]:
    url = "https://api.esimaccess.com/api/v1/open/package/list"
    headers = {"Authorization": ESIM_API_KEY, "Content-Type": "application/json"}
    payload = {"type": "BASE"}
    if location_code:
        payload["locationCode"] = location_code
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        r.raise_for_status()
        return r.json().get("obj", [])
    except Exception as e:
        logger.error(f"Failed to fetch packages: {e}")
        return []

def order_esim_open(memo: str, package_code: str, price_usd: float) -> str | None:
    url = "https://api.esimaccess.com/api/v1/open/esim/order"
    headers = {"Authorization": ESIM_API_KEY, "Content-Type": "application/json"}
    amt_units = int(price_usd * 10000)
    payload = {
        "transactionId": memo,
        "amount":        amt_units,
        "packageInfoList": [{
            "packageCode": package_code,
            "count":       1,
            "price":       amt_units
        }]
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("success"):
            return data["obj"].get("orderNo")
        logger.error(f"Order failed: {data}")
    except Exception as e:
        logger.error(f"Order API error: {e}")
    return None

def query_esim_open(order_no: str) -> list[dict]:
    url = "https://api.esimaccess.com/api/v1/open/esim/query"
    headers = {"Authorization": ESIM_API_KEY, "Content-Type": "application/json"}
    payload = {"orderNo": order_no, "pager": {"pageNum": 1, "pageSize": 20}}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data.get("obj", {}).get("esimList", [])
    except Exception as e:
        logger.error(f"Query API error: {e}")
    return []

# === BOT COMMANDS ===

async def help_cmd(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "\U0001F4D6 *eSIM Bot Help*\n\n"
        "*/start* – Show main menu\n"
        "*/balance* – View your balance\n"
        "*/browse* – Browse available regions\n"
        "*/check* – Check pending payments or orders\n"
        "*/topup <amount>* – Request a top-up (QR + memo)\n"
        "*/topup <user_id> <amount>* – Credit user immediately (admin)\n"
        "*/admin* – Sales stats (admin only)\n",
        parse_mode="Markdown"
    )

async def start(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    menu = [
        [KeyboardButton("📦 Browse"), KeyboardButton("💰 Balance")],
        [KeyboardButton("✅ Check"),  KeyboardButton("📖 Help")]
    ]
    if user_id in ADMIN_IDS:
        menu.append([KeyboardButton("➕ Topup"), KeyboardButton("📊 Admin")])
    await update.message.reply_text(
        "Welcome! Use the menu below:",
        reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True)
    )

async def browse(update: Update, context: CallbackContext):
    pkgs = fetch_packages()
    if not pkgs:
        return await update.message.reply_text("No plans at this time.")
    regions = sorted({p.get("locationCode","GLOBAL") for p in pkgs})
    kb = [[InlineKeyboardButton(r, callback_data=f"REG_{r}")] for r in regions]
    await update.message.reply_text("🌍 Choose a region:", reply_markup=InlineKeyboardMarkup(kb))

async def region_selector(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    region = update.callback_query.data.split("_",1)[1]
    pkgs = fetch_packages(location_code=region)
    if not pkgs:
        return await update.callback_query.message.reply_text(
            f"No plans at this time for region {region}."
        )
    kb = []
    for p in pkgs:
        code = p.get("packageCode") or p.get("slug")
        name = p.get("slug") or code
        price_usd = p.get("price",0) / 10000
        display   = max(price_usd, 5.0)
        kb.append([InlineKeyboardButton(
            f"{name} — ${price_usd:.2f}",
            callback_data=f"PKG_{code}_{display:.2f}"
        )])
    await update.callback_query.message.reply_text(
        f"📡 Packages in {region}:", 
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def pkg_handler(update: Update, context: CallbackContext):
    query    = update.callback_query
    await query.answer()
    _, pkg_code, price_s = query.data.split("_")
    price_usd = float(price_s)
    user_id   = query.from_user.id

    # enforce minimum
    if price_usd < 5.0:
        await query.message.reply_text("⚠️ Minimum 5 USDT; extra will be credited.")
        price_usd = 5.0

    # check balance
    c.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,))
    row = c.fetchone(); bal = row[0] if row else 0.0

    if bal < price_usd:
        # request top-up
        memo = generate_memo()
        uname = query.from_user.username or str(user_id)
        c.execute(
            "INSERT INTO orders (user_id,username,amount,memo,plan_id) "
            "VALUES (?,?,?,?,?)",
            (user_id, uname, price_usd, memo, pkg_code)
        )
        conn.commit()
        txt = (
            f"🔋 Top-up required\nSend *{price_usd:.2f} USDT* to:\n"
            f"`{WALLET_ADDRESS}`\nMemo: `{memo}`"
        )
        return await query.message.reply_photo(
            photo=send_qr_code(txt),
            caption=txt,
            parse_mode="Markdown"
        )

    # deduct balance & place order
    new_bal = bal - price_usd
    c.execute("UPDATE balances SET balance=? WHERE user_id=?", (new_bal, user_id))
    conn.commit()

    memo     = generate_memo()
    order_no = order_esim_open(memo, pkg_code, price_usd)
    if not order_no:
        return await query.message.reply_text("❌ Order failed—please try again.")

    c.execute("""
        UPDATE orders SET memo=?, order_no=?, paid=1 
        WHERE user_id=? AND plan_id=? AND paid=0
    """, (memo, order_no, user_id, pkg_code))
    conn.commit()

    # poll for up to ~30s
    profiles = []
    for _ in range(15):
        profiles = query_esim_open(order_no)
        if profiles:
            break
        time.sleep(2)

    if not profiles:
        return await query.message.reply_text(
            f"✔️ Order {order_no} placed, but profiles not ready yet. Try /check."
        )

    # send each profile
    for p in profiles:
        qr = p.get("qrCodeUrl") or p.get("ac")
        await query.message.reply_photo(photo=send_qr_code(qr), caption=qr)

async def check(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    c.execute("""
        SELECT order_no FROM orders 
        WHERE user_id=? AND paid=1 AND order_no IS NOT NULL 
        ORDER BY id DESC LIMIT 1
    """, (user_id,))
    row = c.fetchone()
    if not row:
        return await update.message.reply_text("No recent orders to check.")
    order_no = row[0]
    profiles = query_esim_open(order_no)
    if not profiles:
        return await update.message.reply_text("⏳ Still allocating, try again later.")
    for p in profiles:
        qr = p.get("qrCodeUrl") or p.get("ac")
        await update.message.reply_photo(photo=send_qr_code(qr), caption=qr)

async def balance(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    c.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,))
    row = c.fetchone(); bal = row[0] if row else 0.0
    await update.message.reply_text(f"💰 Balance: {bal:.2f} USDT\n👤 Your ID: {user_id}")

async def topup(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    args    = context.args
    # Admin credit
    if user_id in ADMIN_IDS and len(args)==2:
        tgt, amt = args[0], float(args[1])
        c.execute("""
            INSERT INTO balances(user_id,balance) VALUES(?,?) 
            ON CONFLICT(user_id) DO UPDATE SET balance=balance+?
        """, (tgt, amt, amt))
        conn.commit()
        return await update.message.reply_text(f"✅ Credited {amt:.2f} USDT to user {tgt}.")
    # User top-up request
    if len(args)==1:
        amt   = float(args[0])
        memo  = generate_memo()
        uname = update.message.from_user.username or str(user_id)
        c.execute("""
            INSERT INTO orders (user_id,username,amount,memo,plan_id) 
            VALUES (?,?,?,?,?)
        """, (user_id, uname, amt, memo, "TOPUP"))
        conn.commit()
        txt = (
            f"🔋 Top-Up Request\nSend *{amt:.2f} USDT* to:\n"
            f"`{WALLET_ADDRESS}`\nMemo: `{memo}`"
        )
        return await update.message.reply_photo(
            photo=send_qr_code(txt), caption=txt, parse_mode="Markdown"
        )
    # Fallback usage
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
    txt, uid = update.message.text, update.message.from_user.id
    if txt == "📦 Browse":     return await browse(update, context)
    if txt == "💰 Balance":    return await balance(update, context)
    if txt == "✅ Check":      return await check(update, context)
    if txt == "📖 Help":       return await help_cmd(update, context)
    if txt == "➕ Topup" and uid in ADMIN_IDS:
        return await update.message.reply_text("Use `/topup <user_id> <amount>`", parse_mode="Markdown")
    if txt == "📊 Admin" and uid in ADMIN_IDS:
        return await admin(update, context)
    await update.message.reply_text("Unknown option. Use /help.")

# === SETUP & RUN ===
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# Slash commands
app.add_handler(CommandHandler("start",   start))
app.add_handler(CommandHandler("help",    help_cmd))
app.add_handler(CommandHandler("balance", balance))
app.add_handler(CommandHandler("browse",  browse))
app.add_handler(CommandHandler("check",   check))
app.add_handler(CommandHandler("topup",   topup))
app.add_handler(CommandHandler("admin",   admin))

# Callback buttons
app.add_handler(CallbackQueryHandler(region_selector, pattern="^REG_"))
app.add_handler(CallbackQueryHandler(pkg_handler,      pattern="^PKG_"))

# Main-menu text
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu))

if __name__ == "__main__":
    print("Bot is starting…")
    app.run_polling()

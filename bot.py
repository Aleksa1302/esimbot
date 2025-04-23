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
ESIM_API_KEY   = os.getenv("ESIM_API_KEY")  # RT-AccessCode
ADMIN_IDS      = list(map(int, os.getenv("ADMIN_IDS","").split(",")))

# === DB SETUP ===
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

# === COMMON HEADERS ===
COMMON_HEADERS = {
    "RT-AccessCode": ESIM_API_KEY,
    "Content-Type":  "application/json"
}

# === UTILITIES ===
def generate_memo() -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))

def send_qr_code(text: str) -> InputFile:
    qr = qrcode.make(text)
    bio = io.BytesIO(); bio.name="qrcode.png"
    qr.save(bio,"PNG"); bio.seek(0)
    return InputFile(bio, filename="qrcode.png")

# === OPEN API WRAPPERS ===

def fetch_packages(location_code: str=None) -> list[dict]:
    """
    GET All Data Packages (BASE).
    Returns list of package dicts.
    """
    url     = "https://api.esimaccess.com/api/v1/open/package/list"
    payload = {"type":"BASE"}
    if location_code:
        payload["locationCode"] = location_code

    try:
        r = requests.post(url, headers=COMMON_HEADERS, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        logger.info(f"package-list raw response: {data}")

        if not data.get("success", False):
            return []
        obj = data.get("obj", {})
        if isinstance(obj, list):
            return obj
        for key in ("packageList","list","data","packages"):
            if isinstance(obj, dict) and key in obj and isinstance(obj[key], list):
                return obj[key]
        if isinstance(obj, dict) and "packageCode" in obj and "price" in obj:
            return [obj]
    except Exception as e:
        logger.error(f"fetch_packages error: {e}")
    return []

def fetch_topup_packages(package_code: str=None, slug: str=None, iccid: str=None) -> list[dict]:
    """
    Same endpoint, with type=TOPUP and optional filters.
    """
    url     = "https://api.esimaccess.com/api/v1/open/package/list"
    payload = {"type":"TOPUP"}
    if package_code: payload["packageCode"] = package_code
    if slug:         payload["slug"]        = slug
    if iccid:        payload["iccid"]       = iccid

    try:
        r = requests.post(url, headers=COMMON_HEADERS, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        logger.info(f"topup-list raw response: {data}")

        if not data.get("success", False):
            return []
        obj = data.get("obj", {})
        if isinstance(obj, list):
            return obj
        for key in ("packageList","list","data","packages"):
            if isinstance(obj, dict) and key in obj and isinstance(obj[key], list):
                return obj[key]
        if isinstance(obj, dict) and "packageCode" in obj and "price" in obj:
            return [obj]
    except Exception as e:
        logger.error(f"fetch_topup_packages error: {e}")
    return []

def order_esim_open(memo: str, pkg_code: str, price_usd: float) -> str|None:
    """
    POST /open/esim/order
    """
    url       = "https://api.esimaccess.com/api/v1/open/esim/order"
    amt_units = int(price_usd * 10000)
    payload   = {
        "transactionId": memo,
        "amount":        amt_units,
        "packageInfoList": [{
            "packageCode": pkg_code,
            "count":       1,
            "price":       amt_units
        }]
    }
    try:
        r = requests.post(url, headers=COMMON_HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        d = r.json()
        if d.get("success"):
            return d["obj"].get("orderNo")
        logger.error(f"order_esim_open failed: {d}")
    except Exception as e:
        logger.error(f"order_esim_open error: {e}")
    return None

def query_esim_open(order_no: str=None, iccid: str=None) -> list[dict]:
    """
    POST /open/esim/query by orderNo or iccid.
    """
    url     = "https://api.esimaccess.com/api/v1/open/esim/query"
    payload = {"pager":{"pageNum":1,"pageSize":20}}
    if order_no: payload["orderNo"] = order_no
    if iccid:    payload["iccid"]    = iccid

    try:
        r = requests.post(url, headers=COMMON_HEADERS, json=payload, timeout=10)
        r.raise_for_status()
        return r.json().get("obj",{}).get("esimList", [])
    except Exception as e:
        logger.error(f"query_esim_open error: {e}")
    return []

# === BOT COMMANDS ===

async def help_cmd(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "\U0001F4D6 *eSIM Bot Help*\n\n"
        "*/start* – Main menu\n"
        "*/balance* – Your USDT balance\n"
        "*/browse* – Browse base plans by country\n"
        "*/topuplans [code]* – Browse TOPUP plans for a package\n"
        "*/queryorder <orderNo>* – Fetch your order profiles\n"
        "*/queryiccid <iccid>*   – Fetch by ICCID\n"
        "*/check* – Check your last order\n"
        "*/topup <amount>* – Request a top-up (QR + memo)\n"
        "*/topup <user_id> <amt>* – Credit user (admin)\n"
        "*/admin* – Sales stats (admin only)\n",
        parse_mode="Markdown"
    )

async def start(update: Update, context: CallbackContext):
    uid = update.effective_user.id
    kb = [
        [KeyboardButton("📦 Browse"), KeyboardButton("💰 Balance")],
        [KeyboardButton("⭮ TopUpPlans"), KeyboardButton("✅ Check")],
        [KeyboardButton("📖 Help")]
    ]
    if uid in ADMIN_IDS:
        kb[1].append(KeyboardButton("➕ Topup"))
        kb[1].append(KeyboardButton("📊 Admin"))
    menu = ReplyKeyboardMarkup(kb, resize_keyboard=True)
    await update.message.reply_text("Welcome! Use the menu below:", reply_markup=menu)

async def browse(update: Update, context: CallbackContext):
    pkgs = fetch_packages()
    if not pkgs:
        return await update.message.reply_text("No plans at this time.")
    # collect all country codes from each pkg's 'location' field
    codes = set()
    for p in pkgs:
        loc = p.get("location","")
        for cc in loc.split(","):
            cc = cc.strip()
            if cc:
                codes.add(cc)
    kb = [[InlineKeyboardButton(cc, callback_data=f"REG_{cc}")] for cc in sorted(codes)]
    await update.message.reply_text("🌍 Choose a country code:", reply_markup=InlineKeyboardMarkup(kb))

async def region_selector(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    country = update.callback_query.data.split("_",1)[1]
    pkgs    = fetch_packages(location_code=country)
    if not pkgs:
        return await update.callback_query.message.reply_text(f"No plans in {country}.")
    kb = []
    for p in pkgs:
        code      = p.get("packageCode") or p.get("slug")
        price_usd = p.get("price",0)/10000
        disp      = max(price_usd, 5.0)
        kb.append([InlineKeyboardButton(
            f"{code} — ${price_usd:.2f}",
            callback_data=f"PKG_{code}_{disp:.2f}"
        )])
    await update.callback_query.message.reply_text(
        f"📡 Plans for {country}:", 
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def pkg_handler(update: Update, context: CallbackContext):
    q   = update.callback_query; await q.answer()
    _, code, price_s = q.data.split("_")
    price_usd        = float(price_s)
    uid              = q.from_user.id

    if price_usd < 5.0:
        await q.message.reply_text("⚠️ Minimum 5 USDT required; extra credited.")
        price_usd = 5.0

    c.execute("SELECT balance FROM balances WHERE user_id=?", (uid,))
    row = c.fetchone(); bal = row[0] if row else 0.0

    if bal < price_usd:
        memo  = generate_memo()
        uname = q.from_user.username or str(uid)
        c.execute(
            "INSERT INTO orders (user_id,username,amount,memo,plan_id) VALUES(?,?,?,?,?)",
            (uid, uname, price_usd, memo, code)
        )
        conn.commit()
        txt = (
            f"🔋 Top-up required\nSend *{price_usd:.2f} USDT* to `{WALLET_ADDRESS}`\n"
            f"Memo: `{memo}`"
        )
        return await q.message.reply_photo(photo=send_qr_code(txt),
                                           caption=txt, parse_mode="Markdown")

    # Deduct & order
    new_bal = bal - price_usd
    c.execute("UPDATE balances SET balance=? WHERE user_id=?", (new_bal, uid))
    conn.commit()

    memo     = generate_memo()
    order_no = order_esim_open(memo, code, price_usd)
    if not order_no:
        return await q.message.reply_text("❌ Order failed—please retry.")

    c.execute("""
      UPDATE orders SET memo=?,order_no=?,paid=1 
      WHERE user_id=? AND plan_id=? AND paid=0
    """, (memo, order_no, uid, code))
    conn.commit()

    # poll for profiles (≈30 s)
    profiles=[]
    for _ in range(15):
        profiles = query_esim_open(order_no=order_no)
        if profiles: break
        time.sleep(2)

    if not profiles:
        return await q.message.reply_text(
            f"✔️ Order {order_no} placed; profiles pending. Use /check."
        )

    for p in profiles:
        qr = p.get("qrCodeUrl") or p.get("ac")
        await q.message.reply_photo(photo=send_qr_code(qr), caption=qr)

async def topuplans_cmd(update: Update, context: CallbackContext):
    args = context.args
    if args:
        code = args[0]
    else:
        # fallback to last paid plan_id
        uid = update.message.from_user.id
        c.execute("SELECT plan_id FROM orders WHERE user_id=? AND paid=1 ORDER BY id DESC LIMIT 1", (uid,))
        row = c.fetchone()
        if not row:
            return await update.message.reply_text("No recent plan—please pass a packageCode.")
        code = row[0]

    pkgs = fetch_topup_packages(package_code=code)
    if not pkgs:
        return await update.message.reply_text(f"No top-up plans for {code}.")
    kb = []
    for p in pkgs:
        tp   = p.get("packageCode") or p.get("slug")
        price_usd = p.get("price",0)/10000
        disp      = max(price_usd, 5.0)
        kb.append([InlineKeyboardButton(
            f"{tp} — ${price_usd:.2f}", 
            callback_data=f"TPUP_{tp}_{disp:.2f}"
        )])
    await update.message.reply_text(
        f"🔄 Top-up plans for {code}:", 
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def topup_plan_handler(update: Update, context: CallbackContext):
    q = update.callback_query; await q.answer()
    _, code, price_s = q.data.split("_")
    price_usd        = float(price_s)
    uid              = q.from_user.id

    if price_usd < 5.0:
        await q.message.reply_text("⚠️ Minimum 5 USDT; extra credited.")
        price_usd = 5.0

    c.execute("SELECT balance FROM balances WHERE user_id=?", (uid,))
    row = c.fetchone(); bal = row[0] if row else 0.0
    if bal < price_usd:
        memo  = generate_memo()
        uname = q.from_user.username or str(uid)
        c.execute(
            "INSERT INTO orders (user_id,username,amount,memo,plan_id) VALUES(?,?,?,?,?)",
            (uid, uname, price_usd, memo, code)
        )
        conn.commit()
        txt = (
            f"🔋 Top-up required\nSend *{price_usd:.2f} USDT* to `{WALLET_ADDRESS}`\n"
            f"Memo: `{memo}`"
        )
        return await q.message.reply_photo(photo=send_qr_code(txt),
                                           caption=txt, parse_mode="Markdown")

    new_bal = bal - price_usd
    c.execute("UPDATE balances SET balance=? WHERE user_id=?", (new_bal, uid))
    conn.commit()

    memo     = generate_memo()
    order_no = order_esim_open(memo, code, price_usd)
    if not order_no:
        return await q.message.reply_text("❌ Top-up order failed.")

    c.execute("""
      UPDATE orders SET memo=?,order_no=?,paid=1 
      WHERE user_id=? AND plan_id=? AND paid=0
    """, (memo, order_no, uid, code))
    conn.commit()

    profiles=[]
    for _ in range(15):
        profiles = query_esim_open(order_no=order_no)
        if profiles: break
        time.sleep(2)

    if not profiles:
        return await q.message.reply_text(f"✔️ Top-up {order_no} placed; profiles pending.")
    for p in profiles:
        qr = p.get("qrCodeUrl") or p.get("ac")
        await q.message.reply_photo(photo=send_qr_code(qr), caption=qr)

async def query_order_cmd(update: Update, context: CallbackContext):
    args = context.args
    if len(args)!=1:
        return await update.message.reply_text("Usage: /queryorder <orderNo>")
    profiles = query_esim_open(order_no=args[0])
    if not profiles:
        return await update.message.reply_text(f"No profiles for order {args[0]} yet.")
    for p in profiles:
        qr = p.get("qrCodeUrl") or p.get("ac")
        await update.message.reply_photo(photo=send_qr_code(qr), caption=qr)

async def query_iccid_cmd(update: Update, context: CallbackContext):
    args = context.args
    if len(args)!=1:
        return await update.message.reply_text("Usage: /queryiccid <iccid>")
    profiles = query_esim_open(iccid=args[0])
    if not profiles:
        return await update.message.reply_text(f"No profiles for ICCID {args[0]}.")
    for p in profiles:
        qr = p.get("qrCodeUrl") or p.get("ac")
        await update.message.reply_photo(photo=send_qr_code(qr), caption=qr)

async def check(update: Update, context: CallbackContext):
    uid = update.message.from_user.id
    c.execute("SELECT order_no FROM orders WHERE user_id=? AND paid=1 AND order_no IS NOT NULL ORDER BY id DESC LIMIT 1", (uid,))
    row = c.fetchone()
    if not row:
        return await update.message.reply_text("No recent orders to check.")
    profiles = query_esim_open(order_no=row[0])
    if not profiles:
        return await update.message.reply_text("⏳ Still allocating; try again later.")
    for p in profiles:
        qr = p.get("qrCodeUrl") or p.get("ac")
        await update.message.reply_photo(photo=send_qr_code(qr), caption=qr)

async def balance(update: Update, context: CallbackContext):
    uid = update.message.from_user.id
    c.execute("SELECT balance FROM balances WHERE user_id=?", (uid,))
    row = c.fetchone(); bal = row[0] if row else 0.0
    await update.message.reply_text(f"💰 Balance: {bal:.2f} USDT\n👤 Your ID: {uid}")

async def topup(update: Update, context: CallbackContext):
    uid  = update.message.from_user.id
    args = context.args
    if uid in ADMIN_IDS and len(args)==2:
        tgt, amt = args[0], float(args[1])
        c.execute(
            "INSERT INTO balances(user_id,balance) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET balance=balance+?", 
            (tgt, amt, amt)
        )
        conn.commit()
        return await update.message.reply_text(f"✅ Credited {amt:.2f} USDT to {tgt}.")
    if len(args)==1:
        amt  = float(args[0])
        memo = generate_memo()
        uname=update.message.from_user.username or str(uid)
        c.execute(
            "INSERT INTO orders (user_id,username,amount,memo,plan_id) VALUES(?,?,?,?,?)",
            (uid, uname, amt, memo, "TOPUP")
        )
        conn.commit()
        txt = (
            f"🔋 Top-Up Request\nSend *{amt:.2f} USDT* to `{WALLET_ADDRESS}`\n"
            f"Memo: `{memo}`"
        )
        return await update.message.reply_photo(photo=send_qr_code(txt),
                                                caption=txt, parse_mode="Markdown")
    usage = "Usage: /topup <amount>" if uid not in ADMIN_IDS else "Usage: /topup <user_id> <amount>"
    await update.message.reply_text(usage)

async def admin(update: Update, context: CallbackContext):
    uid = update.message.from_user.id
    if uid not in ADMIN_IDS:
        return await update.message.reply_text("Unauthorized.")
    c.execute("SELECT COUNT(*), SUM(amount) FROM orders WHERE paid=1")
    sold, rev = c.fetchone()
    c.execute("SELECT COUNT(DISTINCT user_id) FROM orders")
    users = c.fetchone()[0]
    await update.message.reply_text(
        f"📊 Sales Report:\n- Sold: {sold}\n- Revenue: ${rev or 0:.2f}\n- Active users: {users}"
    )

async def handle_main_menu(update: Update, context: CallbackContext):
    txt, uid = update.message.text, update.message.from_user.id
    if txt=="📦 Browse":      return await browse(update, context)
    if txt=="⭮ TopUpPlans":   return await topuplans_cmd(update, context)
    if txt=="💰 Balance":     return await balance(update, context)
    if txt=="✅ Check":       return await check(update, context)
    if txt=="📖 Help":        return await help_cmd(update, context)
    if txt=="➕ Topup" and uid in ADMIN_IDS:
        return await update.message.reply_text("Use `/topup <user_id> <amount>`", parse_mode="Markdown")
    if txt=="📊 Admin" and uid in ADMIN_IDS:
        return await admin(update, context)
    await update.message.reply_text("Unknown option. Use /help.")

# === SETUP & RUN ===
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# commands
app.add_handler(CommandHandler("start",      start))
app.add_handler(CommandHandler("help",       help_cmd))
app.add_handler(CommandHandler("balance",    balance))
app.add_handler(CommandHandler("browse",     browse))
app.add_handler(CommandHandler("topuplans",  topuplans_cmd))
app.add_handler(CommandHandler("queryorder", query_order_cmd))
app.add_handler(CommandHandler("queryiccid", query_iccid_cmd))
app.add_handler(CommandHandler("check",      check))
app.add_handler(CommandHandler("topup",      topup))
app.add_handler(CommandHandler("admin",      admin))

# callbacks
app.add_handler(CallbackQueryHandler(region_selector,    pattern="^REG_"))
app.add_handler(CallbackQueryHandler(pkg_handler,        pattern="^PKG_"))
app.add_handler(CallbackQueryHandler(topup_plan_handler, pattern="^TPUP_"))

# main‐menu text
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu))

if __name__ == "__main__":
    print("Bot starting…")
    app.run_polling()

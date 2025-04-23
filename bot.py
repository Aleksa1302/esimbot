import logging
import sqlite3
import time
import functools
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

# 3rd-party for continent lookups
import pycountry_convert as pc

# === CONFIG ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
ESIM_API_KEY   = os.getenv("ESIM_API_KEY")  # RT-AccessCode
ADMIN_IDS      = list(map(int, os.getenv("ADMIN_IDS","").split(",")))

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

# === TTL CACHE DECORATOR ===
def ttl_cache(ttl_seconds: int):
    """Caches function results for ttl_seconds."""
    def decorator(fn):
        cache = {}
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            entry = cache.get(key)
            now = time.time()
            if entry and now - entry[0] < ttl_seconds:
                return entry[1]
            result = fn(*args, **kwargs)
            cache[key] = (now, result)
            return result
        wrapped.clear_cache = lambda: cache.clear()
        return wrapped
    return decorator

# === CONTINENT MAPPING ===
def country_to_continent(alpha2: str) -> str:
    """Convert ISO2 → continent name, fallback 'Other'."""
    try:
        code = pc.country_alpha2_to_continent_code(alpha2)
        return pc.convert_continent_code_to_continent_name(code)
    except Exception:
        return "Other"

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

def check_tron_payment(memo: str, expected: float) -> float:
    url = (
        f"https://apilist.tronscanapi.com/api/transaction"
        f"?sort=-timestamp&count=true&limit=20&start=0&address={WALLET_ADDRESS}"
    )
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

# === OPEN API WRAPPERS ===

@ttl_cache(ttl_seconds=3600)
def fetch_packages(location_code: str=None) -> list[dict]:
    """List BASE packages, cached per country for 1 h."""
    url     = "https://api.esimaccess.com/api/v1/open/package/list"
    payload = {"type":"BASE"}
    if location_code:
        payload["locationCode"] = location_code
    try:
        r = requests.post(url, headers=COMMON_HEADERS, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        logger.info("BASE-list raw response: %s", data)
        if not data.get("success", False):
            return []
        obj = data.get("obj", {})
        if isinstance(obj, list):
            return obj
        for k in ("packageList","list","data","packages"):
            if isinstance(obj, dict) and k in obj and isinstance(obj[k], list):
                return obj[k]
        if isinstance(obj, dict) and "packageCode" in obj:
            return [obj]
    except Exception as e:
        logger.error(f"fetch_packages error: {e}")
    return []

@ttl_cache(ttl_seconds=3600)
def fetch_topup_packages(package_code: str=None,
                         slug: str=None,
                         iccid: str=None) -> list[dict]:
    """List TOPUP packages, cached per params for 1 h."""
    url     = "https://api.esimaccess.com/api/v1/open/package/list"
    payload = {"type":"TOPUP"}
    if package_code: payload["packageCode"] = package_code
    if slug:         payload["slug"]        = slug
    if iccid:        payload["iccid"]       = iccid
    try:
        r = requests.post(url, headers=COMMON_HEADERS, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        logger.info("TOPUP-list raw response: %s", data)
        if not data.get("success", False):
            return []
        obj = data.get("obj", {})
        if isinstance(obj, list):
            return obj
        for k in ("packageList","list","data","packages"):
            if isinstance(obj, dict) and k in obj and isinstance(obj[k], list):
                return obj[k]
        if isinstance(obj, dict) and "packageCode" in obj:
            return [obj]
    except Exception as e:
        logger.error(f"fetch_topup_packages error: {e}")
    return []

def order_esim_open(memo: str, pkg_code: str, price_usd: float) -> str|None:
    """Place an order, returns orderNo."""
    url       = "https://api.esimaccess.com/api/v1/open/esim/order"
    amt_units = int(price_usd * 10000)
    payload   = {
        "transactionId": memo,
        "amount":        amt_units,
        "packageInfoList":[{"packageCode":pkg_code,"count":1,"price":amt_units}]
    }
    try:
        r = requests.post(url, headers=COMMON_HEADERS, json=payload, timeout=15)
        r.raise_for_status()
        d = r.json()
        if d.get("success"):
            return d["obj"].get("orderNo")
        logger.error("order_esim_open failed: %s", d)
    except Exception as e:
        logger.error(f"order_esim_open error: {e}")
    return None

def query_esim_open(order_no: str=None, iccid: str=None) -> list[dict]:
    """Query allocated profiles by orderNo or ICCID."""
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
        "*/browse* – Browse BASE plans (continent→country→plan)\n"
        "*/topuplans [code]* – Browse TOPUP plans for a package\n"
        "*/queryorder <orderNo>* – Fetch profiles by orderNo\n"
        "*/queryiccid <iccid>* – Fetch profiles by ICCID\n"
        "*/check* – Check last order status\n"
        "*/topup <amount>* – Request a USDT top-up\n"
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
    await update.message.reply_text(
        "Welcome! Use the menu below:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

# — Level 1: show continents —
async def browse(update: Update, context: CallbackContext):
    pkgs = fetch_packages()
    if not pkgs:
        return await update.message.reply_text("No plans at this time.")

    # collect all country codes
    codes = set()
    for p in pkgs:
        for loc in p.get("locationNetworkList", []):
            codes.add(loc["locationCode"])

    # group by continent
    continents = {}
    for cc in codes:
        cont = country_to_continent(cc)
        continents.setdefault(cont, []).append(cc)

    buttons = [
        [InlineKeyboardButton(cont, callback_data=f"CONT_{cont}")]
        for cont in sorted(continents)
    ]
    await update.message.reply_text(
        "🌍 Choose a continent:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# — Level 2: after continent, show countries —
async def continent_selector(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    cont = update.callback_query.data.split("_",1)[1]
    pkgs = fetch_packages()
    # rebuild codes for this continent
    code_to_name = {}
    for p in pkgs:
        for loc in p.get("locationNetworkList", []):
            cc = loc["locationCode"]
            if country_to_continent(cc) == cont:
                code_to_name[cc] = loc["locationName"]

    if not code_to_name:
        return await update.callback_query.message.reply_text(f"No countries found in {cont}.")

    buttons = [
        [InlineKeyboardButton(name, callback_data=f"REG_{cc}")]
        for cc, name in sorted(code_to_name.items(), key=lambda x:x[1])
    ]
    await update.callback_query.message.reply_text(
        f"🌎 Countries in {cont}:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# — Level 3: after country, show plans —
async def country_selector(update: Update, context: CallbackContext):
    await update.callback_query.answer()
    country = update.callback_query.data.split("_",1)[1]
    pkgs    = fetch_packages(location_code=country)
    if not pkgs:
        return await update.callback_query.message.reply_text(f"No plans in {country}.")

    buttons = []
    for p in pkgs:
        code      = p.get("packageCode") or p.get("slug")
        price_usd = p.get("price",0)/10000
        vol_GB    = p.get("volume",0)/(1024**3)
        dur       = p.get("duration",0)
        unit      = p.get("durationUnit","DAY")
        label     = f"{code}: {vol_GB:.1f} GB · {dur}{unit} — ${price_usd:.2f}"
        disp      = max(price_usd, 5.0)
        buttons.append([InlineKeyboardButton(label, callback_data=f"PKG_{code}_{disp:.2f}")])

    await update.callback_query.message.reply_text(
        f"📡 Plans for {country}:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# handle plan‐selection exactly as before…
async def pkg_handler(update: Update, context: CallbackContext):
    # (copy-paste your existing pkg_handler logic)
    query    = update.callback_query
    await query.answer()
    _, code, price_s = query.data.split("_")
    price_usd        = float(price_s)
    user_id          = query.from_user.id

    if price_usd < 5.0:
        await query.message.reply_text("⚠️ Minimum 5 USDT; extra credited.")
        price_usd = 5.0

    c.execute("SELECT balance FROM balances WHERE user_id=?", (user_id,))
    row = c.fetchone(); bal = row[0] if row else 0.0

    if bal < price_usd:
        memo  = generate_memo()
        uname = query.from_user.username or str(user_id)
        c.execute(
            "INSERT INTO orders (user_id,username,amount,memo,plan_id) VALUES(?,?,?,?,?)",
            (user_id, uname, price_usd, memo, code)
        )
        conn.commit()
        txt = (
            f"🔋 Top-up required\nSend *{price_usd:.2f} USDT* to `{WALLET_ADDRESS}`\n"
            f"Memo: `{memo}`"
        )
        return await query.message.reply_photo(
            photo=send_qr_code(txt),
            caption=txt,
            parse_mode="Markdown"
        )

    # deduct & order…
    new_bal = bal - price_usd
    c.execute("UPDATE balances SET balance=? WHERE user_id=?", (new_bal, user_id))
    conn.commit()

    memo     = generate_memo()
    order_no = order_esim_open(memo, code, price_usd)
    if not order_no:
        return await query.message.reply_text("❌ Order failed—please retry.")

    c.execute("""
      UPDATE orders SET memo=?,order_no=?,paid=1
      WHERE user_id=? AND plan_id=? AND paid=0
    """, (memo, order_no, user_id, code))
    conn.commit()

    profiles = []
    for _ in range(15):
        profiles = query_esim_open(order_no=order_no)
        if profiles:
            break
        time.sleep(2)

    if not profiles:
        return await query.message.reply_text(
            f"✔️ Order {order_no} placed; profiles pending. Use /check."
        )
    for p in profiles:
        qr = p.get("qrCodeUrl") or p.get("ac")
        await query.message.reply_photo(photo=send_qr_code(qr), caption=qr)

# …and the rest of your commands (topuplans, queryorder, queryiccid, check, balance, topup, admin, handling main-menu)…  
# …unchanged from earlier snippet…

# === SETUP & RUN ===
app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# slash commands
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

# inline callbacks for 3‐level menu
app.add_handler(CallbackQueryHandler(continent_selector, pattern="^CONT_"))
app.add_handler(CallbackQueryHandler(country_selector,   pattern="^REG_"))
app.add_handler(CallbackQueryHandler(pkg_handler,        pattern="^PKG_"))

# fallback text menu
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu))

if __name__ == "__main__":
    print("Bot is starting…")
    app.run_polling()

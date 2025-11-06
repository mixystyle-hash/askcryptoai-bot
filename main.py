import os, time, sqlite3
from dotenv import load_dotenv
from telegram import Update, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import requests

load_dotenv()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")

FREE_LIMIT = int(os.environ.get("FREE_LIMIT", 10))
PRO_PACK_SIZE = int(os.environ.get("PRO_PACK_SIZE", 100))
PRO_PRICE_USD = float(os.environ.get("PRO_PRICE_USD", 2.50))
PREMIUM_PRICE_USD = float(os.environ.get("PREMIUM_PRICE_USD", 7.00))
PREMIUM_DAYS = int(os.environ.get("PREMIUM_DAYS", 30))
PREMIUM_FAIRUSE_DAILY = int(os.environ.get("PREMIUM_FAIRUSE_DAILY", 300))

FREE_MODEL = os.environ.get("FREE_MODEL", "gpt-4o-mini")
PREMIUM_MODEL = os.environ.get("PREMIUM_MODEL", "gpt-4o")

DB_PATH = os.environ.get("DB_PATH", "bot.db")
ADMIN_ID = int(os.environ.get("ADMIN_USER_ID", 0))
COOLDOWN_FREE_MS = int(os.environ.get("COOLDOWN_FREE_MS", 2000))
COOLDOWN_PREMIUM_MS = int(os.environ.get("COOLDOWN_PREMIUM_MS", 500))

REF_BONUS_PER_FRIEND = int(os.environ.get("REF_BONUS_PER_FRIEND", 2))
REF_MAX_FRIENDS = int(os.environ.get("REF_MAX_FRIENDS", 500))

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    daily_count INTEGER NOT NULL DEFAULT 0,
    premium_until INTEGER NOT NULL DEFAULT 0,
    balance INTEGER NOT NULL DEFAULT 0,
    ref_code TEXT NOT NULL DEFAULT '',
    ref_count INTEGER NOT NULL DEFAULT 0,
    last_msg_ts INTEGER NOT NULL DEFAULT 0
);
""")
conn.execute("""
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    payload TEXT,
    amount_xtr INTEGER,
    ts INTEGER
);
""")
conn.execute("""
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inviter_id INTEGER,
    invited_id INTEGER,
    ts INTEGER,
    UNIQUE(inviter_id, invited_id)
);
""")
conn.commit()

def today_str(): return time.strftime("%Y-%m-%d")
def _mk_ref_code(uid:int)->str: return str(uid)

def get_user(uid:int):
    row = conn.execute("SELECT day,daily_count,premium_until,balance,ref_code,ref_count,last_msg_ts FROM users WHERE user_id=?",(uid,)).fetchone()
    if not row:
        code = _mk_ref_code(uid)
        conn.execute("INSERT INTO users(user_id,day,daily_count,premium_until,balance,ref_code,ref_count,last_msg_ts) VALUES(?,?,?,?,?,?,?,?)",
                     (uid, today_str(), 0, 0, 0, code, 0, 0))
        conn.commit()
        row = (today_str(), 0, 0, 0, code, 0, 0)
    day, daily_count, premium_until, balance, ref_code, ref_count, last_ts = row
    if day != today_str():
        daily_count = 0
        day = today_str()
        conn.execute("UPDATE users SET day=?, daily_count=? WHERE user_id=?", (day, daily_count, uid))
        conn.commit()
    return {"day":day,"daily_count":daily_count,"premium_until":premium_until,"balance":balance,"ref_code":ref_code,"ref_count":ref_count,"last_msg_ts":last_ts}

def set_user(uid:int, **kwargs):
    if not kwargs: return
    fields=[]; values=[]
    for k,v in kwargs.items():
        fields.append(f"{k}=?"); values.append(v)
    values.append(uid)
    conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id=?", tuple(values))
    conn.commit()

def is_premium(u:dict)->bool: return u["premium_until"] > int(__import__("time").time())
def stars_from_usd(usd:float)->int: return int(round(usd * 100))

async def handle_start_referral(uid:int, start_param:str, ctx:ContextTypes.DEFAULT_TYPE):
    if not start_param or not start_param.startswith("ref"): return
    try:
        inviter_id = int(start_param.replace("ref","").strip())
        if inviter_id == uid: return
        get_user(inviter_id)
        exists = conn.execute("SELECT 1 FROM referrals WHERE inviter_id=? AND invited_id=?", (inviter_id, uid)).fetchone()
        if exists: return
        inv_row = conn.execute("SELECT ref_count FROM users WHERE user_id=?", (inviter_id,)).fetchone()
        if not inv_row: return
        current = inv_row[0] or 0
        if current >= REF_MAX_FRIENDS: return
        conn.execute("INSERT OR IGNORE INTO referrals(inviter_id, invited_id, ts) VALUES(?,?,?)", (inviter_id, uid, int(__import__("time").time())))
        new_count = min(current + 1, REF_MAX_FRIENDS)
        conn.execute("UPDATE users SET ref_count=? , balance=balance+? WHERE user_id=?", (new_count, REF_BONUS_PER_FRIEND, inviter_id))
        conn.commit()
        try:
            await ctx.bot.send_message(inviter_id, f"🎉 New friend joined via your link! +{REF_BONUS_PER_FRIEND} answers added.")
        except Exception: pass
    except Exception: pass

def call_openai(prompt:str, premium:bool)->str:
    key = OPENAI_KEY
    if not key: return "OpenAI key is not set. Add OPENAI_API_KEY in your .env."
    model = PREMIUM_MODEL if premium else FREE_MODEL
    max_tokens = 1000 if premium else 500
    headers = {"Authorization": f"Bearer {key}"}
    body = {"model": model,
            "messages":[
                {"role":"system","content":"You are a crypto Q&A assistant. Be concise, no financial advice. Always add: 'This is not financial advice.'"},
                {"role":"user","content": prompt}
            ],
            "max_tokens": max_tokens, "temperature": 0.2}
    for attempt in range(3):
        try:
            r = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=30)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 429 and attempt < 2:
                __import__("time").sleep(2 ** (attempt + 1)); continue
            raise
    return "AI is currently overloaded. Try again in a minute. This is not financial advice."

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    start_param = None
    if update.message and update.message.text:
        parts = update.message.text.split(" ",1)
        if len(parts)==2: start_param = parts[1].strip()
    get_user(uid)
    await handle_start_referral(uid, start_param, ctx)
    await update.message.reply_text(
        "👋 Welcome to AskCryptoAI\n"
        f"• Free: {FREE_LIMIT} answers/day\n"
        f"• Pro Pack: +{PRO_PACK_SIZE} answers — ${PRO_PRICE_USD:.2f}\n"
        f"• Premium: unlimited* — ${PREMIUM_PRICE_USD:.2f}/month\n"
        f"*Fair‑use: up to {PREMIUM_FAIRUSE_DAILY}/day.\n\n"
        "Type your first question or use /upgrade /plan /price /referral /help"
    )

async def plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    premium_state = "YES" if is_premium(u) else "NO"
    until = "-" if not is_premium(u) else __import__("time").strftime("%Y-%m-%d", __import__("time").localtime(u['premium_until']))
    await update.message.reply_text(
        "Your status:\n"
        f"• Today: {u['daily_count']}/{FREE_LIMIT}\n"
        f"• Pro credits: {u['balance']}\n"
        f"• Premium: {premium_state} (until: {until})\n\n"
        "Use /upgrade to add more."
    )

async def referral(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    me = await ctx.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref{uid}"
    left = max(0, REF_MAX_FRIENDS - u["ref_count"])
    await update.message.reply_text(
        "Invite friends and earn extra answers!\n"
        f"• Your link: {link}\n"
        f"• Reward: +{REF_BONUS_PER_FRIEND} answers per friend\n"
        f"• Invited so far: {u['ref_count']} (max {REF_MAX_FRIENDS})"
    )

async def upgrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pro_stars = int(round(PRO_PRICE_USD*100))
    prem_stars = int(round(PREMIUM_PRICE_USD*100))
    await update.message.reply_text("Choose your plan:")
    await update.message.reply_invoice(
        title=f"Pro Pack (+{PRO_PACK_SIZE} answers)",
        description=f"One-time credits. Adds +{PRO_PACK_SIZE} answers to your balance.",
        payload="pro-pack-credits",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice("Pro Pack", pro_stars)]
    )
    await update.message.reply_invoice(
        title=f"Premium ({PREMIUM_DAYS} days)",
        description=f"Unlimited* answers for {PREMIUM_DAYS} days. *Fair-use: {PREMIUM_FAIRUSE_DAILY}/day.",
        payload="premium-30d",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice("Premium", prem_stars)]
    )

async def successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    pay = update.message.successful_payment
    conn.execute("INSERT INTO payments(user_id,payload,amount_xtr,ts) VALUES(?,?,?,?)",
                 (uid, pay.invoice_payload, pay.total_amount, int(__import__("time").time())))
    conn.commit()
    if pay.invoice_payload == "pro-pack-credits":
        set_user(uid, balance=u["balance"] + PRO_PACK_SIZE)
        await update.message.reply_text(f"✅ Thanks! Added +{PRO_PACK_SIZE} answers. Use /plan to check.")
    elif pay.invoice_payload == "premium-30d":
        now = int(__import__("time").time())
        new_until = max(u["premium_until"], now) + PREMIUM_DAYS*86400
        set_user(uid, premium_until=new_until)
        await update.message.reply_text(f"✅ Premium active until {__import__('time').strftime('%Y-%m-%d', __import__('time').localtime(new_until))}.")
    else:
        await update.message.reply_text("✅ Payment received.")

def format_change(pct: float) -> str:
    sign = "▲" if pct >= 0 else "▼"
    return f"{sign} {pct:.2f}%"

async def price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        resp = requests.get("https://api.coingecko.com/api/v3/simple/price",
                            params={"ids":"bitcoin,ethereum","vs_currencies":"eur","include_24hr_change":"true"},
                            timeout=15)
        resp.raise_for_status()
        data = resp.json()
        btc = data.get("bitcoin", {})
        eth = data.get("ethereum", {})
        btc_line = f"BTC: €{btc.get('eur','?')} ({format_change(btc.get('eur_24h_change',0.0))})"
        eth_line = f"ETH: €{eth.get('eur','?')} ({format_change(eth.get('eur_24h_change',0.0))})"
        await update.message.reply_text(btc_line + "\n" + eth_line)
    except Exception:
        await update.message.reply_text("Can't fetch CoinGecko data right now. Try again later.")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "How it works:\n"
        f"• Free: {FREE_LIMIT} answers per day\n"
        f"• Pro Pack: +{PRO_PACK_SIZE} answers for ${PRO_PRICE_USD:.2f}\n"
        f"• Premium: unlimited* for ${PREMIUM_PRICE_USD:.2f}/month\n"
        f"*Fair‑use: up to {PREMIUM_FAIRUSE_DAILY}/day.\n\n"
        "Disclaimer: This is not financial advice. Always DYOR."
    )

async def admin_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_pay = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount_xtr),0) FROM payments").fetchone()
    count_pay, sum_xtr = total_pay[0], total_pay[1]
    pro_cnt = conn.execute("SELECT COUNT(*) FROM payments WHERE payload='pro-pack-credits'").fetchone()[0]
    prem_cnt = conn.execute("SELECT COUNT(*) FROM payments WHERE payload='premium-30d'").fetchone()[0]
    await update.message.reply_text(
        "📊 Stats:\n"
        f"• Users: {total_users}\n"
        f"• Payments: {count_pay} (total {sum_xtr}⭐)\n"
        f"   - Pro packs: {pro_cnt}\n"
        f"   - Premium: {prem_cnt}"
    )

async def handle_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    u = get_user(uid)
    premium = is_premium(u)

    now_ms = int(__import__("time").time()*1000)
    cd_ms = COOLDOWN_PREMIUM_MS if premium else COOLDOWN_FREE_MS
    if now_ms - u["last_msg_ts"] < cd_ms:
        return
    set_user(uid, last_msg_ts=now_ms)

    has_quota = False
    if premium:
        if u["daily_count"] < PREMIUM_FAIRUSE_DAILY:
            has_quota = True
    else:
        if u["daily_count"] < FREE_LIMIT:
            has_quota = True
        elif u["balance"] > 0:
            has_quota = True

    if not has_quota:
        pro_stars = int(round(PRO_PRICE_USD*100))
        prem_stars = int(round(PREMIUM_PRICE_USD*100))
        await update.message.reply_text(
            "You’ve reached your limit.\n"
            f"• ⭐ Pro Pack: +{PRO_PACK_SIZE} answers — ${PRO_PRICE_USD:.2f}\n"
            f"• 🚀 Premium: unlimited* — ${PREMIUM_PRICE_USD:.2f}/month\n"
            f"*Fair‑use: {PREMIUM_FAIRUSE_DAILY}/day."
        )
        await update.message.reply_invoice(
            title=f"Pro Pack (+{PRO_PACK_SIZE} answers)",
            description=f"Adds +{PRO_PACK_SIZE} AI answers to your balance.",
            payload="pro-pack-credits",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice("Pro Pack", pro_stars)]
        )
        await update.message.reply_invoice(
            title=f"Premium ({PREMIUM_DAYS} days)",
            description=f"Unlimited* answers for {PREMIUM_DAYS} days. *Fair‑use: {PREMIUM_FAIRUSE_DAILY}/day.",
            payload="premium-30d",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice("Premium", prem_stars)]
        )
        return

    if premium:
        set_user(uid, daily_count=u["daily_count"] + 1)
    else:
        if u["daily_count"] < FREE_LIMIT:
            set_user(uid, daily_count=u["daily_count"] + 1)
        else:
            set_user(uid, balance=u["balance"] - 1)

    guard = ("IMPORTANT: You must not provide financial advice. "
             "Add 'This is not financial advice.' and suggest doing own research.")
    try:
        reply = call_openai(guard + "\n\nUser question:\n" + text, premium=premium)
        await update.message.reply_text(reply)
    except Exception:
        await update.message.reply_text("AI is overloaded or network error. Please try again in a minute.")

def main():
    if not BOT_TOKEN: raise SystemExit("Missing TELEGRAM_BOT_TOKEN in .env")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("referral", referral))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    app.run_polling()

if __name__ == "__main__":
    main()

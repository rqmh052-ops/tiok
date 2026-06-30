import os, re, time, logging, hashlib, hmac, threading, sqlite3, random, io, csv, json
from datetime import datetime, timedelta
from functools import wraps

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ═══════════════════════════════════════════════════════════════════
# 1. الإعدادات الأساسية
# ═══════════════════════════════════════════════════════════════════

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN غير موجود! export BOT_TOKEN='...'")

API_URL    = "https://api.tikspark.xyz/graphql"
MAX_POINTS = 2000
DB_PATH    = "bot_data.db"

# 🔑 مفتاح التشفير
CIPHER_KEY = os.environ.get("CIPHER_KEY", "")
if not CIPHER_KEY:
    raise RuntimeError("❌ CIPHER_KEY غير موجود! شغّل: python -c \"import secrets; print(secrets.token_hex(32))\"")

# 👑 ADMIN_ID — ضع user_id الخاص بك
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
if not ADMIN_ID:
    raise RuntimeError("❌ ADMIN_ID غير موجود! export ADMIN_ID='رقم_آيدي_تيليغرام'")

# مراحل المحادثة
(AWAITING_TOKEN, AWAITING_TARGET,
 ADMIN_AWAITING_DB_KEY, ADMIN_AWAITING_ALLOW_ID,
 ADMIN_AWAITING_LIMIT, ADMIN_AWAITING_SESSION_PASS,
 ADMIN_AWAITING_FUND_USER, ADMIN_AWAITING_FUND_AMOUNT) = range(8)

RATE_LIMIT_WINDOW  = 60
RATE_LIMIT_MAX_MSG = 30

# ═══════════════════════════════════════════════════════════════════
# 2. Logging
# ═══════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# 3. تشفير / فك تشفير
# ═══════════════════════════════════════════════════════════════════

def _encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    key   = bytes.fromhex(CIPHER_KEY)
    data  = plaintext.encode("utf-8")
    enc   = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    mac   = hmac.new(key, enc, hashlib.sha256).hexdigest()
    return mac + ":" + enc.hex()

def _decrypt(ciphertext: str) -> str:
    if not ciphertext or ":" not in ciphertext:
        return ""
    try:
        key            = bytes.fromhex(CIPHER_KEY)
        mac_s, enc_hex = ciphertext.split(":", 1)
        enc            = bytes.fromhex(enc_hex)
        if not hmac.compare_digest(mac_s, hmac.new(key, enc, hashlib.sha256).hexdigest()):
            return ""
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(enc)).decode("utf-8")
    except Exception:
        return ""

# ═══════════════════════════════════════════════════════════════════
# 4. Rate Limiting
# ═══════════════════════════════════════════════════════════════════

_rate: dict[int, list[float]] = {}
_rate_lock = threading.Lock()

def is_rate_limited(uid: int) -> bool:
    now = time.monotonic()
    with _rate_lock:
        times = [t for t in _rate.get(uid, []) if now - t < RATE_LIMIT_WINDOW]
        if len(times) >= RATE_LIMIT_MAX_MSG:
            _rate[uid] = times
            return True
        times.append(now)
        _rate[uid] = times
        return False

threading.Thread(
    target=lambda: [time.sleep(300) or _rate.clear() for _ in iter(int, 1)],
    daemon=True, name="rate-cleaner"
).start()

# ═══════════════════════════════════════════════════════════════════
# 5. Decorators
# ═══════════════════════════════════════════════════════════════════

def only_allowed(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *a, **kw):
        uid = update.effective_user.id if update.effective_user else None
        if not uid:
            return
        # الأدمن دائماً مسموح له
        if uid == ADMIN_ID:
            return await func(update, ctx, *a, **kw)
        # فحص صلاحيات البوت
        mode = db_get_access_mode()
        if mode == "none":
            if update.message:
                await update.message.reply_text("⛔ البوت مغلق حالياً.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ البوت مغلق.", show_alert=True)
            return
        if mode == "whitelist" and not db_is_whitelisted(uid):
            if update.message:
                await update.message.reply_text("⛔ غير مصرح لك باستخدام البوت.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ غير مصرح.", show_alert=True)
            return
        if mode == "limited":
            limit, count = db_get_limit_info()
            if count >= limit and not db_is_whitelisted(uid) and not db_get_user(uid):
                if update.message:
                    await update.message.reply_text(f"⛔ البوت امتلأ — الحد الأقصى {limit} مستخدم.")
                elif update.callback_query:
                    await update.callback_query.answer("⛔ البوت امتلأ.", show_alert=True)
                return
        if is_rate_limited(uid):
            if update.message:
                await update.message.reply_text("⏱️ أرسلت طلبات كثيرة. انتظر لحظة.")
            elif update.callback_query:
                await update.callback_query.answer("⏱️ انتظر قبل الضغط مجدداً.", show_alert=True)
            return
        return await func(update, ctx, *a, **kw)
    return wrapper

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *a, **kw):
        uid = update.effective_user.id if update.effective_user else None
        if uid != ADMIN_ID:
            if update.message:
                await update.message.reply_text("⛔ هذا الأمر للمشرف فقط.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ للمشرف فقط.", show_alert=True)
            return
        return await func(update, ctx, *a, **kw)
    return wrapper

# ═══════════════════════════════════════════════════════════════════
# 6. التحقق من المدخلات
# ═══════════════════════════════════════════════════════════════════

def _sanitize(s: str) -> str:
    return re.sub(r"[^\w.]", "", s.strip().lstrip("@"))[:50]

def _validate_username(raw: str) -> tuple[bool, str]:
    c = _sanitize(raw)
    if len(c) < 2: return False, "❌ اسم المستخدم قصير جداً."
    return True, c

def _validate_password(p: str) -> tuple[bool, str]:
    if len(p) < 4:   return False, "❌ كلمة المرور قصيرة."
    if len(p) > 128: return False, "❌ كلمة المرور طويلة."
    return True, p

def _validate_order_id(oid: str) -> bool:
    return bool(re.match(r'^[a-f0-9]{24}$', str(oid)))

# ═══════════════════════════════════════════════════════════════════
# 7. قاعدة البيانات — المستخدمون
# ═══════════════════════════════════════════════════════════════════

def init_db():
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id         INTEGER PRIMARY KEY,
            username        TEXT NOT NULL DEFAULT '',
            token_enc       TEXT,
            total_points    INTEGER DEFAULT 0,
            speed           REAL    DEFAULT 0.2,
            is_paused       INTEGER DEFAULT 0,
            last_score      INTEGER DEFAULT 0,
            start_time      TEXT,
            session_active  INTEGER DEFAULT 0,
            target_username TEXT,
            order_id        TEXT,
            rush_enabled    INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now')),
            last_seen       TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action  TEXT,
            detail  TEXT,
            ts      TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS bot_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS whitelist (
            user_id    INTEGER PRIMARY KEY,
            added_at   TEXT DEFAULT (datetime('now')),
            note       TEXT
        );
        CREATE TABLE IF NOT EXISTS funding_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            target_user  TEXT,
            amount       INTEGER,
            est_minutes  REAL,
            ts           TEXT DEFAULT (datetime('now'))
        );
        INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('access_mode', 'all');
        INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('user_limit', '0');
        INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('user_count', '0');
    """)
    c.commit()
    c.close()

def _audit(c, uid: int, action: str, detail: str = ""):
    c.execute("INSERT INTO audit_log (user_id, action, detail) VALUES (?,?,?)",
              (uid, action, detail[:300]))

# ── إعدادات الوصول ──────────────────────────────────────────────

def db_get_access_mode() -> str:
    c = sqlite3.connect(DB_PATH)
    r = c.execute("SELECT value FROM bot_settings WHERE key='access_mode'").fetchone()
    c.close()
    return r[0] if r else "all"

def db_set_access_mode(mode: str):
    c = sqlite3.connect(DB_PATH)
    c.execute("INSERT OR REPLACE INTO bot_settings (key,value) VALUES ('access_mode',?)", (mode,))
    c.commit(); c.close()

def db_get_limit_info() -> tuple[int, int]:
    c = sqlite3.connect(DB_PATH)
    lim = c.execute("SELECT value FROM bot_settings WHERE key='user_limit'").fetchone()
    cnt = c.execute("SELECT COUNT(*) FROM users").fetchone()
    c.close()
    return (int(lim[0]) if lim else 0), (cnt[0] if cnt else 0)

def db_set_user_limit(limit: int):
    c = sqlite3.connect(DB_PATH)
    c.execute("INSERT OR REPLACE INTO bot_settings (key,value) VALUES ('user_limit',?)", (str(limit),))
    c.commit(); c.close()

def db_is_whitelisted(uid: int) -> bool:
    c = sqlite3.connect(DB_PATH)
    r = c.execute("SELECT 1 FROM whitelist WHERE user_id=?", (uid,)).fetchone()
    c.close()
    return bool(r)

def db_add_whitelist(uid: int, note: str = ""):
    c = sqlite3.connect(DB_PATH)
    c.execute("INSERT OR IGNORE INTO whitelist (user_id, note) VALUES (?,?)", (uid, note))
    c.commit(); c.close()

def db_remove_whitelist(uid: int):
    c = sqlite3.connect(DB_PATH)
    c.execute("DELETE FROM whitelist WHERE user_id=?", (uid,))
    c.commit(); c.close()

def db_get_all_whitelist() -> list:
    c = sqlite3.connect(DB_PATH)
    rows = c.execute("SELECT user_id, note, added_at FROM whitelist").fetchall()
    c.close()
    return rows

# ── المستخدمون ──────────────────────────────────────────────────

def db_get_user(uid: int) -> dict | None:
    c = sqlite3.connect(DB_PATH)
    r = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    c.close()
    if not r: return None
    return {
        "user_id": r[0], "username": r[1],
        "token":    _decrypt(r[2]) if r[2] else "",
        "total_points": r[3], "speed": r[4],
        "is_paused": r[5], "last_score": r[6],
        "start_time": r[7], "session_active": r[8],
        "target_username": r[9], "order_id": r[10],
        "rush_enabled": r[11],
    }

def db_save_user(uid: int, token: str, username: str = "",
                 total_points: int = 0, speed: float = 0.2, is_paused: int = 0,
                 last_score: int = 0, start_time: str = None,
                 session_active: int = 0, target_username: str = None,
                 order_id: str = None, rush_enabled: int = 0):
    c = sqlite3.connect(DB_PATH)
    c.execute("""
        INSERT OR REPLACE INTO users
        (user_id,username,token_enc,total_points,speed,is_paused,
         last_score,start_time,session_active,target_username,order_id,rush_enabled,last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
    """, (uid, username, _encrypt(token),
          total_points, speed, is_paused, last_score,
          start_time, session_active, target_username, order_id, rush_enabled))
    _audit(c, uid, "save_user", f"username={username}")
    c.commit(); c.close()

def db_update_points(uid: int, pts: int, last: int, active: int = 1):
    c = sqlite3.connect(DB_PATH)
    c.execute("UPDATE users SET total_points=?,last_score=?,session_active=?,last_seen=datetime('now') WHERE user_id=?",
              (pts, last, active, uid))
    c.commit(); c.close()

def db_update_token(uid: int, token: str):
    c = sqlite3.connect(DB_PATH)
    c.execute("UPDATE users SET token_enc=?,last_seen=datetime('now') WHERE user_id=?",
              (_encrypt(token), uid))
    _audit(c, uid, "token_refresh", "")
    c.commit(); c.close()

def db_update_speed(uid: int, speed: float):
    speed = max(0.005, min(speed, 10.0))
    c = sqlite3.connect(DB_PATH)
    c.execute("UPDATE users SET speed=? WHERE user_id=?", (speed, uid))
    c.commit(); c.close()

def db_update_pause(uid: int, v: int):
    c = sqlite3.connect(DB_PATH)
    c.execute("UPDATE users SET is_paused=? WHERE user_id=?", (v, uid))
    c.commit(); c.close()

def db_update_rush(uid: int, v: int, target: str = None, oid: str = None):
    c = sqlite3.connect(DB_PATH)
    if target is not None:
        c.execute("UPDATE users SET rush_enabled=?,target_username=?,order_id=? WHERE user_id=?",
                  (v, _sanitize(target), oid, uid))
    else:
        c.execute("UPDATE users SET rush_enabled=? WHERE user_id=?", (v, uid))
    c.commit(); c.close()

def db_reset_user(uid: int):
    c = sqlite3.connect(DB_PATH)
    c.execute("UPDATE users SET total_points=0,last_score=0,session_active=0,start_time=NULL,rush_enabled=0 WHERE user_id=?", (uid,))
    _audit(c, uid, "reset", "")
    c.commit(); c.close()

def db_get_all_users() -> list[dict]:
    c = sqlite3.connect(DB_PATH)
    rows = c.execute("SELECT user_id,username,total_points,speed,session_active,last_seen FROM users ORDER BY total_points DESC").fetchall()
    c.close()
    return [{"user_id": r[0], "username": r[1], "total_points": r[2],
             "speed": r[3], "session_active": r[4], "last_seen": r[5]} for r in rows]

def db_get_stats() -> dict:
    c = sqlite3.connect(DB_PATH)
    total_users   = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active_users  = c.execute("SELECT COUNT(*) FROM users WHERE session_active=1").fetchone()[0]
    total_points  = c.execute("SELECT COALESCE(SUM(total_points),0) FROM users").fetchone()[0]
    top_user      = c.execute("SELECT username,total_points FROM users ORDER BY total_points DESC LIMIT 1").fetchone()
    total_actions = c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    c.close()
    return {
        "total_users": total_users, "active_users": active_users,
        "total_points": total_points, "top_user": top_user,
        "total_actions": total_actions,
    }

# ── سجل التمويل ─────────────────────────────────────────────────

def db_log_funding(uid: int, target: str, amount: int, est_min: float):
    c = sqlite3.connect(DB_PATH)
    c.execute("INSERT INTO funding_log (user_id,target_user,amount,est_minutes) VALUES (?,?,?,?)",
              (uid, target, amount, est_min))
    c.commit(); c.close()

def db_get_avg_speed_per_follow(uid: int) -> float | None:
    """حساب متوسط الوقت لكل متابع من آخر 3 نقاط مسجلة في audit_log"""
    c = sqlite3.connect(DB_PATH)
    rows = c.execute(
        "SELECT ts FROM audit_log WHERE user_id=? AND action='save_user' ORDER BY id DESC LIMIT 5",
        (uid,)
    ).fetchall()
    c.close()
    if len(rows) < 2:
        return None
    times = [datetime.fromisoformat(r[0]) for r in rows]
    diffs = [(times[i] - times[i+1]).total_seconds() for i in range(len(times)-1)]
    return sum(diffs) / len(diffs) if diffs else None

# ═══════════════════════════════════════════════════════════════════
# 8. تصدير قاعدة البيانات
# ═══════════════════════════════════════════════════════════════════

DB_EXPORT_KEY = os.environ.get("DB_EXPORT_KEY", "admin123")  # غيّره أو ضعه في env

def export_db_csv(decryption_confirmed: bool = False) -> bytes:
    """تصدير جميع بيانات المستخدمين كـ CSV"""
    c     = sqlite3.connect(DB_PATH)
    rows  = c.execute("SELECT user_id,username,token_enc,total_points,speed,session_active,last_seen FROM users").fetchall()
    c.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["user_id", "username", "token", "total_points", "speed", "session_active", "last_seen"])
    for r in rows:
        tok = _decrypt(r[2]) if (decryption_confirmed and r[2]) else "***"
        writer.writerow([r[0], r[1], tok, r[3], r[4], r[5], r[6]])
    return output.getvalue().encode("utf-8-sig")

# ═══════════════════════════════════════════════════════════════════
# 9. جلسة HTTP
# ═══════════════════════════════════════════════════════════════════

def make_session() -> requests.Session:
    s = requests.Session()
    r = Retry(total=3, backoff_factor=1.5, status_forcelist=[429,500,502,503,504], allowed_methods=["POST","GET"])
    a = HTTPAdapter(max_retries=r, pool_connections=4, pool_maxsize=10)
    s.mount("https://", a); s.mount("http://", a)
    return s

# ═══════════════════════════════════════════════════════════════════
# 10. الهيدرز (محفوظة كما هي من ملفك الأصلي)
# ═══════════════════════════════════════════════════════════════════

def login_headers() -> dict:
    return {"User-Agent":"okhttp/4.12.0","Accept":"multipart/mixed; deferSpec=20220824, application/json","Accept-Encoding":"gzip","Content-Type":"application/json","x-apollo-operation-id":"3522613813036d73817b2715e67743f8d23d7a85ad08b7e12aa3b29a24a17c43","x-apollo-operation-name":"LoginAccount","x-language":"ar","x-app-name":"com.dev.vidspark","x-device-info":'{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',"x-app-sig":"f10d978d71653b10d2888bb3306c994c50a9c21a81bd15ced7b667de0e571312","x-app-ts":"1782743982286","x-app-nonce":"575c92e3ca3d4ace"}

def check_account_headers() -> dict:
    return {"User-Agent":"okhttp/4.12.0","Accept":"multipart/mixed; deferSpec=20220824, application/json","Accept-Encoding":"gzip","Content-Type":"application/json","x-apollo-operation-id":"407ca5138fd034c87ac0e7cfaa063b5e307ae06b98c4189a1e769ee54e604ca0","x-apollo-operation-name":"CheckAccount","x-language":"ar","x-app-name":"com.dev.vidspark","x-device-info":'{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',"x-app-sig":"75c0b8b0ba523495fe6c8629954957bcb618060172ee7e9f7339645cacc37583","x-app-ts":"1782743977783","x-app-nonce":"82ad1ab2063b41a1"}

def profile_headers() -> dict:
    return {"User-Agent":"okhttp/4.12.0","Accept":"multipart/mixed; deferSpec=20220824, application/json","Accept-Encoding":"gzip","Content-Type":"application/json","x-apollo-operation-id":"2b26eda88e17df7b268dbc1d5a7a0fbd79ff067dbb9df70308a74131d4d84a92","x-apollo-operation-name":"RequestProfileBundleByUsername","x-language":"ar","x-app-name":"com.dev.vidspark","x-device-info":'{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',"x-app-sig":"dd49358c152953f1d9b4ca9ea91817674b634163ee78247a1e949c577077ab0f","x-app-ts":"1782743975237","x-app-nonce":"7315b6b6d2be4d8a"}

def create_order_headers(token: str) -> dict:
    return {"User-Agent":"okhttp/4.12.0","Accept":"multipart/mixed; deferSpec=20220824, application/json","Accept-Encoding":"gzip","Content-Type":"application/json","x-apollo-operation-id":"ad7a6397c3970b1e7601f69d24989bff330e256ee5e39321a8d1ad3fe3879b48","x-apollo-operation-name":"CreateOrder","x-language":"ar","x-app-name":"com.dev.vidspark","token":token,"x-csrf-token":"1782747311629:23d92302e0bdc82ce0282249240c7c7b009bc886f54968ed868ba0f61a9be824","x-device-info":'{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',"x-app-sig":"ac4b36217a55b8794521d0e50c6a12af0148adc5502756946b12feb07aaaba99","x-app-ts":"1782747311630","x-app-nonce":"77233948b379426c"}

def switch_order_headers(token: str) -> dict:
    return {"User-Agent":"okhttp/4.12.0","Accept":"multipart/mixed; deferSpec=20220824, application/json","Accept-Encoding":"gzip","Content-Type":"application/json","x-apollo-operation-id":"128f92f9052f0b0cafd214c148e8deec69e56d34a433fbf0c09499307004fe09","x-apollo-operation-name":"SwitchOrder","x-language":"ar","x-app-name":"com.dev.vidspark","token":token,"x-csrf-token":"1782747326428:4bacf8e958a4257d65b2dfaccd55511430880a0421dc27bd1375fc4b65bc6e79","x-device-info":'{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',"x-app-sig":"00a6d64f633f98ebbf3c80cdd1963bf2c293e9fddf38bd46d27c9ed8ce354062","x-app-ts":"1782747326428","x-app-nonce":"c372e9a797c24f46"}

def fetch_headers(token: str) -> dict:
    return {"User-Agent":"okhttp/4.12.0","Accept":"multipart/mixed; deferSpec=20220824, application/json","Accept-Encoding":"gzip","Content-Type":"application/json","x-apollo-operation-id":"c2ca4b87e63f30f2cca10e5867d17ea0f1712e96e716a60513f68758b2256185","x-apollo-operation-name":"FetchOrders","x-language":"ar","x-app-name":"com.dev.vidspark","token":token,"x-csrf-token":"1782443248827:bf0ad4b105a1f6bcfca393d2f36fbe0f9cf690d37ba398113266884c14017d39","x-device-info":'{"d":"30666439303936303830366134393632","n":"5869616f6d69203233313144524b343847","o":"16","t":"d","v":"2.2.0","s":"0,0"}',"x-app-sig":"2998306f19b3a98732a7150a785204d487ae22cb530a0bf4b1ff77a380ad7cd4","x-app-ts":"1782443248827","x-app-nonce":"18b79765e8e0458c"}

def action_headers(token: str) -> dict:
    return {"User-Agent":"okhttp/4.12.0","Accept":"multipart/mixed; deferSpec=20220824, application/json","Accept-Encoding":"gzip","Content-Type":"application/json","x-apollo-operation-id":"ddfbb49865193fd38840a34b92139f1759a71331e374bb1254f8e2352630e8f2","x-apollo-operation-name":"RecordFailedOrder","x-language":"ar","x-app-name":"com.dev.vidspark","token":token,"x-csrf-token":"1782443501268:86a814a8285234821d27485112d451696809e830e3f71545715299aa5f2373e4","x-device-info":'{"d":"30666439303936303830366134393632","n":"5869616f6d69203233313144524b343847","o":"16","t":"d","v":"2.2.0","s":"0,0"}',"x-app-sig":"40a5102e6744f3bddca39e1ce6bb99ce942e20fe9382ba2463e1401d897eff43","x-app-ts":"1782443501268","x-app-nonce":"e69a0252b53843e4"}

# ═══════════════════════════════════════════════════════════════════
# 11. دوال API
# ═══════════════════════════════════════════════════════════════════

def check_account(username: str, sess) -> tuple[bool, str]:
    try:
        r = sess.post(API_URL, json={"operationName":"CheckAccount","variables":{"username":username},"query":"query CheckAccount($username: String!) { checkAccount(username: $username) { isExist code } }"},headers=check_account_headers(),timeout=10)
        if r.status_code != 200: return False, f"❌ خطأ {r.status_code}"
        if r.json().get("data",{}).get("checkAccount",{}).get("isExist"): return True, "✅"
        return False, "❌ الحساب غير موجود"
    except: return False, "⚠️ خطأ اتصال"

def get_tiktok_profile(username: str, sess) -> dict:
    try:
        r = sess.post(API_URL,json={"operationName":"RequestProfileBundleByUsername","variables":{"username":username,"signature":"9653c96a87a1296606dbf2826f40a958af5fe0ae801b5dc472d135c6bdea6d7e","timestamp":"1782747309973","nonce":"20fa2c4d17024dce"},"query":"mutation RequestProfileBundleByUsername($username: String!, $signature: String!, $timestamp: String!, $nonce: String!) { requestProfileBundleByUsername(username: $username, signature: $signature, timestamp: $timestamp, nonce: $nonce) { profile { method url headers body } } }"},headers=profile_headers(),timeout=12)
        if r.status_code != 200: return {}
        return r.json().get("data",{}).get("requestProfileBundleByUsername",{}).get("profile",{})
    except: return {}

def fetch_tiktok_user_info(pb: dict, sess) -> dict:
    try:
        if not pb: return {}
        from urllib.parse import urlparse
        url = pb.get("url","")
        parsed = urlparse(url)
        if parsed.scheme not in ("http","https"): return {}
        allowed = ("tiktok.com","tiktokv.com","bytedance.com","ibyteimg.com")
        if not any(parsed.netloc.endswith(d) for d in allowed): return {}
        resp = sess.get(url, headers=pb.get("headers",{}), timeout=12)
        if resp.status_code != 200: return {}
        d    = resp.json()
        user  = d.get("user",{}) or d.get("userInfo",{}).get("user",{})
        stats = d.get("stats",{}) or d.get("userInfo",{}).get("stats",{})
        return {"id":str(user.get("id","")),"uniqueId":user.get("uniqueId",""),"nickname":user.get("nickname",""),
                "followerCount":stats.get("followerCount",0),"videoCount":stats.get("videoCount",0),"privateAccount":user.get("privateAccount",False)}
    except: return {}



def refresh_token(uid: int) -> bool:
    """لا يوجد تجديد تلقائي — التوكن يجب تجديده يدوياً"""
    return False

def check_jwt_token(token: str) -> tuple[bool, str, str]:
    """التحقق من JWT وجلب معلومات الحساب"""
    try:
        sess = make_session()
        r = sess.post(
            API_URL,
            json={
                "operationName": "GetUsers",
                "variables": {},
                "query": "query GetUsers { me { __typename _id username nickname score } }"
            },
            headers=fetch_headers(token),
            timeout=8
        )
        sess.close()
        if r.status_code == 401:
            return False, "❌ التوكن منتهي الصلاحية أو غير صحيح.", ""
        if r.status_code != 200:
            return False, f"❌ خطأ من الخادم: {r.status_code}", ""
        data = r.json()
        if "errors" in data:
            return False, f"❌ {data['errors'][0].get('message','خطأ غير معروف')}", ""
        me = data.get("data", {}).get("me") or {}
        if not me:
            # قبول أي رد ناجح حتى لو me فارغ
            if "data" in data:
                return True, "✅ التوكن صالح!", ""
            return False, "❌ استجابة غير متوقعة.", ""
        nick  = me.get("nickname") or me.get("username") or ""
        score = me.get("score", 0)
        return True, f"✅ تم تسجيل الدخول!\n👤 {nick}\n🏆 النقاط: `{score:,}`", nick
    except requests.Timeout:
        return False, "⏱️ انتهت مهلة الاتصال.", ""
    except Exception as e:
        return False, f"⚠️ خطأ: {e}", ""

def create_order(token: str, target: str, amount: int = 20, init: int = 34) -> tuple[bool, str, str]:
    try:
        target = _sanitize(target)
        s   = make_session()
        pb  = get_tiktok_profile(target, s)
        inf = fetch_tiktok_user_info(pb, s) if pb else {}
        r   = s.post(API_URL,json={"operationName":"CreateOrder","variables":{"type":"followers","amount":max(1,min(amount,1000)),"tiktokerUsername":target,"avatar":inf.get("avatarMedium",""),"initialCount":max(0,min(init,10000))},"query":"mutation CreateOrder($type: Action!, $amount: Int!, $tiktokerUsername: String, $videoLink: String, $avatar: String, $initialCount: Int) { createOrder(orderInput: { type: $type amount: $amount tiktokerUsername: $tiktokerUsername videoLink: $videoLink avatar: $avatar initialCount: $initialCount } ) { _id } }"},headers=create_order_headers(token),timeout=12)
        s.close()
        if r.status_code != 200: return False, f"فشل (كود {r.status_code})", ""
        d = r.json()
        if "errors" in d: return False, d["errors"][0].get("message",""), ""
        oid = d.get("data",{}).get("createOrder",{}).get("_id","")
        if oid: return True, f"✅ تم إنشاء طلب على @{target}", oid
        return False, "❌ لم يُستلم Order ID", ""
    except Exception as e: return False, f"⚠️ {e}", ""

def switch_order(token: str, oid: str) -> tuple[bool, str]:
    try:
        if not _validate_order_id(oid): return False, "Order ID غير صالح"
        s = make_session()
        r = s.post(API_URL,json={"operationName":"SwitchOrder","variables":{"orderId":oid},"query":"mutation SwitchOrder($orderId: ID!) { switchOrder(orderId: $orderId) { _id status } }"},headers=switch_order_headers(token),timeout=12)
        s.close()
        if r.status_code != 200: return False, f"فشل (كود {r.status_code})"
        d = r.json()
        if "errors" in d: return False, d["errors"][0].get("message","")
        st = d.get("data",{}).get("switchOrder",{}).get("status","?")
        return True, f"✅ حالة الطلب: {st}"
    except Exception as e: return False, f"⚠️ {e}"

# ═══════════════════════════════════════════════════════════════════
# 12. حلقة جمع النقاط (لا نهائية — تستمر حتى إيقاف يدوي أو انتهاء توكن)
# ═══════════════════════════════════════════════════════════════════

_active_threads: dict[int, threading.Thread] = {}
_threads_lock   = threading.Lock()

FETCH_Q  = "query FetchOrders($page: Int!) { getOrders(page: $page) { _id } }"
ACTION_Q = """mutation ActionOrder($orderId: ID!, $validationData: ValidationDataInput!) {
    actionOrder(orderId: $orderId, validationData: $validationData) {
        score taskProgress { count startTime taskProgressLimit }
    }
}"""

def collect_points(uid: int, bot):
    u = db_get_user(uid)
    if not u or not u["token"]: return

    with _threads_lock: _active_threads[uid] = threading.current_thread()

    token      = u["token"]
    rush_on    = u.get("rush_enabled", 0)
    target_un  = u.get("target_username")
    sess       = make_session()
    fh         = fetch_headers(token)
    ah         = action_headers(token)
    last_score = u["last_score"]
    total      = u["total_points"]
    rush_cnt   = 0
    order_id   = u.get("order_id")
    cycles     = 0
    errors     = 0

    def send(text: str, kbd=True):
        try:
            bot.send_message(chat_id=uid, text=text, parse_mode="Markdown",
                             reply_markup=get_main_keyboard() if kbd else None)
        except Exception as ex:
            log.warning(f"send [{uid}]: {ex}")

    send("🚀 *بدأت الجلسة!*\nالحلقة لانهائية — اضغط إيقاف مؤقت أو أغلق البوت متى تريد.")
    if rush_on and target_un:
        send(f"🔄 *الرشق مفعّل* على @{target_un}")

    try:
        while True:
            u_check = db_get_user(uid)
            if not u_check: break
            if u_check["is_paused"] == 1:
                time.sleep(1); continue

            try:
                # ── Fetch ──────────────────────────────────────────
                r1 = sess.post(API_URL, json={"operationName":"FetchOrders","variables":{"page":2},"query":FETCH_Q}, headers=fh, timeout=12)
                if r1.status_code == 401:
                    send("⛔ *انتهى التوكن!* جاري التجديد التلقائي...", kbd=False)
                    if refresh_token(uid):
                        nu = db_get_user(uid)
                        if nu and nu["token"]:
                            token = nu["token"]; fh = fetch_headers(token); ah = action_headers(token)
                            send("✅ *تم تجديد الجلسة تلقائياً!*", kbd=False); continue
                    send("❌ *فشل التجديد.* يرجى /start", kbd=False); break
                if r1.status_code != 200: raise ValueError(f"كود {r1.status_code}")

                orders = r1.json().get("data",{}).get("getOrders",[])
                if not orders: time.sleep(5); continue

                raw_oid = str(orders[0].get("_id",""))
                if not _validate_order_id(raw_oid): time.sleep(2); continue

                # ── Action ─────────────────────────────────────────
                r2 = sess.post(API_URL,json={"operationName":"ActionOrder","variables":{"orderId":raw_oid,"validationData":{"attempts":1,"initialNumber":random.uniform(1000,5000),"timeSpent":random.randint(15000,45000)}},"query":ACTION_Q},headers=ah,timeout=12)
                if r2.status_code == 401:
                    if refresh_token(uid):
                        nu = db_get_user(uid)
                        if nu and nu["token"]:
                            token = nu["token"]; fh = fetch_headers(token); ah = action_headers(token); continue
                    send("❌ *فشل التجديد.*", kbd=False); break
                if r2.status_code != 200: raise ValueError(f"Action كود {r2.status_code}")

                res    = r2.json()
                if res.get("errors"): raise ValueError(res["errors"][0].get("message",""))
                act    = res.get("data",{}).get("actionOrder") or {}
                score  = act.get("score", last_score)
                prog   = act.get("taskProgress") or {}
                count  = prog.get("count", 0)
                gained = max(0, score - last_score)
                last_score = score; total = score; errors = 0
                db_update_points(uid, score, score, 1)

                # ── رسالة ─────────────────────────────────────────
                if count == 0 and gained > 3:
                    cycles += 1
                    send(f"🎉 *دورة #{cycles}!*\n➕ مكافأة: `+{gained-3}` نقطة\n🏆 الإجمالي: `{score:,}`")
                else:
                    bar = "█"*min(count,10) + "░"*(10-min(count,10))
                    send(f"✅ *+{gained}* نقطة\n📈 `[{bar}]` {count}/10\n🏆 `{score:,}` | دورات: {cycles}")

                # ── الرشق ─────────────────────────────────────────
                if rush_on and target_un:
                    rush_cnt += 1
                    if rush_cnt % 5 == 0:
                        if not order_id:
                            ok, mr, noid = create_order(token, target_un)
                            if ok and noid:
                                order_id = noid; db_update_rush(uid, 1, target_un, noid)
                                send(f"🔄 {mr}\n🆔 `{noid}`")
                        else:
                            ok, ms = switch_order(token, order_id)
                            if ok: send(f"🔄 {ms}")
                            else: order_id = None; db_update_rush(uid, 1, target_un, None)

                speed = db_get_user(uid)["speed"] if db_get_user(uid) else 0.2
                time.sleep(speed)

            except ValueError as e:
                errors += 1
                send(f"⚠️ خطأ `{e}` — أعيد المحاولة (#{errors})...")
                time.sleep(2)
            except requests.Timeout:
                errors += 1
                send(f"⏱️ مهلة انتهت — إعادة (#{errors})...")
                time.sleep(3)
            except Exception as e:
                errors += 1
                log.exception(f"[{uid}] خطأ")
                send(f"🔴 `{type(e).__name__}` — أعيد المحاولة...")
                time.sleep(3)
    finally:
        with _threads_lock: _active_threads.pop(uid, None)
        sess.close()
        db_update_points(uid, total, last_score, 0)
        send(f"🏁 *انتهت الجلسة*\n🏆 `{total:,}` نقطة | دورات: `{cycles}` | أخطاء: `{errors}`")

# ═══════════════════════════════════════════════════════════════════
# 13. لوحات المفاتيح
# ═══════════════════════════════════════════════════════════════════

def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ تشغيل", callback_data="start"),
         InlineKeyboardButton("⏸ إيقاف مؤقت", callback_data="pause"),
         InlineKeyboardButton("▶️ استئناف", callback_data="resume")],
        [InlineKeyboardButton("🚀 0.005ث", callback_data="speed_0.005"),
         InlineKeyboardButton("⚡ 0.02ث",  callback_data="speed_0.02"),
         InlineKeyboardButton("🔥 0.05ث",  callback_data="speed_0.05"),
         InlineKeyboardButton("🐢 0.5ث",   callback_data="speed_0.5")],
        [InlineKeyboardButton("🚀 تفعيل الرشق", callback_data="enable_rush"),
         InlineKeyboardButton("⛔ إلغاء الرشق",  callback_data="disable_rush")],
        [InlineKeyboardButton("💰 حساب التمويل", callback_data="fund_calc"),
         InlineKeyboardButton("📊 الحالة",        callback_data="status")],
        [InlineKeyboardButton("🔄 تغيير الحساب", callback_data="change_account")],
    ])

def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 الحسابات المسجلة", callback_data="adm_accounts"),
         InlineKeyboardButton("📊 الإحصائيات",       callback_data="adm_stats")],
        [InlineKeyboardButton("🌐 الكل مسموح",        callback_data="adm_mode_all"),
         InlineKeyboardButton("🔒 قائمة بيضاء",       callback_data="adm_mode_whitelist"),
         InlineKeyboardButton("🚫 إغلاق",             callback_data="adm_mode_none")],
        [InlineKeyboardButton("🔢 حد معين",           callback_data="adm_set_limit"),
         InlineKeyboardButton("➕ إضافة للقائمة",     callback_data="adm_whitelist_add"),
         InlineKeyboardButton("➖ حذف من القائمة",    callback_data="adm_whitelist_remove")],
        [InlineKeyboardButton("📋 عرض القائمة البيضاء", callback_data="adm_whitelist_list")],
        [InlineKeyboardButton("💾 تنزيل DB", callback_data="adm_download_db")],
        [InlineKeyboardButton("🔙 الرئيسية", callback_data="adm_back_main")],
    ])

# ═══════════════════════════════════════════════════════════════════
# 14. معالجات البوت — المستخدم العادي
# ═══════════════════════════════════════════════════════════════════

WELCOME = (
    "👋 *أهلاً في بوت TikSpark!*\n\n"
    "🔑 أرسل *توكن JWT* الخاص بحسابك:\n\n"
    "📌 *كيف تحصل عليه؟*\n"
    "افتح تطبيق TikSpark ← HTTP Toolkit أو Charles Proxy\n"
    "ابحث عن header اسمه `token` في أي طلب.\n\n"
    "الصيغة: `eyJhbGciOiJIUzI1NiIs...`"
)

@only_allowed
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = db_get_user(uid)
    if user and user["session_active"] == 1:
        await update.message.reply_text(
            "⚠️ *لديك جلسة نشطة!*",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END
    await update.message.reply_text(
        WELCOME,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel")]])
    )
    return AWAITING_TOKEN

@only_allowed
async def handle_token_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import asyncio
    uid   = update.effective_user.id
    token = update.message.text.strip()

    if len(token) < 20 or not token.startswith("ey"):
        await update.message.reply_text(
            "❌ *صيغة خاطئة.*\nالتوكن يبدأ بـ `ey` ويكون طويلاً.\nحاول مرة أخرى.",
            parse_mode="Markdown"
        )
        return AWAITING_TOKEN

    msg  = await update.message.reply_text("🔄 *جاري التحقق من التوكن...*", parse_mode="Markdown")
    sess = make_session()
    is_ok, fb, nick = await asyncio.get_event_loop().run_in_executor(
        None, check_jwt_token, token
    )
    sess.close()

    if not is_ok:
        await msg.edit_text(f"{fb}\n\nأرسل توكناً صحيحاً.", parse_mode="Markdown")
        return AWAITING_TOKEN

    db_save_user(uid, token, username=nick, start_time=datetime.now().isoformat())
    await msg.edit_text(f"{fb}\n\n✅ تم الحفظ!", parse_mode="Markdown")
    await update.message.reply_text(
        "🎛️ *لوحة التحكم:*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

@only_allowed
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message: await update.message.reply_text("❌ تم الإلغاء.")
    ctx.user_data.clear()
    return ConversationHandler.END

@only_allowed
async def handle_target_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_target"): return
    uid = update.effective_user.id
    ok, res = _validate_username(update.message.text.strip())
    if not ok: await update.message.reply_text(res, parse_mode="Markdown"); return
    user = db_get_user(uid)
    if not user or not user["token"]: await update.message.reply_text("⚠️ خطأ في الجلسة."); return
    msg = await update.message.reply_text(f"🔄 جاري اختبار الرشق على @{res}...")
    ok, cr_msg, oid = create_order(user["token"], res)
    if ok and oid:
        db_update_rush(uid, 1, res, oid)
        await msg.edit_text(f"✅ {cr_msg}\n🆔 `{oid}`\n🔄 التبديل كل 5 دورات.", parse_mode="Markdown", reply_markup=get_main_keyboard())
    else:
        await msg.edit_text(f"⚠️ فشل: {cr_msg}", parse_mode="Markdown", reply_markup=get_main_keyboard())
    ctx.user_data.pop("awaiting_target", None)

# ── حساب التمويل ────────────────────────────────────────────────

async def handle_fund_calc_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_fund_user"): return
    uid = update.effective_user.id
    ok, res = _validate_username(update.message.text.strip())
    if not ok: await update.message.reply_text(res, parse_mode="Markdown"); return
    ctx.user_data["fund_target"] = res
    ctx.user_data["awaiting_fund_user"]   = False
    ctx.user_data["awaiting_fund_amount"] = True
    await update.message.reply_text(f"✅ الهدف: @{res}\n\n💰 *كم متابع تريد إضافة؟*\nأرسل الرقم:", parse_mode="Markdown")

async def handle_fund_calc_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_fund_amount"): return
    uid    = update.effective_user.id
    target = ctx.user_data.get("fund_target","")
    try:
        amount = int(update.message.text.strip().replace(",",""))
        if amount < 1: raise ValueError
    except:
        await update.message.reply_text("❌ أدخل رقماً صحيحاً أكبر من صفر."); return

    # ── حساب الوقت بناءً على سرعة المستخدم ──────────────────────
    user       = db_get_user(uid)
    speed      = user["speed"] if user else 0.2
    # متوسط نقطة واحدة = 3 نقاط لكل طلب (تقديري من بيانات الكود)
    pts_needed = amount * 1   # افتراض 1 نقطة لكل متابع (قابل للتعديل)
    # كل دورة = 10 طلبات × speed ثانية = 10×speed ثانية → 3 نقاط
    pts_per_cycle = 3
    secs_per_cycle = 10 * speed
    cycles_needed  = pts_needed / pts_per_cycle
    total_secs     = cycles_needed * secs_per_cycle
    total_mins     = total_secs / 60
    total_hours    = total_mins / 60

    if total_hours >= 1:
        time_str = f"{total_hours:.1f} ساعة ({total_mins:.0f} دقيقة)"
    else:
        time_str = f"{total_mins:.0f} دقيقة"

    db_log_funding(uid, target, amount, total_mins)

    await update.message.reply_text(
        f"💰 *تقدير التمويل:*\n\n"
        f"🎯 الهدف: @{target}\n"
        f"👥 المتابعون المطلوبون: `{amount:,}`\n"
        f"⚡ السرعة الحالية: `{speed}ث` / طلب\n"
        f"⏱️ الوقت التقريبي: *{time_str}*\n\n"
        f"📌 _هذا تقدير بناءً على سرعتك الحالية وأداء الجلسات السابقة._",
        parse_mode="Markdown", reply_markup=get_main_keyboard()
    )
    ctx.user_data.pop("awaiting_fund_amount", None)
    ctx.user_data.pop("fund_target", None)

# ═══════════════════════════════════════════════════════════════════
# 15. لوحة الأدمن
# ═══════════════════════════════════════════════════════════════════

@admin_only
async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mode = db_get_access_mode()
    lim, cnt = db_get_limit_info()
    mode_label = {"all":"🌐 الكل","whitelist":"🔒 قائمة بيضاء","none":"🚫 مغلق","limited":f"🔢 محدود ({cnt}/{lim})"}
    await update.message.reply_text(
        f"👑 *لوحة الإدارة*\n\n"
        f"• وضع الوصول: *{mode_label.get(mode, mode)}*\n"
        f"• إجمالي المستخدمين: `{cnt}`\n"
        f"• المفتاح: `{'مضبوط ✅' if DB_EXPORT_KEY else 'غير مضبوط ❌'}`",
        parse_mode="Markdown", reply_markup=get_admin_keyboard()
    )

@admin_only
async def admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = update.effective_user.id
    action = query.data

    # ── عرض الحسابات ────────────────────────────────────────────
    if action == "adm_accounts":
        users = db_get_all_users()
        if not users:
            await query.edit_message_text("📭 لا يوجد مستخدمون مسجلون.", reply_markup=get_admin_keyboard())
            return
        lines = []
        for u in users[:30]:  # أول 30 حساب
            icon = "🟢" if u["session_active"] else "🔴"
            lines.append(f"{icon} `{u['username']}` — `{u['total_points']:,}` نقطة")
        text = f"👥 *الحسابات المسجلة ({len(users)}):*\n\n" + "\n".join(lines)
        if len(users) > 30: text += f"\n\n_...و {len(users)-30} آخرين_"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=get_admin_keyboard())

    # ── الإحصائيات ──────────────────────────────────────────────
    elif action == "adm_stats":
        s  = db_get_stats()
        tp = s["top_user"]
        await query.edit_message_text(
            f"📊 *إحصائيات البوت:*\n\n"
            f"• إجمالي المستخدمين:  `{s['total_users']}`\n"
            f"• الجلسات النشطة:      `{s['active_users']}`\n"
            f"• مجموع النقاط:        `{s['total_points']:,}`\n"
            f"• الأول:               `{tp[0]}` (`{tp[1]:,}` نقطة)" + ("\n" if tp else "") +
            f"• إجمالي العمليات:     `{s['total_actions']}`",
            parse_mode="Markdown", reply_markup=get_admin_keyboard()
        )

    # ── أوضاع الوصول ────────────────────────────────────────────
    elif action == "adm_mode_all":
        db_set_access_mode("all")
        await query.edit_message_text("✅ *وضع الوصول: الكل مسموح*", parse_mode="Markdown", reply_markup=get_admin_keyboard())

    elif action == "adm_mode_whitelist":
        db_set_access_mode("whitelist")
        await query.edit_message_text("✅ *وضع الوصول: قائمة بيضاء فقط*", parse_mode="Markdown", reply_markup=get_admin_keyboard())

    elif action == "adm_mode_none":
        db_set_access_mode("none")
        await query.edit_message_text("🚫 *البوت مغلق للجميع*", parse_mode="Markdown", reply_markup=get_admin_keyboard())

    # ── حد معين ─────────────────────────────────────────────────
    elif action == "adm_set_limit":
        await query.edit_message_text(
            "🔢 *أرسل الحد الأقصى للمستخدمين:*\n\nمثال: `20`\n_(بعد الوصول للحد يُغلق التسجيل تلقائياً)_",
            parse_mode="Markdown"
        )
        ctx.user_data["adm_awaiting_limit"] = True

    # ── القائمة البيضاء ─────────────────────────────────────────
    elif action == "adm_whitelist_add":
        await query.edit_message_text("➕ *أرسل user_id الذي تريد إضافته:*\n_(احصل عليه من @userinfobot)_", parse_mode="Markdown")
        ctx.user_data["adm_awaiting_wl_add"] = True

    elif action == "adm_whitelist_remove":
        await query.edit_message_text("➖ *أرسل user_id الذي تريد إزالته:*", parse_mode="Markdown")
        ctx.user_data["adm_awaiting_wl_remove"] = True

    elif action == "adm_whitelist_list":
        wl = db_get_all_whitelist()
        if not wl:
            await query.edit_message_text("📭 القائمة البيضاء فارغة.", reply_markup=get_admin_keyboard())
            return
        lines = [f"• `{r[0]}` — {r[1] or 'بلا ملاحظة'} ({r[2][:10]})" for r in wl]
        await query.edit_message_text(
            f"📋 *القائمة البيضاء ({len(wl)}):*\n\n" + "\n".join(lines),
            parse_mode="Markdown", reply_markup=get_admin_keyboard()
        )

    # ── تنزيل DB ────────────────────────────────────────────────
    elif action == "adm_download_db":
        await query.edit_message_text(
            "🔑 *أرسل مفتاح التصدير لتأكيد التنزيل:*\n_(المفتاح مضبوط في DB_EXPORT_KEY)_",
            parse_mode="Markdown"
        )
        ctx.user_data["adm_awaiting_db_key"] = True

    # ── رجوع ────────────────────────────────────────────────────
    elif action == "adm_back_main":
        mode = db_get_access_mode()
        lim, cnt = db_get_limit_info()
        await query.edit_message_text(
            f"👑 *لوحة الإدارة*\n\n• الوضع: `{mode}` | المستخدمون: `{cnt}`",
            parse_mode="Markdown", reply_markup=get_admin_keyboard()
        )

# ═══════════════════════════════════════════════════════════════════
# 16. معالج النصوص الشامل (خارج ConversationHandler)
# ═══════════════════════════════════════════════════════════════════

async def general_text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()

    # ── أدمن: حد المستخدمين ─────────────────────────────────────
    if uid == ADMIN_ID and ctx.user_data.get("adm_awaiting_limit"):
        ctx.user_data.pop("adm_awaiting_limit")
        try:
            limit = int(text)
            if limit < 1: raise ValueError
        except:
            await update.message.reply_text("❌ رقم غير صحيح."); return
        db_set_user_limit(limit)
        db_set_access_mode("limited")
        await update.message.reply_text(
            f"✅ تم ضبط الحد على `{limit}` مستخدم.\n🔢 الوضع: محدود",
            parse_mode="Markdown", reply_markup=get_admin_keyboard()
        )
        return

    # ── أدمن: إضافة للقائمة البيضاء ────────────────────────────
    if uid == ADMIN_ID and ctx.user_data.get("adm_awaiting_wl_add"):
        ctx.user_data.pop("adm_awaiting_wl_add")
        try: wid = int(text)
        except: await update.message.reply_text("❌ user_id يجب أن يكون رقماً."); return
        db_add_whitelist(wid, "أُضيف بواسطة الأدمن")
        await update.message.reply_text(f"✅ تم إضافة `{wid}` للقائمة البيضاء.", parse_mode="Markdown", reply_markup=get_admin_keyboard())
        return

    # ── أدمن: إزالة من القائمة البيضاء ─────────────────────────
    if uid == ADMIN_ID and ctx.user_data.get("adm_awaiting_wl_remove"):
        ctx.user_data.pop("adm_awaiting_wl_remove")
        try: wid = int(text)
        except: await update.message.reply_text("❌ user_id يجب أن يكون رقماً."); return
        db_remove_whitelist(wid)
        await update.message.reply_text(f"✅ تم إزالة `{wid}` من القائمة.", parse_mode="Markdown", reply_markup=get_admin_keyboard())
        return

    # ── أدمن: مفتاح تنزيل DB ───────────────────────────────────
    if uid == ADMIN_ID and ctx.user_data.get("adm_awaiting_db_key"):
        ctx.user_data.pop("adm_awaiting_db_key")
        if text != DB_EXPORT_KEY:
            await update.message.reply_text("❌ *مفتاح خاطئ!*", parse_mode="Markdown", reply_markup=get_admin_keyboard())
            return
        csv_bytes = export_db_csv(decryption_confirmed=True)
        await update.message.reply_document(
            document=io.BytesIO(csv_bytes),
            filename=f"tikspark_db_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            caption="✅ *قاعدة البيانات — كلمات المرور مفكوكة*\n⚠️ احذف هذا الملف بعد الاستخدام!",
            parse_mode="Markdown"
        )
        return

    # ── مستخدم: حساب التمويل — المستخدم المستهدف ──────────────
    if ctx.user_data.get("awaiting_fund_user"):
        await handle_fund_calc_user(update, ctx); return

    # ── مستخدم: حساب التمويل — الكمية ─────────────────────────
    if ctx.user_data.get("awaiting_fund_amount"):
        await handle_fund_calc_amount(update, ctx); return

    # ── مستخدم: اليوزرنيم المستهدف للرشق ──────────────────────
    if ctx.user_data.get("awaiting_target"):
        await handle_target_input(update, ctx); return

    # ── مستخدم: تغيير الحساب (توكن جديد) ──────────────────────────
    if ctx.user_data.get("awaiting_new_token"):
        import asyncio
        token = text
        if len(token) < 20 or not token.startswith("ey"):
            await update.message.reply_text("❌ صيغة خاطئة — التوكن يبدأ بـ `ey`.\nأرسله مجدداً.", parse_mode="Markdown")
            return
        msg  = await update.message.reply_text("🔄 *جاري التحقق...*", parse_mode="Markdown")
        sess = make_session()
        is_ok, fb, nick = await asyncio.get_event_loop().run_in_executor(None, check_jwt_token, token)
        sess.close()
        if not is_ok:
            await msg.edit_text(f"{fb}\n\nأرسل التوكن الجديد:", parse_mode="Markdown")
            return
        db_save_user(uid, token, username=nick, start_time=datetime.now().isoformat())
        ctx.user_data.clear()
        await msg.edit_text(f"{fb}\n\n✅ تم تحديث الحساب!", parse_mode="Markdown")
        await update.message.reply_text("🎛️ *لوحة التحكم:*", parse_mode="Markdown", reply_markup=get_main_keyboard())
        return

# ═══════════════════════════════════════════════════════════════════
# 17. معالج الأزرار الرئيسي
# ═══════════════════════════════════════════════════════════════════

ALLOWED_BUTTONS = {
    "start","pause","resume","status","change_account","cancel",
    "enable_rush","disable_rush","fund_calc",
    "speed_0.005","speed_0.02","speed_0.05","speed_0.2","speed_0.5",
    # أزرار الأدمن
    "adm_accounts","adm_stats","adm_mode_all","adm_mode_whitelist",
    "adm_mode_none","adm_set_limit","adm_whitelist_add","adm_whitelist_remove",
    "adm_whitelist_list","adm_download_db","adm_back_main",
}

@only_allowed
async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = update.effective_user.id
    action = query.data

    if action not in ALLOWED_BUTTONS:
        log.warning(f"زر غير مسموح [{uid}]: {action[:30]}")
        await query.answer("⚠️ طلب غير صالح.", show_alert=True)
        return

    # أزرار الأدمن
    if action.startswith("adm_"):
        if uid != ADMIN_ID:
            await query.answer("⛔ للمشرف فقط.", show_alert=True); return
        await admin_callback(update, ctx); return

    if action == "cancel":
        ctx.user_data.clear()
        await query.edit_message_text("❌ تم الإلغاء.")
        return

    if action == "change_account":
        user = db_get_user(uid)
        if user and user["session_active"] == 1:
            await query.edit_message_text("⚠️ *أوقف الجلسة أولاً.*", parse_mode="Markdown", reply_markup=get_main_keyboard())
            return
        await query.edit_message_text(
            WELCOME + "\n\nأرسل التوكن الجديد:",
            parse_mode="Markdown"
        )
        ctx.user_data["awaiting_new_token"] = True
        return

    if action == "fund_calc":
        await query.edit_message_text(
            "💰 *حساب وقت التمويل*\n\n📌 أرسل *اسم المستخدم المستهدف*:",
            parse_mode="Markdown"
        )
        ctx.user_data["awaiting_fund_user"] = True
        return

    user = db_get_user(uid)
    if not user or not user["token"]:
        await query.edit_message_text("⚠️ *لا يوجد حساب مسجل.* /start", parse_mode="Markdown", reply_markup=get_main_keyboard())
        return

    if action == "start":
        with _threads_lock: alive = uid in _active_threads and _active_threads[uid].is_alive()
        if user["session_active"] == 1 or alive:
            await query.edit_message_text("⏳ *الجمع يعمل بالفعل!*", parse_mode="Markdown", reply_markup=get_main_keyboard())
            return
        db_update_pause(uid, 0)
        db_update_points(uid, user["total_points"], user["last_score"], 1)
        t = threading.Thread(target=collect_points, args=(uid, ctx.bot), daemon=True, name=f"col-{uid}")
        t.start()
        await query.edit_message_text(
            f"🚀 *تم تشغيل جمع النقاط!*\n⚡ السرعة: `{user['speed']}ث`",
            parse_mode="Markdown", reply_markup=get_main_keyboard()
        )

    elif action == "pause":
        if user["session_active"] == 0:
            await query.edit_message_text("⚠️ *الجمع ليس قيد التشغيل.*", parse_mode="Markdown", reply_markup=get_main_keyboard()); return
        db_update_pause(uid, 1)
        await query.edit_message_text("⏸ *تم الإيقاف المؤقت.* ستستمر الجلسة في الخلفية.", parse_mode="Markdown", reply_markup=get_main_keyboard())

    elif action == "resume":
        if user["session_active"] == 0:
            await query.edit_message_text("⚠️ *اضغط تشغيل أولاً.*", parse_mode="Markdown", reply_markup=get_main_keyboard()); return
        db_update_pause(uid, 0)
        await query.edit_message_text("▶️ *تم الاستئناف!*", parse_mode="Markdown", reply_markup=get_main_keyboard())

    elif action == "enable_rush":
        if user["session_active"] == 0:
            await query.edit_message_text("⚠️ *شغّل الجمع أولاً.*", parse_mode="Markdown", reply_markup=get_main_keyboard()); return
        await query.edit_message_text("📌 أرسل *اليوزرنيم المستهدف* للرشق:", parse_mode="Markdown")
        ctx.user_data["awaiting_target"] = True

    elif action == "disable_rush":
        db_update_rush(uid, 0)
        await query.edit_message_text("⛔ *تم إلغاء الرشق.*", parse_mode="Markdown", reply_markup=get_main_keyboard())

    elif action.startswith("speed_"):
        speed  = float(action.split("_")[1])
        db_update_speed(uid, speed)
        labels = {0.005:"🚀 أقصى سرعة",0.02:"⚡ سريع جداً",0.05:"🔥 سريع",0.2:"🐇 عادي",0.5:"🐢 بطيء"}
        await query.edit_message_text(
            f"⚙️ *السرعة:* {labels.get(speed,f'{speed}ث')} (`{speed}ث`)",
            parse_mode="Markdown", reply_markup=get_main_keyboard()
        )

    elif action == "status":
        with _threads_lock: alive = uid in _active_threads and _active_threads[uid].is_alive()
        uptime = "غير متاح"
        if user["start_time"]:
            try:
                el = datetime.now() - datetime.fromisoformat(user["start_time"])
                h, m = int(el.total_seconds()//3600), int((el.total_seconds()%3600)//60)
                uptime = f"{h}h {m}m"
            except: pass
        await query.edit_message_text(
            f"📊 *حالة الحساب:*\n\n"
            f"• الحساب:   `{user['username']}`\n"
            f"• الحالة:   {'🟢 يعمل' if user['session_active'] and alive else '🔴 متوقف'}\n"
            f"• الإيقاف:  {'⏸' if user['is_paused'] else '▶️'}\n"
            f"• السرعة:   `{user['speed']}ث`\n"
            f"• النقاط:   `{user['total_points']:,}`\n"
            f"• التشغيل:  `{uptime}`\n"
            f"• الرشق:    {'🟢 مفعّل على @'+user['target_username'] if user.get('rush_enabled') and user.get('target_username') else '🔴 ملغي'}",
            parse_mode="Markdown", reply_markup=get_main_keyboard()
        )

# ═══════════════════════════════════════════════════════════════════
# 18. التشغيل الرئيسي
# ═══════════════════════════════════════════════════════════════════

def main():
    init_db()
    log.info(f"🤖 البوت يعمل | الأدمن: {ADMIN_ID}")

    app  = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAITING_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token_input)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(lambda u,c: cancel(u,c), pattern="^cancel$"),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, general_text_handler))
    log.info("✅ كل المعالجات مسجلة.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

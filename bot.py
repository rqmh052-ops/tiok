#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, json, logging, threading, asyncio, sqlite3, shutil
from datetime import datetime
from io import BytesIO

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# ═══════════════════════════════════════════════
#  ⚙️  إعدادات البوت
# ═══════════════════════════════════════════════
BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "8411663176:AAEsI2yAj-mQQ6uspRrOI_yJPbCYEtjBbwo")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "8287678319"))
API_URL    = "https://api.tikspark.xyz/graphql"
DB_PATH    = "tikspark.db"
BACKUP_DIR = "db_backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
#  🔌  قاعدة البيانات
# ═══════════════════════════════════════════════
def db_connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db_connect()
    c = conn.cursor()
    c.executescript("""
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS allowed_users (
        user_id    INTEGER PRIMARY KEY,
        username   TEXT,
        added_at   TEXT DEFAULT (datetime('now')),
        added_by   INTEGER,
        active     INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        tiktok_user TEXT NOT NULL,
        token       TEXT,
        started_at  TEXT DEFAULT (datetime('now')),
        ended_at    TEXT,
        total_score INTEGER DEFAULT 0,
        task_count  INTEGER DEFAULT 0,
        status      TEXT DEFAULT 'running',
        speed       REAL DEFAULT 0.2
    );

    CREATE TABLE IF NOT EXISTS headers (
        htype TEXT PRIMARY KEY,
        data  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS speed_presets (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        value REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS db_backups_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        sent_at    TEXT DEFAULT (datetime('now')),
        seq        INTEGER DEFAULT 0,
        file_size  INTEGER DEFAULT 0
    );
    """)

    # السرعات الافتراضية
    existing = c.execute("SELECT COUNT(*) FROM speed_presets").fetchone()[0]
    if existing == 0:
        defaults = [
            ("🐢 بطيء",        5.0),
            ("🚶 عادي",        1.0),
            ("🏃 سريع",        0.2),
            ("⚡ سريع جداً",   0.05),
            ("🚀 صاروخ",       0.005),
        ]
        c.executemany("INSERT INTO speed_presets (label,value) VALUES (?,?)", defaults)

    # الهيدرز الافتراضية
    for htype, hdata in [
        ("login",   json.dumps(LOGIN_HEADERS_DEFAULT)),
        ("fetch",   json.dumps(FETCH_HEADERS_DEFAULT)),
        ("action",  json.dumps(ACTION_HEADERS_DEFAULT)),
    ]:
        c.execute("INSERT OR IGNORE INTO headers (htype,data) VALUES (?,?)", (htype, hdata))

    conn.commit()
    conn.close()

def db_setting(key, value=None):
    conn = db_connect()
    if value is None:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else None
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()

def db_get_headers(htype):
    conn = db_connect()
    row = conn.execute("SELECT data FROM headers WHERE htype=?", (htype,)).fetchone()
    conn.close()
    return json.loads(row["data"]) if row else {}

def db_save_headers(htype, hdata):
    conn = db_connect()
    conn.execute("INSERT OR REPLACE INTO headers (htype,data) VALUES (?,?)",
                 (htype, json.dumps(hdata)))
    conn.commit()
    conn.close()

def db_is_allowed(user_id):
    if user_id == ADMIN_ID:
        return True
    conn = db_connect()
    row = conn.execute(
        "SELECT active FROM allowed_users WHERE user_id=? AND active=1", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None

def db_add_user(user_id, username, added_by):
    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO allowed_users (user_id,username,added_by,active) VALUES (?,?,?,1)",
        (user_id, username, added_by)
    )
    conn.commit()
    conn.close()

def db_remove_user(user_id):
    conn = db_connect()
    conn.execute("UPDATE allowed_users SET active=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def db_list_users():
    conn = db_connect()
    rows = conn.execute(
        "SELECT user_id, username, added_at FROM allowed_users WHERE active=1"
    ).fetchall()
    conn.close()
    return rows

def db_get_speeds():
    conn = db_connect()
    rows = conn.execute("SELECT id, label, value FROM speed_presets ORDER BY value DESC").fetchall()
    conn.close()
    return rows

def db_add_speed(label, value):
    conn = db_connect()
    conn.execute("INSERT INTO speed_presets (label,value) VALUES (?,?)", (label, value))
    conn.commit()
    conn.close()

def db_delete_speed(sid):
    conn = db_connect()
    conn.execute("DELETE FROM speed_presets WHERE id=?", (sid,))
    conn.commit()
    conn.close()

def db_save_session(user_id, tiktok_user, token, speed):
    conn = db_connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO sessions (user_id,tiktok_user,token,speed) VALUES (?,?,?,?)",
        (user_id, tiktok_user, token, speed)
    )
    sid = c.lastrowid
    conn.commit()
    conn.close()
    return sid

def db_update_session(sid, score, count, status=None):
    conn = db_connect()
    if status:
        conn.execute(
            "UPDATE sessions SET total_score=?,task_count=?,status=?,ended_at=datetime('now') WHERE id=?",
            (score, count, status, sid)
        )
    else:
        conn.execute(
            "UPDATE sessions SET total_score=?,task_count=? WHERE id=?",
            (score, count, sid)
        )
    conn.commit()
    conn.close()

def db_list_sessions(user_id=None):
    conn = db_connect()
    if user_id:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY id DESC LIMIT 30"
        ).fetchall()
    conn.close()
    return rows

def db_backup_log(seq, size):
    conn = db_connect()
    conn.execute("INSERT INTO db_backups_log (seq,file_size) VALUES (?,?)", (seq, size))
    # احتفظ فقط بآخر سجل (الرقم فقط للعرض)
    conn.commit()
    conn.close()

def db_get_backup_seq():
    conn = db_connect()
    row = conn.execute("SELECT MAX(seq) as s FROM db_backups_log").fetchone()
    conn.close()
    return (row["s"] or 0) + 1

# ═══════════════════════════════════════════════
#  📡  الهيدرز الافتراضية (تُقرأ من DB عند التشغيل)
# ═══════════════════════════════════════════════
LOGIN_HEADERS_DEFAULT = {
    "User-Agent": "okhttp/4.12.0",
    "Accept": "multipart/mixed; deferSpec=20220824, application/json",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "x-apollo-operation-id": "3522613813036d73817b2715e67743f8d23d7a85ad08b7e12aa3b29a24a17c43",
    "x-apollo-operation-name": "LoginAccount",
    "x-language": "ar",
    "x-app-name": "com.dev.vidspark",
    "x-device-info": '{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',
    "x-app-sig":   "6024ed0395f78f2d27dc9823e678222d4bf0a99210975f20371c8aa15703f699",
    "x-app-ts":    "1782921077469",
    "x-app-nonce": "a9ca074d062b43a4"
}
FETCH_HEADERS_DEFAULT = {
    "User-Agent": "okhttp/4.12.0",
    "Accept": "multipart/mixed; deferSpec=20220824, application/json",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "x-apollo-operation-id": "c2ca4b87e63f30f2cca10e5867d17ea0f1712e96e716a60513f68758b2256185",
    "x-apollo-operation-name": "FetchOrders",
    "x-language": "ar",
    "x-app-name": "com.dev.vidspark",
    "x-csrf-token": "1782443248827:bf0ad4b105a1f6bcfca393d2f36fbe0f9cf690d37ba398113266884c14017d39",
    "x-device-info": '{"d":"30666439303936303830366134393632","n":"5869616f6d69203233313144524b343847","o":"16","t":"d","v":"2.2.0","s":"0,0"}',
    "x-app-sig":   "2998306f19b3a98732a7150a785204d487ae22cb530a0bf4b1ff77a380ad7cd4",
    "x-app-ts":    "1782443248827",
    "x-app-nonce": "18b79765e8e0458c"
}
ACTION_HEADERS_DEFAULT = {
    "User-Agent": "okhttp/4.12.0",
    "Accept": "multipart/mixed; deferSpec=20220824, application/json",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "x-apollo-operation-id": "ddfbb49865193fd38840a34b92139f1759a71331e374bb1254f8e2352630e8f2",
    "x-apollo-operation-name": "RecordFailedOrder",
    "x-language": "ar",
    "x-app-name": "com.dev.vidspark",
    "x-csrf-token": "1782443501268:86a814a8285234821d27485112d451696809e830e3f71545715299aa5f2373e4",
    "x-device-info": '{"d":"30666439303936303830366134393632","n":"5869616f6d69203233313144524b343847","o":"16","t":"d","v":"2.2.0","s":"0,0"}',
    "x-app-sig":   "40a5102e6744f3bddca39e1ce6bb99ce942e20fe9382ba2463e1401d897eff43",
    "x-app-ts":    "1782443501268",
    "x-app-nonce": "e69a0252b53843e4"
}

# ═══════════════════════════════════════════════
#  📦  دوال API (محمية - لا تُعدَّل)
# ═══════════════════════════════════════════════
def login_account(username, password):
    try:
        headers = db_get_headers("login")
        payload = {
            "operationName": "LoginAccount",
            "variables": {
                "data": {
                    "id": "", "uniqueId": username, "nickname": "",
                    "avatarMedium": "https://p16-common-sign.tiktokcdn.com/tos-alisg-avt-0068/709ff2826cb78c4b7ee81b8c69157606~tplv-tiktokx-cropcenter:720:720.webp?dr=14579&refresh_token=9b28df9e&x-expires=1783090800&x-signature=3HXYlIl2Fdv%2Fkl7xKX4SUO7SM1I%3D&t=4d5b0474&ps=13740610&shp=a5d48078&shcp=2472a6c6&idc=my2",
                    "followerCount": 31, "followingCount": 87,
                    "videoCount": 0, "privateAccount": False,
                    "diggCount": 0, "authMethod": "local",
                    "password": password
                }
            },
            "query": """
            mutation LoginAccount($data: TiktokInfo) {
                loginTiktok(data: $data) {
                    accessToken refreshToken
                    user { __typename ...UserFields }
                }
            }
            fragment UserFields on User {
                _id tiktokId nickname email score diggCount followerCount
                followingCount friendCount isMembershipExpired heartCount
                username avatar banned vip vipExpiresAt authMethod
                isSubscription allowd referralCode referralCount referredBy
            }
            """
        }
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=12)
        if resp.status_code != 200:
            return False, f"❌ كود {resp.status_code}", ""
        data = resp.json()
        errs = data.get("errors")
        if errs:
            return False, f"❌ {errs[0].get('message','خطأ')}", ""
        token = data.get("data",{}).get("loginTiktok",{}).get("accessToken")
        if not token:
            return False, "❌ لم يُرجع الخادم توكناً", ""
        return True, "✅ تم تسجيل الدخول", token
    except Exception as e:
        return False, f"❌ {e}", ""

def fetch_order_id(token):
    try:
        headers = db_get_headers("fetch")
        headers["token"] = token
        payload = {
            "operationName": "FetchOrders",
            "variables": {"page": 2},
            "query": "query FetchOrders($page: Int!) { getOrders(page: $page) { _id } }"
        }
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        orders = data.get("data",{}).get("getOrders",[])
        return orders[0]["_id"] if orders else None
    except Exception:
        return None

def execute_order(token, order_id):
    try:
        headers = db_get_headers("action")
        headers["token"] = token
        payload = {
            "operationName": "ActionOrder",
            "variables": {
                "orderId": order_id,
                "validationData": {
                    "attempts": 1,
                    "initialNumber": 2953.0,
                    "timeSpent": 31883.0
                }
            },
            "query": """
            mutation ActionOrder($orderId: ID!, $validationData: ValidationDataInput!) {
                actionOrder(orderId: $orderId, validationData: $validationData) {
                    score
                    taskProgress { count startTime taskProgressLimit }
                }
            }
            """
        }
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=12)
        if resp.status_code != 200:
            return None
        data = resp.json()
        errs = data.get("errors")
        if errs:
            return {"error": errs[0].get("message","")}
        return data.get("data",{}).get("actionOrder",{})
    except Exception:
        return None

# ═══════════════════════════════════════════════
#  🧵  كلاس الجلسة + Loop
# ═══════════════════════════════════════════════
class Session:
    def __init__(self, sid, db_id, user_id, tiktok_user, token, speed):
        self.id          = sid
        self.db_id       = db_id
        self.user_id     = user_id
        self.tiktok_user = tiktok_user
        self.token       = token
        self.speed       = speed
        self.running     = False
        self.stop_flag   = False
        self.thread      = None
        self.total_score = 0
        self.task_count  = 0
        self.errors      = 0
        self.last_score  = 0
        self.start_time  = None
        self.status_msg_id = None

sessions: dict[int, Session] = {}
sessions_lock = threading.Lock()
session_counter = 0

def collector_loop(session: Session, bot, chat_id, loop):
    session.running    = True
    session.stop_flag  = False
    session.start_time = datetime.now()
    token      = session.token
    last_score = 0
    cons_err   = 0

    def safe_send(text, markup=None):
        coro = _edit_or_send(bot, chat_id, session, text, markup)
        asyncio.run_coroutine_threadsafe(coro, loop)

    safe_send("🚀 *جارٍ الجمع...*\n⏳ انتظر أول تحديث.")

    while not session.stop_flag:
        try:
            order_id = fetch_order_id(token)
            if not order_id:
                time.sleep(0.5)
                continue

            result = execute_order(token, order_id)
            if result is None:
                cons_err += 1
                if cons_err >= 5:
                    safe_send("🛑 *توقف — أخطاء متتالية.*")
                    break
                time.sleep(2)
                continue

            if isinstance(result, dict) and "error" in result:
                if "Rate limit" in result["error"]:
                    time.sleep(0.8)
                    continue
                cons_err += 1
                if cons_err >= 5:
                    safe_send(f"⚠️ *توقف — خطأ:* {result['error']}")
                    break
                time.sleep(1.5)
                continue

            cons_err = 0
            score = result.get("score", last_score)
            prog  = result.get("taskProgress", {})
            count = prog.get("count", session.task_count)

            if score < last_score:
                score = last_score
            gained     = max(0, score - last_score)
            last_score = score
            session.last_score  = score
            session.total_score = score
            session.task_count  = count

            db_update_session(session.db_id, score, count)

            bar    = "█" * min(count, 10) + "░" * (10 - min(count, 10))
            badge  = f"🎉 +{gained}" if gained > 0 else "⏳"
            elapsed = (datetime.now() - session.start_time).total_seconds()
            rate   = count / elapsed if elapsed > 0 else 0

            text = (
                f"🏃 *جلسة نشطة — {session.tiktok_user}*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{badge}\n"
                f"🏆 النقاط: `{score:,}`\n"
                f"📦 المهام: `{count}` `[{bar}]`\n"
                f"⚡ السرعة: `{session.speed}ث`\n"
                f"📈 المعدل: `{rate:.1f}` مهمة/ث\n"
                f"⏱ المدة: `{int(elapsed//60)}د {int(elapsed%60)}ث`"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⏹ إيقاف هذه الجلسة", callback_data=f"stop_{session.id}")
            ]])
            safe_send(text, kb)
            time.sleep(session.speed)

        except Exception as e:
            cons_err += 1
            log.error(f"collector_loop error: {e}")
            if cons_err >= 5:
                safe_send("💥 توقف بسبب خطأ غير متوقع.")
                break
            time.sleep(2)

    session.running = False
    db_update_session(session.db_id, session.total_score, session.task_count, "stopped")
    elapsed = (datetime.now() - session.start_time).total_seconds()
    safe_send(
        f"🏁 *انتهت الجلسة*\n"
        f"👤 {session.tiktok_user}\n"
        f"🏆 النهائي: `{session.total_score:,}`\n"
        f"📦 المهام: `{session.task_count}`\n"
        f"⏱ المدة: `{int(elapsed//60)}د {int(elapsed%60)}ث`"
    )

async def _edit_or_send(bot, chat_id, session: Session, text, markup=None):
    try:
        kwargs = dict(chat_id=chat_id, text=text, parse_mode="Markdown")
        if markup:
            kwargs["reply_markup"] = markup
        if session.status_msg_id:
            await bot.edit_message_text(message_id=session.status_msg_id, **kwargs)
        else:
            msg = await bot.send_message(**kwargs)
            session.status_msg_id = msg.message_id
    except Exception:
        pass

# ═══════════════════════════════════════════════
#  🎨  بناء لوحات الأزرار
# ═══════════════════════════════════════════════
def kb_main(user_id):
    is_admin = (user_id == ADMIN_ID)
    rows = [
        [InlineKeyboardButton("➕ جلسة جديدة",       callback_data="new_session"),
         InlineKeyboardButton("📊 حالتي",             callback_data="my_status")],
        [InlineKeyboardButton("📋 جلساتي",            callback_data="my_sessions"),
         InlineKeyboardButton("⚡ تغيير السرعة",       callback_data="speed_menu")],
    ]
    if is_admin:
        rows.append([
            InlineKeyboardButton("🛡 لوحة الإدارة",   callback_data="admin_panel"),
        ])
    return InlineKeyboardMarkup(rows)

def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 إدارة المستخدمين",  callback_data="adm_users"),
         InlineKeyboardButton("📋 كل الجلسات",        callback_data="adm_sessions")],
        [InlineKeyboardButton("🔧 تحديث الهيدرز",     callback_data="adm_headers"),
         InlineKeyboardButton("⚡ إدارة السرعات",      callback_data="adm_speeds")],
        [InlineKeyboardButton("💾 تنزيل قاعدة البيانات", callback_data="adm_dl_db"),
         InlineKeyboardButton("📤 رفع قاعدة البيانات",   callback_data="adm_ul_db")],
        [InlineKeyboardButton("⏹ إيقاف كل الجلسات",  callback_data="adm_stop_all")],
        [InlineKeyboardButton("🔙 الرئيسية",           callback_data="back_main")],
    ])

def kb_speed(user_id):
    speeds = db_get_speeds()
    rows = []
    row  = []
    for s in speeds:
        row.append(InlineKeyboardButton(s["label"], callback_data=f"speed_{s['id']}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton("➕ إضافة سرعة",   callback_data="adm_add_speed"),
                     InlineKeyboardButton("🗑 حذف سرعة",     callback_data="adm_del_speed_menu")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def kb_back(target="back_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=target)]])

def kb_users(users):
    rows = []
    for u in users:
        rows.append([InlineKeyboardButton(
            f"🚫 حذف {u['username'] or u['user_id']}",
            callback_data=f"adm_rm_user_{u['user_id']}"
        )])
    rows.append([InlineKeyboardButton("➕ إضافة مستخدم", callback_data="adm_add_user")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)

def kb_del_speed(speeds):
    rows = []
    for s in speeds:
        rows.append([InlineKeyboardButton(
            f"🗑 {s['label']} ({s['value']}ث)",
            callback_data=f"adm_del_speed_{s['id']}"
        )])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="adm_speeds")])
    return InlineKeyboardMarkup(rows)

# ═══════════════════════════════════════════════
#  🤖  معالجات البوت
# ═══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_allowed(uid):
        await update.message.reply_text("⛔️ ليس لديك صلاحية لاستخدام هذا البوت.")
        return
    name = update.effective_user.first_name or "مستخدم"
    await update.message.reply_text(
        f"👋 أهلاً *{name}*!\n\n"
        f"🤖 *بوت TikSpark* جاهز للعمل.\n"
        f"اختر من الأزرار أدناه:",
        parse_mode="Markdown",
        reply_markup=kb_main(uid)
    )

async def btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not db_is_allowed(uid):
        await q.answer("⛔️ غير مصرح", show_alert=True); return

    data    = q.data
    chat_id = q.message.chat_id
    loop    = asyncio.get_event_loop()

    # ── رجوع للرئيسية ──────────────────────────
    if data == "back_main":
        await q.edit_message_text(
            "🏠 *الرئيسية*", parse_mode="Markdown", reply_markup=kb_main(uid)
        )
        return

    # ── قائمة السرعات ──────────────────────────
    if data == "speed_menu":
        await q.edit_message_text(
            "⚡ *اختر سرعة الجمع:*\n_(تُطبَّق على جلستك التالية أو الحالية)_",
            parse_mode="Markdown", reply_markup=kb_speed(uid)
        )
        return

    if data.startswith("speed_") and not data.startswith("speed_menu"):
        sid_or_id = data[6:]
        try:
            preset_id = int(sid_or_id)
            conn  = db_connect()
            row   = conn.execute("SELECT label,value FROM speed_presets WHERE id=?", (preset_id,)).fetchone()
            conn.close()
            if not row:
                await q.answer("❌ سرعة غير موجودة", show_alert=True); return
            speed_val   = row["value"]
            speed_label = row["label"]
            ctx.user_data["selected_speed"] = speed_val
            # تطبيق على الجلسات النشطة لهذا المستخدم
            with sessions_lock:
                for s in sessions.values():
                    if s.user_id == uid and s.running:
                        s.speed = speed_val
            await q.edit_message_text(
                f"✅ السرعة: *{speed_label}* `({speed_val}ث)`",
                parse_mode="Markdown", reply_markup=kb_speed(uid)
            )
        except ValueError:
            await q.answer("❌ خطأ", show_alert=True)
        return

    # ── جلسة جديدة ─────────────────────────────
    if data == "new_session":
        ctx.user_data["awaiting"] = "username"
        await q.edit_message_text(
            "👤 أرسل *اسم المستخدم* (TikTok):",
            parse_mode="Markdown", reply_markup=kb_back()
        )
        return

    # ── إيقاف جلسة بعينها ───────────────────────
    if data.startswith("stop_"):
        try:
            target_id = int(data[5:])
        except ValueError:
            await q.answer("❌ خطأ", show_alert=True); return
        with sessions_lock:
            s = sessions.get(target_id)
        if not s:
            await q.answer("⚠️ الجلسة غير موجودة", show_alert=True); return
        if s.user_id != uid and uid != ADMIN_ID:
            await q.answer("⛔️ ليست جلستك", show_alert=True); return
        s.stop_flag = True
        await q.edit_message_text(
            f"⏹ *جارٍ إيقاف* `{s.tiktok_user}`...",
            parse_mode="Markdown"
        )
        return

    # ── حالة جلساتي ────────────────────────────
    if data == "my_status":
        with sessions_lock:
            active = [s for s in sessions.values() if s.user_id == uid and s.running]
        if not active:
            await q.edit_message_text("📭 لا توجد جلسات نشطة لك.", reply_markup=kb_main(uid))
            return
        lines = []
        btns  = []
        for s in active:
            elapsed = (datetime.now() - s.start_time).total_seconds() if s.start_time else 0
            lines.append(
                f"🟢 *{s.tiktok_user}*\n"
                f"   🏆 `{s.total_score:,}` | 📦 `{s.task_count}` | ⚡ `{s.speed}ث`\n"
                f"   ⏱ `{int(elapsed//60)}د {int(elapsed%60)}ث`"
            )
            btns.append([InlineKeyboardButton(
                f"⏹ إيقاف {s.tiktok_user}", callback_data=f"stop_{s.id}"
            )])
        btns.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
        await q.edit_message_text(
            "📊 *جلساتك النشطة:*\n\n" + "\n\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(btns)
        )
        return

    # ── قائمة جلساتي (كل الجلسات) ──────────────
    if data == "my_sessions":
        rows = db_list_sessions(uid)
        if not rows:
            await q.edit_message_text("📭 لا توجد جلسات سابقة.", reply_markup=kb_main(uid))
            return
        text = "📋 *جلساتك الأخيرة:*\n\n"
        for r in rows:
            icon = "🟢" if r["status"] == "running" else "🔴"
            text += f"{icon} `{r['tiktok_user']}` — 🏆 `{r['total_score']:,}` — 📦 `{r['task_count']}`\n"
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb_back())
        return

    # ═══════ لوحة الإدارة ═══════════════════════
    if data == "admin_panel":
        if uid != ADMIN_ID:
            await q.answer("⛔️ أدمن فقط", show_alert=True); return
        with sessions_lock:
            active_count = sum(1 for s in sessions.values() if s.running)
        total_users = len(db_list_users())
        await q.edit_message_text(
            f"🛡 *لوحة الإدارة*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🟢 الجلسات النشطة: `{active_count}`\n"
            f"👥 المستخدمون المصرح لهم: `{total_users}`",
            parse_mode="Markdown",
            reply_markup=kb_admin()
        )
        return

    if data == "adm_users":
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        users = db_list_users()
        text  = "👥 *المستخدمون المصرح لهم:*\n\n"
        if not users:
            text += "_(لا يوجد مستخدمون مضافون)_"
        for u in users:
            text += f"• `{u['user_id']}` — @{u['username'] or '—'} — منذ {u['added_at'][:10]}\n"
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb_users(users))
        return

    if data == "adm_add_user":
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        ctx.user_data["awaiting"] = "adm_add_user"
        await q.edit_message_text(
            "➕ أرسل *معرف المستخدم (ID)* أو @اسمه لإضافته:",
            parse_mode="Markdown", reply_markup=kb_back("adm_users")
        )
        return

    if data.startswith("adm_rm_user_"):
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        target_uid = int(data[12:])
        db_remove_user(target_uid)
        await q.answer("✅ تمت إزالة المستخدم")
        # أعد عرض القائمة
        users = db_list_users()
        text  = "👥 *المستخدمون:*\n"
        for u in users:
            text += f"• `{u['user_id']}` — @{u['username'] or '—'}\n"
        await q.edit_message_text(text or "_(فارغ)_", parse_mode="Markdown",
                                  reply_markup=kb_users(users))
        return

    if data == "adm_sessions":
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        rows = db_list_sessions()
        text = "📋 *آخر الجلسات (كل المستخدمين):*\n\n"
        for r in rows:
            icon = "🟢" if r["status"] == "running" else "🔴"
            text += f"{icon} `{r['tiktok_user']}` — 🏆`{r['total_score']:,}` — uid:`{r['user_id']}`\n"
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb_back("admin_panel"))
        return

    if data == "adm_stop_all":
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        count = 0
        with sessions_lock:
            for s in sessions.values():
                if s.running:
                    s.stop_flag = True
                    count += 1
        await q.edit_message_text(
            f"⏹ *أُوقفت {count} جلسة.*",
            parse_mode="Markdown", reply_markup=kb_back("admin_panel")
        )
        return

    # ── الهيدرز ────────────────────────────────
    if data == "adm_headers":
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        ctx.user_data["awaiting"] = "adm_headers"
        await q.edit_message_text(
            "🔧 *تحديث الهيدرز*\n\n"
            "أرسل JSON يحتوي على المفاتيح المراد تحديثها، مثال:\n"
            "```\n"
            '{"x-app-sig":"...","x-app-ts":"...","x-app-nonce":"...","x-csrf-token":"..."}'
            "\n```\n"
            "سيتم تحديث الثلاث مجموعات تلقائياً.",
            parse_mode="Markdown", reply_markup=kb_back("admin_panel")
        )
        return

    # ── السرعات (إدارة) ────────────────────────
    if data == "adm_speeds":
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        speeds = db_get_speeds()
        text   = "⚡ *السرعات المتاحة:*\n\n"
        for s in speeds:
            text += f"• `{s['id']}` — {s['label']} — `{s['value']}ث`\n"
        rows = [
            [InlineKeyboardButton("➕ إضافة سرعة",        callback_data="adm_add_speed"),
             InlineKeyboardButton("🗑 حذف سرعة",           callback_data="adm_del_speed_menu")],
            [InlineKeyboardButton("🔙 رجوع",               callback_data="admin_panel")],
        ]
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "adm_add_speed":
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        ctx.user_data["awaiting"] = "adm_add_speed_label"
        await q.edit_message_text(
            "➕ أرسل *اسم السرعة* (مثال: `🚀 صاروخ 2`):",
            parse_mode="Markdown", reply_markup=kb_back("adm_speeds")
        )
        return

    if data == "adm_del_speed_menu":
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        speeds = db_get_speeds()
        await q.edit_message_text(
            "🗑 *اختر السرعة للحذف:*",
            parse_mode="Markdown", reply_markup=kb_del_speed(speeds)
        )
        return

    if data.startswith("adm_del_speed_"):
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        try:
            spid = int(data[14:])
        except ValueError:
            await q.answer("❌ خطأ"); return
        db_delete_speed(spid)
        await q.answer("✅ حُذفت")
        speeds = db_get_speeds()
        await q.edit_message_text("🗑 *السرعات المتبقية:*\n" + "\n".join(f"• {s['label']}" for s in speeds),
                                  parse_mode="Markdown", reply_markup=kb_del_speed(speeds))
        return

    # ── تنزيل قاعدة البيانات ───────────────────
    if data == "adm_dl_db":
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        await q.answer("⏳ جارٍ الإرسال...")
        await send_db_to_admin(ctx.bot, chat_id, "طلب يدوي")
        return

    # ── رفع قاعدة البيانات ─────────────────────
    if data == "adm_ul_db":
        if uid != ADMIN_ID:
            await q.answer("⛔️", show_alert=True); return
        ctx.user_data["awaiting"] = "adm_upload_db"
        await q.edit_message_text(
            "📤 *رفع قاعدة البيانات*\n\n"
            "أرسل ملف `.db` وسيتم استبدال القاعدة الحالية.\n"
            "⚠️ سيتم إيقاف جميع الجلسات النشطة أولاً.",
            parse_mode="Markdown", reply_markup=kb_back("admin_panel")
        )
        return

    await q.answer()

# ═══════════════════════════════════════════════
#  💬  معالج الرسائل النصية
# ═══════════════════════════════════════════════
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    if not db_is_allowed(uid):
        return
    text     = update.message.text.strip()
    awaiting = ctx.user_data.get("awaiting")

    # ── اسم المستخدم TikTok ────────────────────
    if awaiting == "username":
        ctx.user_data["tiktok_username"] = text
        ctx.user_data["awaiting"]        = "password"
        await update.message.reply_text("🔑 أرسل *كلمة المرور*:", parse_mode="Markdown")
        return

    # ── كلمة المرور → تسجيل دخول وبدء جلسة ──
    if awaiting == "password":
        tiktok_user = ctx.user_data.pop("tiktok_username", "")
        password    = text
        ctx.user_data.pop("awaiting", None)
        msg = await update.message.reply_text("🔄 *جارٍ تسجيل الدخول...*", parse_mode="Markdown")
        ok, info, token = login_account(tiktok_user, password)
        if not ok:
            await msg.edit_text(f"{info}\nحاول مرة أخرى.", reply_markup=kb_main(uid))
            return
        speed = ctx.user_data.get("selected_speed", 0.2)
        db_id = db_save_session(uid, tiktok_user, token, speed)
        global session_counter
        with sessions_lock:
            session_counter += 1
            sid     = session_counter
            session = Session(sid, db_id, uid, tiktok_user, token, speed)
            sessions[sid] = session
        loop   = asyncio.get_event_loop()
        thread = threading.Thread(
            target=collector_loop,
            args=(session, ctx.bot, update.effective_chat.id, loop),
            daemon=True
        )
        thread.start()
        session.thread = thread
        await msg.edit_text(
            f"✅ *تم تسجيل الدخول* | بدأت الجلسة\n"
            f"👤 `{tiktok_user}` | ⚡ `{speed}ث`",
            parse_mode="Markdown", reply_markup=kb_main(uid)
        )
        return

    # ── هيدرز JSON ──────────────────────────────
    if awaiting == "adm_headers":
        if uid != ADMIN_ID:
            return
        ctx.user_data.pop("awaiting", None)
        try:
            new_h = json.loads(text)
        except json.JSONDecodeError as e:
            await update.message.reply_text(f"❌ JSON غير صالح: {e}", reply_markup=kb_admin())
            return
        keys = ["x-app-sig","x-app-ts","x-app-nonce","x-csrf-token","x-device-info",
                "x-apollo-operation-id","User-Agent","token"]
        updated = []
        for htype in ["login","fetch","action"]:
            hdata = db_get_headers(htype)
            for k in keys:
                if k in new_h:
                    hdata[k] = new_h[k]
                    if k not in updated:
                        updated.append(k)
            db_save_headers(htype, hdata)
        await update.message.reply_text(
            f"✅ *تم تحديث الهيدرز*\nالمفاتيح المحدَّثة: `{', '.join(updated)}`",
            parse_mode="Markdown", reply_markup=kb_admin()
        )
        return

    # ── إضافة مستخدم ────────────────────────────
    if awaiting == "adm_add_user":
        if uid != ADMIN_ID:
            return
        ctx.user_data.pop("awaiting", None)
        # قبول ID رقمي أو @username (يحتاج resolve - هنا نخزن كما هو)
        raw = text.lstrip("@")
        try:
            target_uid = int(raw)
            db_add_user(target_uid, "", uid)
            await update.message.reply_text(
                f"✅ أُضيف المستخدم `{target_uid}`",
                parse_mode="Markdown", reply_markup=kb_admin()
            )
        except ValueError:
            await update.message.reply_text(
                "⚠️ أرسل معرف رقمي (ID) فقط.\n"
                "يمكنك الحصول عليه من @userinfobot",
                reply_markup=kb_admin()
            )
        return

    # ── إضافة سرعة: الاسم ───────────────────────
    if awaiting == "adm_add_speed_label":
        if uid != ADMIN_ID:
            return
        ctx.user_data["new_speed_label"] = text
        ctx.user_data["awaiting"]        = "adm_add_speed_value"
        await update.message.reply_text(
            f"⚡ اسم السرعة: *{text}*\n\nأرسل الآن *القيمة بالثواني* (مثال: `0.01`):",
            parse_mode="Markdown"
        )
        return

    # ── إضافة سرعة: القيمة ──────────────────────
    if awaiting == "adm_add_speed_value":
        if uid != ADMIN_ID:
            return
        label = ctx.user_data.pop("new_speed_label", "جديد")
        ctx.user_data.pop("awaiting", None)
        try:
            val = float(text.replace(",","."))
            if val <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ قيمة غير صالحة.", reply_markup=kb_admin())
            return
        db_add_speed(label, val)
        await update.message.reply_text(
            f"✅ أُضيفت السرعة: *{label}* `({val}ث)`",
            parse_mode="Markdown", reply_markup=kb_admin()
        )
        return

# ═══════════════════════════════════════════════
#  📁  معالج الملفات (رفع قاعدة البيانات)
# ═══════════════════════════════════════════════
async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    awaiting = ctx.user_data.get("awaiting")
    if awaiting != "adm_upload_db":
        return
    ctx.user_data.pop("awaiting", None)

    doc = update.message.document
    if not doc or not doc.file_name.endswith(".db"):
        await update.message.reply_text("❌ أرسل ملف `.db` صالح.", reply_markup=kb_admin())
        return

    # إيقاف كل الجلسات
    with sessions_lock:
        for s in sessions.values():
            s.stop_flag = True
    await asyncio.sleep(1)

    # تنزيل الملف واستبدال DB
    try:
        file = await ctx.bot.get_file(doc.file_id)
        buf  = BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        backup_path = os.path.join(BACKUP_DIR, f"pre_upload_{int(time.time())}.db")
        shutil.copy2(DB_PATH, backup_path)
        with open(DB_PATH, "wb") as f:
            f.write(buf.read())
        await update.message.reply_text(
            "✅ *تم رفع قاعدة البيانات وتطبيقها.*\n"
            "⚠️ أعد تشغيل البوت لضمان تطبيق كامل.",
            parse_mode="Markdown", reply_markup=kb_admin()
        )
    except Exception as e:
        await update.message.reply_text(f"❌ فشل الرفع: {e}", reply_markup=kb_admin())

# ═══════════════════════════════════════════════
#  💾  إرسال قاعدة البيانات للأدمن
# ═══════════════════════════════════════════════
async def send_db_to_admin(bot, chat_id, reason="تلقائي"):
    if not os.path.exists(DB_PATH):
        return
    seq       = db_get_backup_seq()
    file_size = os.path.getsize(DB_PATH)
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M")

    # نسخ لملف مؤقت (تجنب القراءة أثناء الكتابة)
    tmp = f"/tmp/tikspark_bk_{seq}.db"
    shutil.copy2(DB_PATH, tmp)

    with open(tmp, "rb") as f:
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=f"tikspark_backup_{seq}.db",
                caption=(
                    f"💾 *نسخة احتياطية #{seq}*\n"
                    f"📅 {now_str}\n"
                    f"📦 الحجم: `{file_size:,}` بايت\n"
                    f"🔖 السبب: {reason}"
                ),
                parse_mode="Markdown"
            )
            db_backup_log(seq, file_size)
            # حذف النسخة السابقة من مجلد Backups (نحتفظ بآخر نسخة فقط)
            for old in os.listdir(BACKUP_DIR):
                if old.endswith(".db"):
                    os.remove(os.path.join(BACKUP_DIR, old))
            shutil.copy2(tmp, os.path.join(BACKUP_DIR, f"backup_{seq}.db"))
        except Exception as e:
            log.error(f"send_db_to_admin failed: {e}")
    if os.path.exists(tmp):
        os.remove(tmp)

# ═══════════════════════════════════════════════
#  ⏰  مهمة النسخ الاحتياطي التلقائي كل 30 دقيقة
# ═══════════════════════════════════════════════
async def auto_backup(ctx: ContextTypes.DEFAULT_TYPE):
    await send_db_to_admin(ctx.bot, ADMIN_ID, "تلقائي كل 30 دقيقة")

# ═══════════════════════════════════════════════
#  🚀  تشغيل البوت
# ═══════════════════════════════════════════════
def main():
    db_init()
    log.info("✅ قاعدة البيانات جاهزة")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(btn))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # النسخ التلقائي كل 30 دقيقة
    app.job_queue.run_repeating(auto_backup, interval=1800, first=1800)

    log.info("🚀 البوت يعمل...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

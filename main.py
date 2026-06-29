import time
import json
import logging
import threading
import sqlite3
import random
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ===================================================================
# 1. الإعدادات الأساسية والثوابت
# ===================================================================

BOT_TOKEN        = "8130994366:AAEP5qKlVFRhFqQYPVtgX58NtEjORB-SbKA"
API_URL          = "https://api.tikspark.xyz/graphql"
MAX_POINTS       = 2000
DB_PATH          = "bot_data.db"

# مراحل المحادثة (ConversationHandler)
AWAITING_USERNAME = 1
AWAITING_PASSWORD = 2
AWAITING_TARGET   = 3   # لإدخال اسم المستخدم المستهدف للرشق

# ===================================================================
# 2. إعدادات التسجيل (Logging)
# ===================================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ===================================================================
# 3. قاعدة البيانات (SQLite) — حفظ كل شيء
# ===================================================================

def init_db():
    """إنشاء جدول المستخدمين إذا لم يكن موجوداً."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            password TEXT NOT NULL,
            token TEXT,
            total_points INTEGER DEFAULT 0,
            speed REAL DEFAULT 0.2,
            is_paused INTEGER DEFAULT 0,
            last_score INTEGER DEFAULT 0,
            start_time TEXT,
            session_active INTEGER DEFAULT 0,
            target_username TEXT,
            order_id TEXT,
            rush_enabled INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def db_get_user(user_id: int):
    """استرجاع بيانات مستخدم من قاعدة البيانات."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0],
            "username": row[1],
            "password": row[2],
            "token": row[3],
            "total_points": row[4],
            "speed": row[5],
            "is_paused": row[6],
            "last_score": row[7],
            "start_time": row[8],
            "session_active": row[9],
            "target_username": row[10],
            "order_id": row[11],
            "rush_enabled": row[12]
        }
    return None

def db_save_user(user_id: int, username: str, password: str, token: str = None,
                 total_points: int = 0, speed: float = 0.2, is_paused: int = 0,
                 last_score: int = 0, start_time: str = None, session_active: int = 0,
                 target_username: str = None, order_id: str = None, rush_enabled: int = 0):
    """حفظ أو تحديث بيانات مستخدم في قاعدة البيانات."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO users
        (user_id, username, password, token, total_points, speed, is_paused,
         last_score, start_time, session_active, target_username, order_id, rush_enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, username, password, token, total_points, speed, is_paused,
          last_score, start_time, session_active, target_username, order_id, rush_enabled))
    conn.commit()
    conn.close()

def db_update_points(user_id: int, total_points: int, last_score: int, session_active: int = 1):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET total_points=?, last_score=?, session_active=? WHERE user_id=?",
                 (total_points, last_score, session_active, user_id))
    conn.commit()
    conn.close()

def db_update_token(user_id: int, token: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET token=? WHERE user_id=?", (token, user_id))
    conn.commit()
    conn.close()

def db_update_speed(user_id: int, speed: float):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET speed=? WHERE user_id=?", (speed, user_id))
    conn.commit()
    conn.close()

def db_update_pause(user_id: int, is_paused: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_paused=? WHERE user_id=?", (is_paused, user_id))
    conn.commit()
    conn.close()

def db_update_rush(user_id: int, rush_enabled: int, target_username: str = None, order_id: str = None):
    conn = sqlite3.connect(DB_PATH)
    if target_username is not None:
        conn.execute("UPDATE users SET rush_enabled=?, target_username=?, order_id=? WHERE user_id=?",
                     (rush_enabled, target_username, order_id, user_id))
    else:
        conn.execute("UPDATE users SET rush_enabled=? WHERE user_id=?", (rush_enabled, user_id))
    conn.commit()
    conn.close()

def db_reset_user(user_id: int):
    """إعادة تعيين نقاط المستخدم وجلساته."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET total_points=0, last_score=0, session_active=0, start_time=NULL, rush_enabled=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# ===================================================================
# 4. جلسة HTTP مع إعادة المحاولة
# ===================================================================

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["POST", "GET"])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

# ===================================================================
# 5. الهيدرز (مأخوذة من ملف الرشق المرفق، محدثة بدقة)
# ===================================================================

def login_headers() -> dict:
    return {
        "User-Agent": "okhttp/4.12.0",
        "Accept": "multipart/mixed; deferSpec=20220824, application/json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "x-apollo-operation-id": "3522613813036d73817b2715e67743f8d23d7a85ad08b7e12aa3b29a24a17c43",
        "x-apollo-operation-name": "LoginAccount",
        "x-language": "ar",
        "x-app-name": "com.dev.vidspark",
        "x-device-info": '{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',
        "x-app-sig": "f10d978d71653b10d2888bb3306c994c50a9c21a81bd15ced7b667de0e571312",
        "x-app-ts": "1782743982286",
        "x-app-nonce": "575c92e3ca3d4ace",
    }

def check_account_headers() -> dict:
    return {
        "User-Agent": "okhttp/4.12.0",
        "Accept": "multipart/mixed; deferSpec=20220824, application/json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "x-apollo-operation-id": "407ca5138fd034c87ac0e7cfaa063b5e307ae06b98c4189a1e769ee54e604ca0",
        "x-apollo-operation-name": "CheckAccount",
        "x-language": "ar",
        "x-app-name": "com.dev.vidspark",
        "x-device-info": '{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',
        "x-app-sig": "75c0b8b0ba523495fe6c8629954957bcb618060172ee7e9f7339645cacc37583",
        "x-app-ts": "1782743977783",
        "x-app-nonce": "82ad1ab2063b41a1",
    }

def profile_headers() -> dict:
    return {
        "User-Agent": "okhttp/4.12.0",
        "Accept": "multipart/mixed; deferSpec=20220824, application/json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "x-apollo-operation-id": "2b26eda88e17df7b268dbc1d5a7a0fbd79ff067dbb9df70308a74131d4d84a92",
        "x-apollo-operation-name": "RequestProfileBundleByUsername",
        "x-language": "ar",
        "x-app-name": "com.dev.vidspark",
        "x-device-info": '{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',
        "x-app-sig": "dd49358c152953f1d9b4ca9ea91817674b634163ee78247a1e949c577077ab0f",
        "x-app-ts": "1782743975237",
        "x-app-nonce": "7315b6b6d2be4d8a",
    }

def create_order_headers(token: str) -> dict:
    return {
        "User-Agent": "okhttp/4.12.0",
        "Accept": "multipart/mixed; deferSpec=20220824, application/json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "x-apollo-operation-id": "ad7a6397c3970b1e7601f69d24989bff330e256ee5e39321a8d1ad3fe3879b48",
        "x-apollo-operation-name": "CreateOrder",
        "x-language": "ar",
        "x-app-name": "com.dev.vidspark",
        "token": token,
        "x-csrf-token": "1782747311629:23d92302e0bdc82ce0282249240c7c7b009bc886f54968ed868ba0f61a9be824",
        "x-device-info": '{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',
        "x-app-sig": "ac4b36217a55b8794521d0e50c6a12af0148adc5502756946b12feb07aaaba99",
        "x-app-ts": "1782747311630",
        "x-app-nonce": "77233948b379426c",
    }

def switch_order_headers(token: str) -> dict:
    return {
        "User-Agent": "okhttp/4.12.0",
        "Accept": "multipart/mixed; deferSpec=20220824, application/json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "x-apollo-operation-id": "128f92f9052f0b0cafd214c148e8deec69e56d34a433fbf0c09499307004fe09",
        "x-apollo-operation-name": "SwitchOrder",
        "x-language": "ar",
        "x-app-name": "com.dev.vidspark",
        "token": token,
        "x-csrf-token": "1782747326428:4bacf8e958a4257d65b2dfaccd55511430880a0421dc27bd1375fc4b65bc6e79",
        "x-device-info": '{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',
        "x-app-sig": "00a6d64f633f98ebbf3c80cdd1963bf2c293e9fddf38bd46d27c9ed8ce354062",
        "x-app-ts": "1782747326428",
        "x-app-nonce": "c372e9a797c24f46",
    }

def fetch_headers(token: str) -> dict:
    return {
        "User-Agent": "okhttp/4.12.0",
        "Accept": "multipart/mixed; deferSpec=20220824, application/json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "x-apollo-operation-id": "c2ca4b87e63f30f2cca10e5867d17ea0f1712e96e716a60513f68758b2256185",
        "x-apollo-operation-name": "FetchOrders",
        "x-language": "ar",
        "x-app-name": "com.dev.vidspark",
        "token": token,
        "x-csrf-token": "1782443248827:bf0ad4b105a1f6bcfca393d2f36fbe0f9cf690d37ba398113266884c14017d39",
        "x-device-info": '{"d":"30666439303936303830366134393632","n":"5869616f6d69203233313144524b343847","o":"16","t":"d","v":"2.2.0","s":"0,0"}',
        "x-app-sig": "2998306f19b3a98732a7150a785204d487ae22cb530a0bf4b1ff77a380ad7cd4",
        "x-app-ts": "1782443248827",
        "x-app-nonce": "18b79765e8e0458c",
    }

def action_headers(token: str) -> dict:
    return {
        "User-Agent": "okhttp/4.12.0",
        "Accept": "multipart/mixed; deferSpec=20220824, application/json",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "x-apollo-operation-id": "ddfbb49865193fd38840a34b92139f1759a71331e374bb1254f8e2352630e8f2",
        "x-apollo-operation-name": "RecordFailedOrder",
        "x-language": "ar",
        "x-app-name": "com.dev.vidspark",
        "token": token,
        "x-csrf-token": "1782443501268:86a814a8285234821d27485112d451696809e830e3f71545715299aa5f2373e4",
        "x-device-info": '{"d":"30666439303936303830366134393632","n":"5869616f6d69203233313144524b343847","o":"16","t":"d","v":"2.2.0","s":"0,0"}',
        "x-app-sig": "40a5102e6744f3bddca39e1ce6bb99ce942e20fe9382ba2463e1401d897eff43",
        "x-app-ts": "1782443501268",
        "x-app-nonce": "e69a0252b53843e4",
    }

# ===================================================================
# 6. دوال تسجيل الدخول والتحقق
# ===================================================================

def check_account(username: str, sess: requests.Session) -> tuple[bool, str]:
    """التحقق من وجود الحساب في TikSpark."""
    try:
        payload = {
            "operationName": "CheckAccount",
            "variables": {"username": username},
            "query": "query CheckAccount($username: String!) { checkAccount(username: $username) { isExist code } }"
        }
        resp = sess.post(API_URL, json=payload, headers=check_account_headers(), timeout=10)
        if resp.status_code != 200:
            return False, f"❌ خطأ من الخادم: {resp.status_code}"
        data = resp.json()
        check = data.get("data", {}).get("checkAccount", {})
        if check.get("isExist"):
            return True, "✅ الحساب موجود"
        return False, "❌ الحساب غير موجود في TikSpark"
    except Exception as e:
        return False, f"⚠️ خطأ: {e}"

def get_tiktok_profile(username: str, sess: requests.Session) -> dict:
    """جلب بيانات البروفايل من TikSpark (RequestProfileBundleByUsername)."""
    try:
        payload = {
            "operationName": "RequestProfileBundleByUsername",
            "variables": {
                "username": username,
                "signature": "9653c96a87a1296606dbf2826f40a958af5fe0ae801b5dc472d135c6bdea6d7e",
                "timestamp": "1782747309973",
                "nonce": "20fa2c4d17024dce"
            },
            "query": "mutation RequestProfileBundleByUsername($username: String!, $signature: String!, $timestamp: String!, $nonce: String!) { requestProfileBundleByUsername(username: $username, signature: $signature, timestamp: $timestamp, nonce: $nonce) { profile { method url headers body } } }"
        }
        resp = sess.post(API_URL, json=payload, headers=profile_headers(), timeout=12)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        profile_bundle = data.get("data", {}).get("requestProfileBundleByUsername", {}).get("profile", {})
        return profile_bundle
    except Exception as e:
        log.warning(f"get_tiktok_profile error: {e}")
        return {}

def fetch_tiktok_user_info(profile_bundle: dict, sess: requests.Session) -> dict:
    """استخدام bundle لجلب بيانات المستخدم من TikTok API."""
    try:
        if not profile_bundle:
            return {}
        method  = profile_bundle.get("method", "GET").upper()
        url     = profile_bundle.get("url", "")
        headers = profile_bundle.get("headers", {})
        body    = profile_bundle.get("body", "")

        if not url:
            return {}

        if method == "GET":
            resp = sess.get(url, headers=headers, timeout=12)
        else:
            resp = sess.post(url, data=body, headers=headers, timeout=12)

        if resp.status_code != 200:
            return {}

        tiktok_data = resp.json()
        user = tiktok_data.get("user", {}) or tiktok_data.get("userInfo", {}).get("user", {})
        stats = tiktok_data.get("stats", {}) or tiktok_data.get("userInfo", {}).get("stats", {})

        return {
            "id":             str(user.get("id", "")),
            "uniqueId":       user.get("uniqueId", ""),
            "nickname":       user.get("nickname", ""),
            "avatarMedium":   user.get("avatarMedium", {}).get("urlList", [""])[0] if isinstance(user.get("avatarMedium"), dict) else user.get("avatarMedium", ""),
            "followerCount":  stats.get("followerCount", 0),
            "followingCount": stats.get("followingCount", 0),
            "videoCount":     stats.get("videoCount", 0),
            "diggCount":      stats.get("diggCount", 0),
            "privateAccount": user.get("privateAccount", False),
        }
    except Exception as e:
        log.warning(f"fetch_tiktok_user_info error: {e}")
        return {}

def do_login(username: str, password: str, sess: requests.Session) -> tuple[bool, str, str]:
    """
    تسجيل الدخول الكامل:
    1. تحقق من الحساب في TikSpark.
    2. جلب بروفايل TikTok (اختياري).
    3. تنفيذ LoginAccount للحصول على accessToken.
    """
    # خطوة 1
    exists, msg = check_account(username, sess)
    if not exists:
        return False, msg, ""

    # خطوة 2
    profile_bundle  = get_tiktok_profile(username, sess)
    tiktok_info     = fetch_tiktok_user_info(profile_bundle, sess) if profile_bundle else {}

    user_data = {
        "id":             tiktok_info.get("id", ""),
        "uniqueId":       username,
        "nickname":       tiktok_info.get("nickname", ""),
        "avatarMedium":   tiktok_info.get("avatarMedium", ""),
        "followerCount":  tiktok_info.get("followerCount", 0),
        "followingCount": tiktok_info.get("followingCount", 0),
        "videoCount":     tiktok_info.get("videoCount", 0),
        "privateAccount": tiktok_info.get("privateAccount", False),
        "diggCount":      tiktok_info.get("diggCount", 0),
        "authMethod":     "local",
        "password":       password,
    }

    # خطوة 3
    try:
        LOGIN_Q = """
        mutation LoginAccount($data: TiktokInfo) {
            loginTiktok(data: $data) {
                accessToken
                refreshToken
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
        payload = {
            "operationName": "LoginAccount",
            "variables":     {"data": user_data},
            "query":         LOGIN_Q,
        }
        resp = sess.post(API_URL, json=payload, headers=login_headers(), timeout=12)

        if resp.status_code != 200:
            return False, f"❌ خطأ من الخادم: {resp.status_code}", ""

        data   = resp.json()
        errors = data.get("errors")
        if errors:
            err_msg = errors[0].get("message", "خطأ غير معروف")
            if "password" in err_msg.lower() or "invalid" in err_msg.lower():
                return False, "❌ كلمة المرور غير صحيحة.", ""
            return False, f"❌ {err_msg}", ""

        login_data   = data.get("data", {}).get("loginTiktok", {})
        access_token = login_data.get("accessToken", "")

        if not access_token:
            return False, "❌ لم يُرجع الخادم توكن. تحقق من بياناتك.", ""

        user_info = login_data.get("user", {})
        nickname  = user_info.get("nickname") or user_info.get("username") or username
        score     = user_info.get("score", 0)

        return True, f"✅ تم تسجيل الدخول!\n👤 {nickname}\n🏆 النقاط الحالية: `{score:,}`", access_token

    except requests.Timeout:
        return False, "⏱️ انتهت مهلة الاتصال — حاول مرة أخرى.", ""
    except Exception as e:
        return False, f"⚠️ خطأ: {e}", ""

def refresh_token(user_id: int) -> bool:
    """
    إعادة تسجيل الدخول تلقائياً باستخدام بيانات المستخدم المحفوظة في قاعدة البيانات.
    تُستخدم عند انتهاء صلاحية التوكن (كود 401).
    """
    user = db_get_user(user_id)
    if not user or not user["username"] or not user["password"]:
        return False
    sess = make_session()
    success, msg, token = do_login(user["username"], user["password"], sess)
    sess.close()
    if success and token:
        db_update_token(user_id, token)
        log.info(f"✅ تم تجديد التوكن للمستخدم {user_id}")
        return True
    log.warning(f"❌ فشل تجديد التوكن للمستخدم {user_id}: {msg}")
    return False

# ===================================================================
# 7. دوال الرشق (CreateOrder + SwitchOrder)
# ===================================================================

def create_order(token: str, target_username: str, amount: int = 20, initial_count: int = 34) -> tuple[bool, str, str]:
    """إنشاء طلب (متابعين) على حساب مستهدف."""
    try:
        sess = make_session()
        # جلب صورة المستخدم المستهدف (avatar) حتى ينجح الطلب
        profile = get_tiktok_profile(target_username, sess)
        tiktok_info = fetch_tiktok_user_info(profile, sess) if profile else {}
        avatar = tiktok_info.get("avatarMedium", "")

        payload = {
            "operationName": "CreateOrder",
            "variables": {
                "type": "followers",
                "amount": amount,
                "tiktokerUsername": target_username,
                "avatar": avatar,
                "initialCount": initial_count
            },
            "query": "mutation CreateOrder($type: Action!, $amount: Int!, $tiktokerUsername: String, $videoLink: String, $avatar: String, $initialCount: Int) { createOrder(orderInput: { type: $type amount: $amount tiktokerUsername: $tiktokerUsername videoLink: $videoLink avatar: $avatar initialCount: $initialCount } ) { _id type videoLink tiktokerUsername avatar score priority amount initialCount fulfilled isPublished status createdAt } }"
        }
        resp = sess.post(API_URL, json=payload, headers=create_order_headers(token), timeout=12)
        sess.close()
        if resp.status_code != 200:
            return False, f"فشل إنشاء الطلب (كود {resp.status_code})", ""
        data = resp.json()
        if "errors" in data:
            return False, f"خطأ API: {data['errors'][0].get('message', '')}", ""
        order_id = data.get("data", {}).get("createOrder", {}).get("_id", "")
        if order_id:
            return True, f"✅ تم إنشاء طلب الرشق على @{target_username}", order_id
        return False, "❌ لم يتم استلام Order ID", ""
    except Exception as e:
        return False, f"⚠️ خطأ: {e}", ""

def switch_order(token: str, order_id: str) -> tuple[bool, str]:
    """تبديل حالة الطلب (تشغيل/إيقاف) لكسب نقاط إضافية."""
    try:
        payload = {
            "operationName": "SwitchOrder",
            "variables": {"orderId": order_id},
            "query": "mutation SwitchOrder($orderId: ID!) { switchOrder(orderId: $orderId) { _id status fulfilled isPublished } }"
        }
        sess = make_session()
        resp = sess.post(API_URL, json=payload, headers=switch_order_headers(token), timeout=12)
        sess.close()
        if resp.status_code != 200:
            return False, f"فشل تبديل الطلب (كود {resp.status_code})"
        data = resp.json()
        if "errors" in data:
            return False, f"خطأ API: {data['errors'][0].get('message', '')}"
        status = data.get("data", {}).get("switchOrder", {}).get("status", "unknown")
        return True, f"✅ تم تبديل حالة الطلب إلى: {status}"
    except Exception as e:
        return False, f"⚠️ خطأ: {e}"

# ===================================================================
# 8. دالة جمع النقاط الأساسية + نظام الرشق (تعمل في خيط منفصل)
# ===================================================================

def collect_points(user_id: int, bot):
    """
    الحلقة الرئيسية لجمع النقاط:
    - FetchOrders → ActionOrder
    - كل 5 دورات: CreateOrder / SwitchOrder (إذا كان الرشق مفعّلاً)
    - تجديد التوكن تلقائياً عند انتهاء الصلاحية
    - الحد الأقصى 2000 نقطة
    """
    user = db_get_user(user_id)
    if not user or not user["token"]:
        log.warning(f"لا يوجد توكن للمستخدم {user_id}")
        return

    token = user["token"]
    target_username = user.get("target_username")
    rush_enabled = user.get("rush_enabled", 0)
    order_id = user.get("order_id")

    # حالة الجلسة (تُحفظ في الذاكرة المحلية)
    st = {
        "running": True,
        "paused": False,
        "total_score": user["total_points"],
        "last_score": user["last_score"],
        "start_time": datetime.now(),
        "session_active": True,
        "rush_counter": 0,
        "order_id": order_id,
    }

    sess = make_session()
    fh = fetch_headers(token)
    ah = action_headers(token)

    def send(text: str, kbd=True):
        try:
            bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=get_main_keyboard() if kbd else None
            )
        except Exception as ex:
            log.warning(f"فشل إرسال رسالة للمستخدم {user_id}: {ex}")

    send("🚀 *بدأ جمع النقاط!*\nالحد الأقصى: 2000 نقطة.")
    if rush_enabled and target_username:
        send(f"🔄 *وضع الرشق مفعّل* على @{target_username}")

    FETCH_Q = "query FetchOrders($page: Int!) { getOrders(page: $page) { _id } }"
    ACTION_Q = """
    mutation ActionOrder($orderId: ID!, $validationData: ValidationDataInput!) {
        actionOrder(orderId: $orderId, validationData: $validationData) {
            score
            taskProgress { count startTime taskProgressLimit }
        }
    }"""

    consecutive_errors = 0
    MAX_CONSECUTIVE = 5
    last_score = st["last_score"]

    while st["running"]:
        # التحقق من الحد الأقصى
        if st["total_score"] >= MAX_POINTS:
            send(f"🏆 *وصلت للحد الأقصى!*\nالإجمالي: `{st['total_score']:,}` نقطة\nاستخدم /start للبدء من جديد.", kbd=False)
            db_reset_user(user_id)
            break

        # التحقق من الإيقاف المؤقت (من قاعدة البيانات)
        user_check = db_get_user(user_id)
        if user_check and user_check["is_paused"] == 1:
            st["paused"] = True
            time.sleep(1)
            continue
        else:
            st["paused"] = False

        try:
            # ── 1. جلب الطلبات ──────────────────────────────────────────
            r1 = sess.post(
                API_URL,
                json={"operationName": "FetchOrders", "variables": {"page": 2}, "query": FETCH_Q},
                headers=fh,
                timeout=12
            )
            if r1.status_code == 401:
                send("⛔ *انتهت صلاحية التوكن!*\nجاري تجديد الجلسة تلقائياً...", kbd=False)
                if refresh_token(user_id):
                    new_user = db_get_user(user_id)
                    if new_user and new_user["token"]:
                        token = new_user["token"]
                        fh = fetch_headers(token)
                        ah = action_headers(token)
                        send("✅ *تم تجديد الجلسة!*", kbd=False)
                        continue
                    else:
                        send("❌ *فشل تجديد الجلسة.*", kbd=False)
                        break
                else:
                    send("❌ *فشل تجديد الجلسة.*\nيرجى تسجيل الدخول مجدداً بـ /start", kbd=False)
                    break

            if r1.status_code != 200:
                raise ValueError(f"كود الخادم: {r1.status_code}")

            orders = r1.json().get("data", {}).get("getOrders", [])
            if not orders:
                send("📭 *لا توجد طلبات متاحة الآن.*\nسيتم الانتظار 10 ثوانٍ...")
                time.sleep(10)
                continue

            order_id_fetched = orders[0]["_id"]

            # ── 2. تنفيذ الطلب (ActionOrder) ──────────────────────────
            r2 = sess.post(
                API_URL,
                json={
                    "operationName": "ActionOrder",
                    "variables": {
                        "orderId": order_id_fetched,
                        "validationData": {
                            "attempts": 1,
                            "initialNumber": random.uniform(1000, 5000),
                            "timeSpent": random.randint(15000, 45000)
                        }
                    },
                    "query": ACTION_Q
                },
                headers=ah,
                timeout=12
            )
            if r2.status_code == 401:
                if refresh_token(user_id):
                    new_user = db_get_user(user_id)
                    if new_user and new_user["token"]:
                        token = new_user["token"]
                        fh = fetch_headers(token)
                        ah = action_headers(token)
                        continue
                send("❌ *فشل تجديد الجلسة.*", kbd=False)
                break

            if r2.status_code != 200:
                raise ValueError(f"فشل تنفيذ الطلب — كود: {r2.status_code}")

            # ── 3. معالجة النتيجة ────────────────────────────────────────
            result = r2.json()
            api_err = result.get("errors")
            if api_err:
                raise ValueError(api_err[0].get("message", "خطأ API غير معروف"))

            action = result.get("data", {}).get("actionOrder") or {}
            score = action.get("score", last_score)
            progress = action.get("taskProgress") or {}
            count = progress.get("count", 0)

            gained = max(0, score - last_score)
            last_score = score
            st["total_score"] = score
            st["last_score"] = score
            consecutive_errors = 0

            # تحديث قاعدة البيانات
            db_update_points(user_id, score, last_score, 1)

            # ── 4. بناء رسالة النقاط ─────────────────────────────────
            if count == 0 and gained > 3:
                bonus = gained - 3
                msg = (
                    f"🎉 *اكتملت دورة!*\n"
                    f"➕ مكافأة: `+{bonus}` نقطة\n"
                    f"🏆 الإجمالي: `{score:,}` نقطة"
                )
            else:
                bar_fill = min(count, 10)
                bar = "█" * bar_fill + "░" * (10 - bar_fill)
                msg = (
                    f"✅ *تم جمع `+{gained}` نقطة*\n"
                    f"📈 `[{bar}]` {count}/∞\n"
                    f"🏆 الإجمالي: `{score:,}` نقطة"
                )

            send(msg)

            # ── 5. نظام الرشق (كل 5 دورات) ────────────────────────────
            if rush_enabled and target_username:
                st["rush_counter"] += 1
                if st["rush_counter"] % 5 == 0:
                    if not st["order_id"]:
                        # إنشاء طلب جديد
                        success, cr_msg, new_order_id = create_order(token, target_username, amount=20, initial_count=34)
                        if success and new_order_id:
                            st["order_id"] = new_order_id
                            db_update_rush(user_id, 1, target_username, new_order_id)
                            send(f"🔄 {cr_msg}\n🆔 `{new_order_id}`")
                        else:
                            send(f"⚠️ فشل إنشاء الطلب: {cr_msg}")
                    else:
                        # تبديل الطلب الحالي
                        success, sw_msg = switch_order(token, st["order_id"])
                        if success:
                            send(f"🔄 {sw_msg}")
                        else:
                            send(f"⚠️ فشل تبديل الطلب: {sw_msg}\nسيتم إنشاء طلب جديد.")
                            st["order_id"] = None
                            db_update_rush(user_id, 1, target_username, None)

            # ── 6. الانتظار حسب السرعة ────────────────────────────────
            time.sleep(user_check["speed"] if user_check else 0.2)

        except ValueError as ve:
            consecutive_errors += 1
            log.warning(f"[{user_id}] خطأ: {ve}")
            send(f"⚠️ *خطأ:* {ve}\nالمحاولة {consecutive_errors}/{MAX_CONSECUTIVE}...")
            if consecutive_errors >= MAX_CONSECUTIVE:
                send(f"🛑 *توقف تلقائي* — {MAX_CONSECUTIVE} أخطاء متتالية.\nيرجى إعادة التشغيل بـ /start", kbd=False)
                break
            time.sleep(3 * consecutive_errors)

        except requests.Timeout:
            consecutive_errors += 1
            send(f"⏱️ *انتهت مهلة الاتصال* — إعادة المحاولة... ({consecutive_errors})")
            time.sleep(4)

        except Exception as e:
            consecutive_errors += 1
            log.exception(f"[{user_id}] خطأ غير متوقع")
            send(f"🔴 *خطأ غير متوقع:*\n`{type(e).__name__}: {e}`\nسيتم إعادة المحاولة...")
            time.sleep(5)

    # ── انتهاء الجلسة ────────────────────────────────────────────────────
    sess.close()
    db_update_points(user_id, st["total_score"], last_score, 0)
    send(f"🏁 *انتهت الجلسة*\n🏆 الإجمالي النهائي: `{st['total_score']:,}` نقطة")

# ===================================================================
# 9. لوحة المفاتيح (أزرار مضمنة فقط)
# ===================================================================

def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ تشغيل", callback_data="start"),
            InlineKeyboardButton("⏸ إيقاف مؤقت", callback_data="pause"),
            InlineKeyboardButton("▶️ استئناف", callback_data="resume"),
        ],
        [
            InlineKeyboardButton("🚀 0.005ث", callback_data="speed_0.005"),
            InlineKeyboardButton("⚡ 0.02ث", callback_data="speed_0.02"),
            InlineKeyboardButton("🔥 0.05ث", callback_data="speed_0.05"),
        ],
        [
            InlineKeyboardButton("🐢 0.5ث", callback_data="speed_0.5"),
            InlineKeyboardButton("🐇 0.2ث", callback_data="speed_0.2"),
        ],
        [
            InlineKeyboardButton("🚀 تفعيل الرشق", callback_data="enable_rush"),
            InlineKeyboardButton("⛔ إلغاء الرشق", callback_data="disable_rush"),
        ],
        [
            InlineKeyboardButton("📊 الحالة", callback_data="status"),
            InlineKeyboardButton("🔄 تغيير الحساب", callback_data="change_account"),
        ],
    ])

# ===================================================================
# 10. معالجات البوت (Handlers)
# ===================================================================

WELCOME = (
    "👋 *أهلاً في بوت TikSpark!*\n\n"
    "📌 أرسل لي *اسم المستخدم* (يوزرنيم TikTok) للبدء.\n"
    "مثال: `aosnzh`"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بدء المحادثة واستقبال اليوزرنيم."""
    user_id = update.effective_user.id
    user = db_get_user(user_id)
    if user and user["session_active"] == 1:
        await update.message.reply_text(
            "⚠️ *لديك جلسة نشطة بالفعل!*\nاستخدم الأزرار للتحكم.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return ConversationHandler.END

    await update.message.reply_text(
        WELCOME,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ إلغاء", callback_data="cancel")
        ]])
    )
    return AWAITING_USERNAME

async def handle_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال اسم المستخدم."""
    username = update.message.text.strip().lstrip("@")
    if len(username) < 2:
        await update.message.reply_text(
            "❌ اسم المستخدم قصير جداً. حاول مرة أخرى:",
            parse_mode="Markdown"
        )
        return AWAITING_USERNAME
    context.user_data["pending_username"] = username
    await update.message.reply_text(
        f"👤 *اسم المستخدم:* `{username}`\n\n"
        f"🔑 الآن أرسل *كلمة المرور*:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ إلغاء", callback_data="cancel")
        ]])
    )
    return AWAITING_PASSWORD

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال كلمة المرور وتنفيذ تسجيل الدخول."""
    user_id = update.effective_user.id
    password = update.message.text.strip()
    username = context.user_data.get("pending_username", "")

    if not username:
        await update.message.reply_text("⚠️ حدث خطأ. ابدأ من جديد بـ /start")
        return ConversationHandler.END

    msg = await update.message.reply_text(
        f"🔄 *جاري تسجيل الدخول...*\n👤 `{username}`",
        parse_mode="Markdown"
    )

    import asyncio
    loop = asyncio.get_event_loop()
    sess = make_session()
    is_ok, feedback, access_token = await loop.run_in_executor(
        None, do_login, username, password, sess
    )
    sess.close()

    if not is_ok:
        await msg.edit_text(
            f"{feedback}\n\nحاول مرة أخرى أو استخدم /start.",
            parse_mode="Markdown"
        )
        context.user_data.pop("pending_username", None)
        return AWAITING_USERNAME

    # حفظ البيانات في قاعدة البيانات
    db_save_user(
        user_id, username, password, access_token,
        total_points=0, speed=0.2, is_paused=0,
        last_score=0, start_time=None, session_active=0,
        target_username=None, order_id=None, rush_enabled=0
    )

    await msg.edit_text(
        f"{feedback}\n\n✅ جاهز! استخدم الأزرار للتحكم.",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        "🎛️ *لوحة التحكم جاهزة:*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    context.user_data.pop("pending_username", None)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إلغاء المحادثة."""
    if update.message:
        await update.message.reply_text("❌ تم الإلغاء. استخدم /start للبدء من جديد.")
    context.user_data.pop("pending_username", None)
    return ConversationHandler.END

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج جميع الأزرار المضمنة."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    action = query.data

    # ── تغيير الحساب ────────────────────────────────────────────────────
    if action == "change_account":
        user = db_get_user(user_id)
        if user and user["session_active"] == 1:
            await query.edit_message_text(
                "⚠️ *لديك جلسة نشطة.*\nأوقف الجلسة أولاً ثم غيّر الحساب.",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            return
        await query.edit_message_text(
            WELCOME + "\n\nأرسل اليوزرنيم الجديد:",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_new_account"] = True
        return

    if action == "cancel":
        await query.edit_message_text("❌ تم الإلغاء.")
        return

    user = db_get_user(user_id)
    if not user or not user["token"]:
        await query.edit_message_text(
            "⚠️ *لا يوجد حساب مسجل.*\nاستخدم /start لتسجيل الدخول.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return

    # ── تشغيل ───────────────────────────────────────────────────────────
    if action == "start":
        if user["session_active"] == 1:
            await query.edit_message_text(
                "⏳ *الجمع يعمل بالفعل!*\nاستخدم زر الحالة لرؤية التقدم.",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            return
        db_update_pause(user_id, 0)
        db_update_points(user_id, user["total_points"], user["last_score"], 1)
        thread = threading.Thread(
            target=collect_points,
            args=(user_id, context.bot),
            daemon=True,
            name=f"collector-{user_id}"
        )
        thread.start()
        await query.edit_message_text(
            "🚀 *تم تشغيل جمع النقاط!*\nستصلك رسائل التحديث قريباً.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

    # ── إيقاف مؤقت ───────────────────────────────────────────────────────
    elif action == "pause":
        if user["session_active"] == 0:
            await query.edit_message_text(
                "⚠️ *الجمع ليس قيد التشغيل.*",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            return
        if user["is_paused"] == 1:
            await query.edit_message_text(
                "⏸ *الجمع متوقف مؤقتاً بالفعل.*",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            return
        db_update_pause(user_id, 1)
        await query.edit_message_text(
            "⏸ *تم إيقاف الجمع مؤقتاً.*\nاستخدم زر الاستئناف للمتابعة.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

    # ── استئناف ──────────────────────────────────────────────────────────
    elif action == "resume":
        if user["session_active"] == 0:
            await query.edit_message_text(
                "⚠️ *الجمع ليس قيد التشغيل.*\nاضغط تشغيل أولاً.",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            return
        if user["is_paused"] == 0:
            await query.edit_message_text(
                "▶️ *الجمع يعمل بالفعل.*",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            return
        db_update_pause(user_id, 0)
        await query.edit_message_text(
            "▶️ *تم استئناف الجمع!*",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

    # ── تفعيل الرشق ─────────────────────────────────────────────────────
    elif action == "enable_rush":
        if user["session_active"] == 0:
            await query.edit_message_text(
                "⚠️ *شغّل الجمع أولاً.*",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            return
        await query.edit_message_text(
            "📌 أرسل *اسم المستخدم المستهدف* للرشق (مثال: `aosnzh`):",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_target"] = True
        return

    # ── إلغاء الرشق ─────────────────────────────────────────────────────
    elif action == "disable_rush":
        db_update_rush(user_id, 0)
        await query.edit_message_text(
            "⛔ *تم إلغاء وضع الرشق.*",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

    # ── تغيير السرعة ────────────────────────────────────────────────────
    elif action.startswith("speed_"):
        speed = float(action.split("_")[1])
        db_update_speed(user_id, speed)
        labels = {
            0.005: "🚀 أقصى سرعة 0.005ث",
            0.02:  "⚡ سريع جداً 0.02ث",
            0.05:  "🔥 سريع 0.05ث",
            0.2:   "🐇 عادي 0.2ث",
            0.5:   "🐢 بطيء 0.5ث"
        }
        label = labels.get(speed, f"{speed}ث")
        await query.edit_message_text(
            f"⚙️ *السرعة الجديدة:* {label}",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

    # ── الحالة ──────────────────────────────────────────────────────────
    elif action == "status":
        running = user["session_active"] == 1
        paused = user["is_paused"] == 1
        uptime = "غير متاح"
        if user["start_time"]:
            try:
                start = datetime.fromisoformat(user["start_time"])
                elapsed = datetime.now() - start
                hours = int(elapsed.total_seconds() // 3600)
                minutes = int((elapsed.total_seconds() % 3600) // 60)
                uptime = f"{hours} ساعة {minutes} دقيقة"
            except:
                pass

        await query.edit_message_text(
            f"📊 *حالة الحساب:*\n\n"
            f"• الحساب:   `{user['username']}`\n"
            f"• الحالة:   {'🟢 يعمل' if running else '🔴 متوقف'}\n"
            f"• الإيقاف:  {'⏸ متوقف' if paused else '▶️ يعمل'}\n"
            f"• السرعة:   `{user['speed']}ث` بين كل طلب\n"
            f"• النقاط:   `{user['total_points']:,}` نقطة\n"
            f"• الحد:     `{MAX_POINTS}` نقطة\n"
            f"• وقت التشغيل: `{uptime}`\n"
            f"• المتبقي:   `{max(0, MAX_POINTS - user['total_points'])}` نقطة\n"
            f"• الرشق:    {'🟢 مفعّل' if user.get('rush_enabled', 0) else '🔴 ملغي'}",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

# ===================================================================
# 11. معالج إدخال الهدف للرشق (يُستدعى من مرحلة AWAITING_TARGET)
# ===================================================================

async def handle_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """استقبال اسم المستخدم المستهدف للرشق وإنشاء طلب تجريبي."""
    user_id = update.effective_user.id
    target = update.message.text.strip().lstrip("@")

    if len(target) < 2:
        await update.message.reply_text("❌ اسم المستخدم قصير جداً.", parse_mode="Markdown")
        return

    user = db_get_user(user_id)
    if not user or not user["token"]:
        await update.message.reply_text("⚠️ حدث خطأ في الجلسة.", parse_mode="Markdown")
        return

    msg = await update.message.reply_text(
        f"🔄 جاري اختبار الرشق على @{target}...",
        parse_mode="Markdown"
    )

    # إنشاء طلب رشق تجريبي
    success, cr_msg, order_id = create_order(user["token"], target, amount=20, initial_count=34)

    if success and order_id:
        db_update_rush(user_id, 1, target, order_id)
        await msg.edit_text(
            f"✅ {cr_msg}\n🆔 `{order_id}`\n\n🔄 سيتم التبديل تلقائياً كل 5 دورات.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
    else:
        await msg.edit_text(
            f"⚠️ فشل الرشق: {cr_msg}\nتأكد من صحة اليوزرنيم.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

    context.user_data.pop("awaiting_target", None)

# ===================================================================
# 12. التشغيل الرئيسي
# ===================================================================

def main():
    init_db()
    log.info("🤖 البوت يعمل مع قاعدة بيانات وجلسات محفوظة.")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAITING_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username),
            ],
            AWAITING_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password),
            ],
            AWAITING_TARGET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_target_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(lambda u, c: cancel(u, c), pattern="^cancel$"),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_callback))

    log.info("✅ البوت جاهز — اضغط Ctrl+C للإيقاف.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
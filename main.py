import time
import json
import logging
import threading
import os
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ========== الإعدادات ==========
BOT_TOKEN      = "8130994366:AAEP5qKlVFRhFqQYPVtgX58NtEjORB-SbKA"
API_URL        = "https://api.tikspark.xyz/graphql"
AWAITING_TOKEN = 1

# ========== Logging ==========
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ========== HTTP Session ==========
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["POST"])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

# ========== الهيدرز ==========
def fetch_headers(token: str) -> dict:
    return {
        "User-Agent":               "okhttp/4.12.0",
        "Accept":                   "multipart/mixed; deferSpec=20220824, application/json",
        "Accept-Encoding":          "gzip",
        "Content-Type":             "application/json",
        "x-apollo-operation-id":    "c2ca4b87e63f30f2cca10e5867d17ea0f1712e96e716a60513f68758b2256185",
        "x-apollo-operation-name":  "FetchOrders",
        "x-language":               "ar",
        "x-app-name":               "com.dev.vidspark",
        "token":                    token,
        "x-csrf-token":             "1782443248827:bf0ad4b105a1f6bcfca393d2f36fbe0f9cf690d37ba398113266884c14017d39",
        "x-device-info":            '{"d":"30666439303936303830366134393632","n":"5869616f6d69203233313144524b343847","o":"16","t":"d","v":"2.2.0","s":"0,0"}',
        "x-app-sig":                "2998306f19b3a98732a7150a785204d487ae22cb530a0bf4b1ff77a380ad7cd4",
        "x-app-ts":                 "1782443248827",
        "x-app-nonce":              "18b79765e8e0458c",
    }

def action_headers(token: str) -> dict:
    return {
        "User-Agent":               "okhttp/4.12.0",
        "Accept":                   "multipart/mixed; deferSpec=20220824, application/json",
        "Accept-Encoding":          "gzip",
        "Content-Type":             "application/json",
        "x-apollo-operation-id":    "ddfbb49865193fd38840a34b92139f1759a71331e374bb1254f8e2352630e8f2",
        "x-apollo-operation-name":  "RecordFailedOrder",
        "x-language":               "ar",
        "x-app-name":               "com.dev.vidspark",
        "token":                    token,
        "x-csrf-token":             "1782443501268:86a814a8285234821d27485112d451696809e830e3f71545715299aa5f2373e4",
        "x-device-info":            '{"d":"30666439303936303830366134393632","n":"5869616f6d69203233313144524b343847","o":"16","t":"d","v":"2.2.0","s":"0,0"}',
        "x-app-sig":                "40a5102e6744f3bddca39e1ce6bb99ce942e20fe9382ba2463e1401d897eff43",
        "x-app-ts":                 "1782443501268",
        "x-app-nonce":              "e69a0252b53843e4",
    }

# ========== التحقق من التوكن ==========
def check_token(token: str) -> tuple[bool, str]:
    try:
        sess = make_session()
        payload = {
            "operationName": "FetchOrders",
            "variables":     {"page": 1},
            "query":         "query FetchOrders($page: Int!) { getOrders(page: $page) { _id } }"
        }
        resp = sess.post(API_URL, json=payload, headers=fetch_headers(token), timeout=8)
        if resp.status_code == 401:
            return False, "❌ التوكن منتهي الصلاحية أو غير صحيح."
        if resp.status_code != 200:
            return False, f"❌ خطأ من الخادم: كود {resp.status_code}"
        data = resp.json()
        if "errors" in data:
            return False, f"❌ خطأ: {data['errors'][0].get('message', 'خطأ غير معروف')}"
        if "data" in data:
            return True, "✅ التوكن صالح!"
        return False, "❌ استجابة غير متوقعة."
    except requests.Timeout:
        return False, "⏱️ انتهت مهلة الاتصال — تحقق من الإنترنت."
    except Exception as e:
        return False, f"⚠️ خطأ في الاتصال: {e}"

# ========== حالة المستخدمين في الذاكرة ==========
user_state: dict[int, dict] = {}
state_lock = threading.Lock()

def get_state(user_id: int) -> dict:
    with state_lock:
        if user_id not in user_state:
            user_state[user_id] = {
                "running":       False,
                "stop_flag":     False,
                "thread":        None,
                "token":         None,
                "speed":         0.2,
                "total_score":   0,
                "task_count":    0,
                "errors_count":  0,
                "current_order": "لا يوجد",
                "session":       None,
            }
        return user_state[user_id]

# ========== دالة جمع النقاط ==========
def collect_points(user_id: int, token: str, bot):
    st = get_state(user_id)
    st["running"]       = True
    st["stop_flag"]     = False
    st["total_score"]   = 0
    st["task_count"]    = 0
    st["errors_count"]  = 0
    st["current_order"] = "لا يوجد"
    st["session"]       = make_session()

    sess       = st["session"]
    fh         = fetch_headers(token)
    ah         = action_headers(token)
    last_score = 0

    def send(text: str, kbd=True):
        try:
            bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=get_main_keyboard() if kbd else None
            )
        except Exception as ex:
            log.warning(f"send_message failed [{user_id}]: {ex}")

    send("🚀 *بدأ جمع النقاط!*\nستصلك تحديثات بعد كل عملية.")

    FETCH_Q = "query FetchOrders($page: Int!) { getOrders(page: $page) { _id } }"
    ACTION_Q = """
    mutation ActionOrder($orderId: ID!, $validationData: ValidationDataInput!) {
        actionOrder(orderId: $orderId, validationData: $validationData) {
            score
            taskProgress { count startTime taskProgressLimit }
        }
    }"""

    consecutive_errors = 0
    MAX_CONSECUTIVE    = 5

    while not st.get("stop_flag", False):
        try:
            # ── 1. جلب الطلبات ──────────────────────────────────────────
            r1 = sess.post(
                API_URL,
                json={"operationName": "FetchOrders", "variables": {"page": 2}, "query": FETCH_Q},
                headers=fh,
                timeout=12
            )
            if r1.status_code == 401:
                send("⛔ *انتهت صلاحية التوكن!*\nأعد التسجيل بـ /start", kbd=False)
                break
            if r1.status_code != 200:
                raise ValueError(f"كود الخادم: {r1.status_code}")

            orders = r1.json().get("data", {}).get("getOrders", [])
            if not orders:
                send("📭 *لا توجد طلبات متاحة الآن.*\nسيتم الانتظار 10 ثوانٍ...")
                time.sleep(10)
                continue

            order_id = orders[0]["_id"]
            st["current_order"] = order_id

            # ── 2. تنفيذ الطلب ──────────────────────────────────────────
            r2 = sess.post(
                API_URL,
                json={
                    "operationName": "ActionOrder",
                    "variables": {
                        "orderId": order_id,
                        "validationData": {
                            "attempts":      1,
                            "initialNumber": 2953.0,
                            "timeSpent":     31883.0
                        }
                    },
                    "query": ACTION_Q
                },
                headers=ah,
                timeout=12
            )
            if r2.status_code == 401:
                send("⛔ *انتهت صلاحية التوكن أثناء التنفيذ!*\nأعد التسجيل بـ /start", kbd=False)
                break
            if r2.status_code != 200:
                raise ValueError(f"فشل تنفيذ الطلب — كود: {r2.status_code}")

            # ── 3. معالجة النتيجة ────────────────────────────────────────
            result  = r2.json()
            api_err = result.get("errors")
            if api_err:
                raise ValueError(api_err[0].get("message", "خطأ API غير معروف"))

            action   = result.get("data", {}).get("actionOrder") or {}
            score    = action.get("score", last_score)
            progress = action.get("taskProgress") or {}
            count    = progress.get("count", st["task_count"])

            gained             = max(0, score - last_score)
            last_score         = score
            st["total_score"]  = score
            st["task_count"]   = count
            consecutive_errors = 0

            # ── 4. بناء الرسالة ─────────────────────────────────────────
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

            # ── 5. الانتظار ─────────────────────────────────────────────
            time.sleep(st.get("speed", 0.2))

        except ValueError as ve:
            consecutive_errors += 1
            st["errors_count"] += 1
            log.warning(f"[{user_id}] خطأ: {ve}")
            send(f"⚠️ *خطأ:* {ve}\nالمحاولة {consecutive_errors}/{MAX_CONSECUTIVE}...")
            if consecutive_errors >= MAX_CONSECUTIVE:
                send(f"🛑 *توقف تلقائي* — {MAX_CONSECUTIVE} أخطاء متتالية.\nتحقق من حسابك ثم أعد التشغيل.", kbd=False)
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
    st["running"] = False
    try:
        st["session"].close()
    except Exception:
        pass

    send(
        f"🏁 *انتهت جلسة الجمع*\n"
        f"🏆 الإجمالي النهائي: `{st['total_score']:,}` نقطة\n"
        f"📋 أخطاء مسجلة: `{st['errors_count']}`"
    )

# ========== لوحة المفاتيح ==========
def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ تشغيل",  callback_data="start"),
            InlineKeyboardButton("⏹ إيقاف",  callback_data="stop"),
        ],
        [
            InlineKeyboardButton("🚀 0.005ث", callback_data="speed_0.005"),
            InlineKeyboardButton("⚡ 0.02ث",  callback_data="speed_0.02"),
            InlineKeyboardButton("🔥 0.05ث",  callback_data="speed_0.05"),
        ],
        [
            InlineKeyboardButton("🐢 بطيء 0.5ث", callback_data="speed_0.5"),
            InlineKeyboardButton("🐇 عادي 0.2ث", callback_data="speed_0.2"),
        ],
        [
            InlineKeyboardButton("📊 الحالة",       callback_data="status"),
            InlineKeyboardButton("🔄 تغيير التوكن", callback_data="change_token"),
        ],
    ])

# ========== المعالجات ==========
WELCOME = (
    "👋 *أهلاً في بوت TikSpark!*\n\n"
    "🔑 أرسل لي *توكن JWT* الخاص بحسابك للبدء.\n"
    "📌 مثال: `eyJhbGciOiJIUzI1NiIs...`\n\n"
    "⚙️ احصل على التوكن من طلب `LoginAccount` في التطبيق."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # إيقاف أي جلسة سابقة
    st = get_state(user_id)
    st["stop_flag"] = True
    t = st.get("thread")
    if t and t.is_alive():
        t.join(timeout=3)

    # مسح الحالة كاملاً في كل /start (بما فيها التوكن)
    with state_lock:
        user_state[user_id] = {
            "running":       False,
            "stop_flag":     False,
            "thread":        None,
            "token":         None,
            "speed":         0.2,
            "total_score":   0,
            "task_count":    0,
            "errors_count":  0,
            "current_order": "لا يوجد",
            "session":       None,
        }

    await update.message.reply_text(
        WELCOME,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ إلغاء", callback_data="cancel")
        ]])
    )
    return AWAITING_TOKEN

async def handle_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    token   = update.message.text.strip()

    if len(token) < 20 or not token.startswith("ey"):
        await update.message.reply_text(
            "❌ *صيغة التوكن خاطئة.*\n"
            "التوكن يجب أن يبدأ بـ `ey` ويكون طويلاً.\n"
            "حاول مرة أخرى أو اضغط /start للبدء.",
            parse_mode="Markdown"
        )
        return AWAITING_TOKEN

    msg = await update.message.reply_text("🔄 *جاري التحقق من التوكن...*", parse_mode="Markdown")

    import asyncio
    loop = asyncio.get_event_loop()
    is_valid, feedback = await loop.run_in_executor(None, check_token, token)

    if not is_valid:
        await msg.edit_text(
            f"{feedback}\n\nأرسل توكناً صحيحاً أو استخدم /start.",
            parse_mode="Markdown"
        )
        return AWAITING_TOKEN

    # حفظ التوكن في الذاكرة فقط (لا قاعدة بيانات)
    st = get_state(user_id)
    st["token"] = token
    st["speed"] = 0.2

    await msg.edit_text(
        f"{feedback}\n\n✅ تم الحفظ! استخدم الأزرار للتحكم.",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        "🎛️ *لوحة التحكم جاهزة:*",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("❌ تم الإلغاء. استخدم /start للبدء من جديد.")
    return ConversationHandler.END

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    action  = query.data

    # ── تغيير التوكن ────────────────────────────────────────────────────
    if action == "change_token":
        st = get_state(user_id)
        st["stop_flag"] = True
        t = st.get("thread")
        if t and t.is_alive():
            t.join(timeout=3)
        await query.edit_message_text(
            WELCOME + "\n\nأرسل التوكن الجديد:",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_token"] = True
        return

    if action == "cancel":
        await query.edit_message_text("❌ تم الإلغاء.")
        return

    st = get_state(user_id)
    token = st.get("token")

    if not token:
        await query.edit_message_text(
            "⚠️ *لا يوجد توكن مسجل.*\nاستخدم /start لإضافة توكنك.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return

    # ── تشغيل ───────────────────────────────────────────────────────────
    if action == "start":
        if st.get("running"):
            await query.edit_message_text(
                "⏳ *الجمع يعمل بالفعل!*\nاستخدم زر الحالة لرؤية التقدم.",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            return
        st["stop_flag"] = False
        bot    = context.bot
        thread = threading.Thread(
            target=collect_points,
            args=(user_id, token, bot),
            daemon=True,
            name=f"collector-{user_id}"
        )
        thread.start()
        st["thread"] = thread
        await query.edit_message_text(
            "🚀 *تم تشغيل جمع النقاط!*\nستصلك رسائل التحديث قريباً.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

    # ── إيقاف ───────────────────────────────────────────────────────────
    elif action == "stop":
        if not st.get("running"):
            await query.edit_message_text(
                "⚠️ *الجمع ليس قيد التشغيل حالياً.*",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            return
        st["stop_flag"] = True
        await query.edit_message_text(
            f"⏹ *جاري الإيقاف...*\n🏆 الإجمالي حتى الآن: `{st['total_score']:,}` نقطة",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

    # ── السرعة ──────────────────────────────────────────────────────────
    elif action.startswith("speed_"):
        speed = float(action.split("_")[1])
        st["speed"] = speed
        labels = {
            0.005: "🚀 أقصى سرعة 0.005ث",
            0.02:  "⚡ سريع جداً 0.02ث",
            0.05:  "🔥 سريع 0.05ث",
            0.2:   "🐇 عادي 0.2ث",
            0.5:   "🐢 بطيء 0.5ث"
        }
        label     = labels.get(speed, f"{speed}ث")
        state_txt = "🟢 يعمل" if st.get("running") else "🔴 متوقف"
        await query.edit_message_text(
            f"⚙️ *السرعة الجديدة:* {label}\n📌 الحالة: {state_txt}",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

    # ── الحالة ──────────────────────────────────────────────────────────
    elif action == "status":
        running    = st.get("running", False)
        state_icon = "🟢 يعمل" if running else "🔴 متوقف"
        speed      = st.get("speed", 0.2)
        score      = st.get("total_score", 0)
        count      = st.get("task_count", 0)
        order      = st.get("current_order", "لا يوجد")
        errors     = st.get("errors_count", 0)

        await query.edit_message_text(
            f"📊 *حالة الحساب:*\n\n"
            f"• الحالة:    {state_icon}\n"
            f"• السرعة:   `{speed}ث` بين كل طلب\n"
            f"• النقاط:    `{score:,}` نقطة\n"
            f"• المهام:    `{count}/∞`\n"
            f"• الأخطاء:  `{errors}`\n"
            f"• آخر أمر:  `{order}`",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

# ========== التشغيل ==========
def main():
    log.info("🤖 البوت يشتغل — لا قاعدة بيانات، كل شيء في الذاكرة.")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            AWAITING_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token),
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

    log.info("✅ جاهز — اضغط Ctrl+C للإيقاف.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

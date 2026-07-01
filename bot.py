#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import logging
import threading
import random
from datetime import datetime

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ============================================================
#                     🔧 إعدادات البوت
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8411663176:AAEsI2yAj-mQQ6uspRrOI_yJPbCYEtjBbwo")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8287678319"))
API_URL = "https://api.tikspark.xyz/graphql"

# ============================================================
#                     📦 الهيدرز الثابتة (قابلة للتحديث)
# ============================================================
LOGIN_HEADERS = {
    "User-Agent": "okhttp/4.12.0",
    "Accept": "multipart/mixed; deferSpec=20220824, application/json",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "x-apollo-operation-id": "3522613813036d73817b2715e67743f8d23d7a85ad08b7e12aa3b29a24a17c43",
    "x-apollo-operation-name": "LoginAccount",
    "x-language": "ar",
    "x-app-name": "com.dev.vidspark",
    "x-device-info": '{"d":"30316661383133663939383030616638","n":"494e46494e495820496e66696e6978205836373238","o":"15","t":"d","v":"2.2.0","s":"0,0"}',
    "x-app-sig": "6024ed0395f78f2d27dc9823e678222d4bf0a99210975f20371c8aa15703f699",
    "x-app-ts": "1782921077469",
    "x-app-nonce": "a9ca074d062b43a4"
}

FETCH_HEADERS = {
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
    "x-app-sig": "2998306f19b3a98732a7150a785204d487ae22cb530a0bf4b1ff77a380ad7cd4",
    "x-app-ts": "1782443248827",
    "x-app-nonce": "18b79765e8e0458c",
}

ACTION_HEADERS_TEMPLATE = {
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
    "x-app-sig": "40a5102e6744f3bddca39e1ce6bb99ce942e20fe9382ba2463e1401d897eff43",
    "x-app-ts": "1782443501268",
    "x-app-nonce": "e69a0252b53843e4",
}

# ============================================================
#                     🧠 إدارة الجلسات
# ============================================================
class Session:
    def __init__(self, sid, username):
        self.id = sid
        self.username = username
        self.token = None
        self.running = False
        self.stop_flag = False
        self.thread = None
        self.speed = 0.2
        self.total_score = 0
        self.task_count = 0
        self.errors = 0
        self.last_score = 0
        self.start_time = None
        self.status_msg_id = None

sessions = {}
session_counter = 0
lock = threading.Lock()

# ============================================================
#                     🔐 دوال تسجيل الدخول والجمع
# ============================================================

def login_account(username, password):
    try:
        payload = {
            "operationName": "LoginAccount",
            "variables": {
                "data": {
                    "id": "",
                    "uniqueId": username,
                    "nickname": "",
                    "avatarMedium": "https://p16-common-sign.tiktokcdn.com/tos-alisg-avt-0068/709ff2826cb78c4b7ee81b8c69157606~tplv-tiktokx-cropcenter:720:720.webp?dr=14579&refresh_token=9b28df9e&x-expires=1783090800&x-signature=3HXYlIl2Fdv%2Fkl7xKX4SUO7SM1I%3D&t=4d5b0474&ps=13740610&shp=a5d48078&shcp=2472a6c6&idc=my2",
                    "followerCount": 31,
                    "followingCount": 87,
                    "videoCount": 0,
                    "privateAccount": False,
                    "diggCount": 0,
                    "authMethod": "local",
                    "password": password
                }
            },
            "query": """
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
        }
        resp = requests.post(API_URL, json=payload, headers=LOGIN_HEADERS, timeout=12)
        if resp.status_code != 200:
            return False, f"❌ كود {resp.status_code}", ""
        data = resp.json()
        errors = data.get("errors")
        if errors:
            return False, f"❌ {errors[0].get('message', 'خطأ')}", ""
        token = data.get("data", {}).get("loginTiktok", {}).get("accessToken")
        if not token:
            return False, "❌ لم يُرجع الخادم توكن.", ""
        return True, "✅ تم تسجيل الدخول", token
    except Exception as e:
        return False, f"❌ {e}", ""

def fetch_order_id(token):
    headers = FETCH_HEADERS.copy()
    headers["token"] = token
    payload = {
        "operationName": "FetchOrders",
        "variables": {"page": 2},
        "query": "query FetchOrders($page: Int!) { getOrders(page: $page) { _id } }"
    }
    try:
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        orders = data.get("data", {}).get("getOrders", [])
        return orders[0]["_id"] if orders else None
    except Exception:
        return None

def execute_order(token, order_id):
    headers = ACTION_HEADERS_TEMPLATE.copy()
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
                taskProgress {
                    count
                    startTime
                    taskProgressLimit
                }
            }
        }
        """
    }
    try:
        resp = requests.post(API_URL, json=payload, headers=headers, timeout=12)
        if resp.status_code != 200:
            return None
        data = resp.json()
        errors = data.get("errors")
        if errors:
            return {"error": errors[0].get("message", "")}
        return data.get("data", {}).get("actionOrder", {})
    except Exception:
        return None

def collector_loop(session: Session, bot, chat_id):
    session.running = True
    session.stop_flag = False
    session.start_time = datetime.now()
    token = session.token
    last_score = 0

    def send_update(text):
        try:
            if session.status_msg_id:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=session.status_msg_id,
                    text=text,
                    parse_mode="Markdown"
                )
            else:
                msg = bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                session.status_msg_id = msg.message_id
        except Exception:
            pass

    send_update("🚀 *بدأ الجمع...*\n⏳ انتظر أول تحديث.")

    consecutive_errors = 0
    while not session.stop_flag:
        try:
            order_id = fetch_order_id(token)
            if not order_id:
                time.sleep(2)
                continue

            result = execute_order(token, order_id)
            if result is None:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    send_update("🛑 *توقف بسبب أخطاء متتالية.*")
                    break
                time.sleep(3)
                continue

            if isinstance(result, dict) and "error" in result:
                if "Rate limit" in result["error"]:
                    time.sleep(1)
                    continue
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= 5:
                        send_update(f"⚠️ *خطأ:* {result['error']}\nتوقف.")
                        break
                    time.sleep(2)
                    continue

            consecutive_errors = 0
            score = result.get("score", last_score)
            progress = result.get("taskProgress", {})
            count = progress.get("count", session.task_count)

            if score < last_score:
                score = last_score

            gained = max(0, score - last_score)
            last_score = score
            session.last_score = last_score
            session.total_score = score
            session.task_count = count

            bar = "█" * min(count, 10) + "░" * (10 - min(count, 10))
            status = "⏳ تقدم" if gained == 0 else f"🎉 +{gained}"

            text = (
                f"📊 *{session.username}*\n"
                f"{status} | [{bar}] {count}/∞\n"
                f"🏆 الإجمالي: `{score:,}`\n"
                f"⚡ السرعة: {session.speed}ث"
            )
            send_update(text)

            time.sleep(session.speed)

        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors >= 5:
                send_update(f"💥 *خطأ غير متوقع:* {e}")
                break
            time.sleep(3)

    session.running = False
    send_update(f"🏁 *انتهت الجلسة*\n🏆 النهائي: `{session.total_score:,}`")

# ============================================================
#                     ⌨️ أزرار البوت
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ بدء جلسة جديدة", callback_data="new_session")],
        [InlineKeyboardButton("⏹ إيقاف الجلسة الحالية", callback_data="stop_session")],
        [InlineKeyboardButton("📊 حالة الجلسة الحالية", callback_data="status")],
        [InlineKeyboardButton("⚙️ تغيير السرعة", callback_data="speed_menu")],
        [InlineKeyboardButton("📋 كل الجلسات", callback_data="list_sessions")],
        [InlineKeyboardButton("🔄 تحديث الهيدرز (إدمن)", callback_data="update_headers")],
    ])

def speed_keyboard():
    speeds = [0.005, 0.02, 0.05, 0.2, 0.5, 1.0, 5.0]
    buttons = []
    row = []
    for s in speeds:
        row.append(InlineKeyboardButton(f"{s}ث", callback_data=f"speed_{s}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)

# ============================================================
#                     📨 معالجات البوت
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا البوت حصري للمالك.")
        return
    await update.message.reply_text(
        "🚀 *بوت TikSpark الحصري*\n\n"
        "أنت المالك، يمكنك فتح جلسات غير محدودة.\n"
        "استخدم الأزرار للتحكم.",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await query.edit_message_text("❌ غير مصرح.")
        return

    data = query.data
    chat_id = update.effective_chat.id

    if data.startswith("speed_"):
        speed = float(data.split("_")[1])
        with lock:
            if sessions:
                last_sid = list(sessions.keys())[-1]
                sessions[last_sid].speed = speed
        await query.edit_message_text(
            f"✅ تم ضبط السرعة إلى `{speed}ث`",
            parse_mode="Markdown",
            reply_markup=speed_keyboard()
        )
        return

    if data == "back_main":
        await query.edit_message_text(
            "🚀 *لوحة التحكم*",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
        return

    if data == "speed_menu":
        await query.edit_message_text(
            "⚙️ *اختر السرعة:*",
            parse_mode="Markdown",
            reply_markup=speed_keyboard()
        )
        return

    if data == "new_session":
        await query.edit_message_text(
            "👤 أرسل *اسم المستخدم* (TikTok):",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "username"
        return

    if data == "stop_session":
        with lock:
            if sessions:
                last_sid = list(sessions.keys())[-1]
                session = sessions[last_sid]
                if session.running:
                    session.stop_flag = True
                    await query.edit_message_text(
                        f"⏹ *جارٍ إيقاف الجلسة* `{session.username}`",
                        parse_mode="Markdown"
                    )
                else:
                    await query.edit_message_text("⚠️ لا توجد جلسة نشطة.")
            else:
                await query.edit_message_text("⚠️ لا توجد جلسات.")
        return

    if data == "status":
        with lock:
            if sessions:
                last_sid = list(sessions.keys())[-1]
                session = sessions[last_sid]
                status = "🟢 تعمل" if session.running else "🔴 متوقفة"
                text = (
                    f"📊 *حالة الجلسة*\n"
                    f"👤 {session.username}\n"
                    f"📌 الحالة: {status}\n"
                    f"⚡ السرعة: {session.speed}ث\n"
                    f"🏆 النقاط: `{session.total_score:,}`\n"
                    f"📋 المهام: {session.task_count}\n"
                    f"❌ الأخطاء: {session.errors}\n"
                    f"⏰ بدأت: {session.start_time.strftime('%H:%M:%S') if session.start_time else '—'}"
                )
                await query.edit_message_text(text, parse_mode="Markdown")
            else:
                await query.edit_message_text("⚠️ لا توجد جلسات.")
        return

    if data == "list_sessions":
        with lock:
            if not sessions:
                await query.edit_message_text("📭 لا توجد جلسات.")
                return
            text = "📋 *قائمة الجلسات:*\n\n"
            for sid, sess in sessions.items():
                status = "🟢" if sess.running else "🔴"
                text += f"{status} `{sess.username}` (ID: {sid}) - {sess.task_count} مهمة\n"
            text += "\nاستخدم /stop <id> لإيقاف جلسة."
            await query.edit_message_text(text, parse_mode="Markdown")
        return

    if data == "update_headers":
        await query.edit_message_text(
            "🔧 *تحديث الهيدرز*\n"
            "أرسل الهيدرز الجديدة على شكل JSON.\n"
            "مثال: `{\"x-app-sig\":\"...\", \"x-app-ts\":\"...\"}`\n"
            "سيتم تحديث LOGIN_HEADERS, FETCH_HEADERS, ACTION_HEADERS.",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "update_headers"
        return

    await query.edit_message_text("❌ أمر غير معروف.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    text = update.message.text.strip()
    awaiting = context.user_data.get("awaiting")

    if awaiting == "username":
        context.user_data["username"] = text
        context.user_data["awaiting"] = "password"
        await update.message.reply_text("🔑 أرسل *كلمة المرور*:", parse_mode="Markdown")
        return

    if awaiting == "password":
        username = context.user_data.get("username")
        password = text
        await update.message.reply_text("🔄 *جاري تسجيل الدخول...*", parse_mode="Markdown")
        success, msg, token = login_account(username, password)
        if not success:
            await update.message.reply_text(f"{msg}\nحاول مرة أخرى.")
            context.user_data.pop("awaiting", None)
            context.user_data.pop("username", None)
            return

        with lock:
            global session_counter
            session_counter += 1
            sid = session_counter
            session = Session(sid, username)
            session.token = token
            sessions[sid] = session

        bot = context.bot
        thread = threading.Thread(
            target=collector_loop,
            args=(session, bot, update.effective_chat.id),
            daemon=True
        )
        thread.start()
        session.thread = thread

        await update.message.reply_text(
            f"{msg}\n✅ بدأت الجلسة `{username}` (ID: {sid})",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
        context.user_data.pop("awaiting", None)
        context.user_data.pop("username", None)
        return

    if awaiting == "update_headers":
        try:
            new_headers = json.loads(text)
            global LOGIN_HEADERS, FETCH_HEADERS, ACTION_HEADERS_TEMPLATE
            if "x-app-sig" in new_headers:
                LOGIN_HEADERS["x-app-sig"] = new_headers["x-app-sig"]
                FETCH_HEADERS["x-app-sig"] = new_headers["x-app-sig"]
                ACTION_HEADERS_TEMPLATE["x-app-sig"] = new_headers["x-app-sig"]
            if "x-app-ts" in new_headers:
                LOGIN_HEADERS["x-app-ts"] = new_headers["x-app-ts"]
                FETCH_HEADERS["x-app-ts"] = new_headers["x-app-ts"]
                ACTION_HEADERS_TEMPLATE["x-app-ts"] = new_headers["x-app-ts"]
            if "x-app-nonce" in new_headers:
                LOGIN_HEADERS["x-app-nonce"] = new_headers["x-app-nonce"]
                FETCH_HEADERS["x-app-nonce"] = new_headers["x-app-nonce"]
                ACTION_HEADERS_TEMPLATE["x-app-nonce"] = new_headers["x-app-nonce"]
            if "x-csrf-token" in new_headers:
                FETCH_HEADERS["x-csrf-token"] = new_headers["x-csrf-token"]
                ACTION_HEADERS_TEMPLATE["x-csrf-token"] = new_headers["x-csrf-token"]
            await update.message.reply_text("✅ تم تحديث الهيدرز بنجاح!", reply_markup=main_keyboard())
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ في JSON: {e}")
        context.user_data.pop("awaiting", None)
        return

# ============================================================
#                     🚀 تشغيل البوت
# ============================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 البوت يعمل... (اضغط Ctrl+C للإيقاف)")
    app.run_polling()

if __name__ == "__main__":
    main()
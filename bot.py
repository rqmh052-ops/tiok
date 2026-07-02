#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import sqlite3
import logging
import threading
import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ============================================================
#                     🔧 إعدادات البيئة
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8411663176:AAEsI2yAj-mQQ6uspRrOI_yJPbCYEtjBbwo")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8287678319"))
API_URL = "https://api.tikspark.xyz/graphql"

# ============================================================
#                     📦 الهيدرز الثابتة
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
#                     🗄️ قاعدة البيانات (SQLite)
# ============================================================
DB_PATH = "data.sqlite"
BACKUP_DIR = "backups"
Path(BACKUP_DIR).mkdir(exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_admin INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            tiktok_username TEXT,
            token TEXT,
            speed REAL DEFAULT 0.2,
            is_running INTEGER DEFAULT 0,
            total_score INTEGER DEFAULT 0,
            task_count INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0,
            last_score INTEGER DEFAULT 0,
            start_time TEXT,
            status_msg_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS speed_presets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            speed REAL UNIQUE,
            label TEXT,
            display_order INTEGER
        )
    ''')
    default_speeds = [(0.005, '🚀 خارقة', 1),
                      (0.02, '⚡ سريعة جداً', 2),
                      (0.05, '🔥 سريعة', 3),
                      (0.2, '🐇 عادية', 4),
                      (0.5, '🐢 بطيئة', 5),
                      (1.0, '⏳ بطيئة جداً', 6),
                      (5.0, '🐌 أبطأ', 7)]
    for speed, label, order in default_speeds:
        c.execute('INSERT OR IGNORE INTO speed_presets (speed, label, display_order) VALUES (?, ?, ?)',
                  (speed, label, order))
    conn.commit()
    conn.close()

init_db()

def get_db_conn():
    return sqlite3.connect(DB_PATH)

def add_user(user_id, username):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def add_session(user_id, tiktok_username, token):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute('''
        INSERT INTO sessions (user_id, tiktok_username, token, start_time)
        VALUES (?, ?, ?, ?)
    ''', (user_id, tiktok_username, token, datetime.now().isoformat()))
    session_id = c.lastrowid
    conn.commit()
    conn.close()
    return session_id

def update_session(session_id, **kwargs):
    conn = get_db_conn()
    c = conn.cursor()
    for key, value in kwargs.items():
        c.execute(f'UPDATE sessions SET {key} = ? WHERE id = ?', (value, session_id))
    conn.commit()
    conn.close()

def get_session(session_id):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE id = ?', (session_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_sessions_by_user(user_id):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE user_id = ?', (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_sessions():
    conn = get_db_conn()
    c = conn.cursor()
    c.execute('SELECT * FROM sessions')
    rows = c.fetchall()
    conn.close()
    return rows

def get_speed_presets():
    conn = get_db_conn()
    c = conn.cursor()
    c.execute('SELECT speed, label FROM speed_presets ORDER BY display_order')
    rows = c.fetchall()
    conn.close()
    return rows

def add_speed_preset(speed, label, order):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute('INSERT INTO speed_presets (speed, label, display_order) VALUES (?, ?, ?)', (speed, label, order))
    conn.commit()
    conn.close()

def delete_speed_preset(speed):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute('DELETE FROM speed_presets WHERE speed = ?', (speed,))
    conn.commit()
    conn.close()

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

# ============================================================
#                     🧵 حلقة الجمع
# ============================================================
def collector_loop(session_id, bot, chat_id):
    session_data = get_session(session_id)
    if not session_data:
        return
    token = session_data[3]
    speed = session_data[4]
    update_session(session_id, is_running=1)
    last_score = session_data[9]

    def send_update(text):
        try:
            msg_id = session_data[11]
            if msg_id:
                bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode="Markdown")
            else:
                msg = bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                update_session(session_id, status_msg_id=msg.message_id)
        except Exception:
            pass

    send_update("🚀 *بدأ الجمع...*\n⏳ انتظر أول تحديث.")

    consecutive_errors = 0
    while True:
        current = get_session(session_id)
        if not current or current[5] == 0:
            break
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
                    update_session(session_id, is_running=0)
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
                        update_session(session_id, is_running=0)
                        break
                    time.sleep(2)
                    continue

            consecutive_errors = 0
            score = result.get("score", last_score)
            progress = result.get("taskProgress", {})
            count = progress.get("count", session_data[7])

            if score < last_score:
                score = last_score

            gained = max(0, score - last_score)
            last_score = score
            update_session(session_id, total_score=score, task_count=count, last_score=last_score)

            bar = "█" * min(count, 10) + "░" * (10 - min(count, 10))
            status = "⏳ تقدم" if gained == 0 else f"🎉 +{gained}"

            text = (
                f"📊 *{current[2]}*\n"
                f"{status} | [{bar}] {count}/∞\n"
                f"🏆 الإجمالي: `{score:,}`\n"
                f"⚡ السرعة: {speed}ث"
            )
            send_update(text)

            updated = get_session(session_id)
            speed = updated[4] if updated else speed
            time.sleep(speed)

        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors >= 5:
                send_update(f"💥 *خطأ غير متوقع:* {e}")
                update_session(session_id, is_running=0)
                break
            time.sleep(3)

    update_session(session_id, is_running=0)
    final = get_session(session_id)
    if final:
        send_update(f"🏁 *انتهت الجلسة*\n🏆 النهائي: `{final[6]:,}`")

# ============================================================
#                     ⌨️ أزرار البوت
# ============================================================

def main_keyboard(user_id):
    sessions = get_sessions_by_user(user_id)
    has_running = any(s[5] == 1 for s in sessions)
    keyboard = [
        [InlineKeyboardButton("▶️ بدء جلسة جديدة", callback_data="new_session")],
    ]
    if has_running:
        keyboard.append([InlineKeyboardButton("⏹ إيقاف الجلسة الحالية", callback_data="stop_session")])
    keyboard.append([InlineKeyboardButton("📊 حالة الجلسة", callback_data="status")])
    keyboard.append([InlineKeyboardButton("⚡ تغيير السرعة", callback_data="speed_menu")])
    keyboard.append([InlineKeyboardButton("📋 جلساتي", callback_data="my_sessions")])
    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("⚙️ لوحة الإدارة", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

def admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("📋 كل الجلسات", callback_data="list_all_sessions")],
        [InlineKeyboardButton("⚡ إدارة السرعات", callback_data="manage_speeds")],
        [InlineKeyboardButton("💾 نسخ احتياطي (تحميل)", callback_data="backup_db")],
        [InlineKeyboardButton("📂 استعادة قاعدة بيانات", callback_data="restore_db")],
        [InlineKeyboardButton("🔄 تحديث الهيدرز", callback_data="update_headers")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(keyboard)

def speed_keyboard(user_id):
    presets = get_speed_presets()
    buttons = []
    for speed, label in presets:
        buttons.append([InlineKeyboardButton(f"{label} ({speed}ث)", callback_data=f"set_speed_{speed}")])
    buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)

def manage_speeds_keyboard():
    presets = get_speed_presets()
    buttons = []
    for speed, label in presets:
        buttons.append([InlineKeyboardButton(f"❌ حذف {label} ({speed}ث)", callback_data=f"del_speed_{speed}")])
    buttons.append([InlineKeyboardButton("➕ إضافة سرعة جديدة", callback_data="add_speed")])
    buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)

# ============================================================
#                     📨 معالجات البوت
# ============================================================
AWAITING_USERNAME, AWAITING_PASSWORD, AWAITING_HEADERS, AWAITING_SPEED_NAME, AWAITING_SPEED_VALUE, AWAITING_RESTORE = range(6)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name
    add_user(user_id, username)
    await update.message.reply_text(
        f"👋 مرحباً {username}!\n\n🚀 *بوت TikSpark*\nاستخدم الأزرار للتحكم بجلساتك.",
        parse_mode="Markdown",
        reply_markup=main_keyboard(user_id)
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data
    chat_id = update.effective_chat.id

    if data == "new_session":
        await query.edit_message_text(
            "👤 أرسل *اسم المستخدم* (TikTok):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel")]])
        )
        return AWAITING_USERNAME

    if data == "stop_session":
        sessions = get_sessions_by_user(user_id)
        running = [s for s in sessions if s[5] == 1]
        if not running:
            await query.edit_message_text("⚠️ لا توجد جلسة نشطة.", reply_markup=main_keyboard(user_id))
            return
        session_id = running[0][0]
        update_session(session_id, is_running=0)
        await query.edit_message_text(
            f"⏹ *تم إيقاف الجلسة* `{running[0][2]}`",
            parse_mode="Markdown",
            reply_markup=main_keyboard(user_id)
        )
        return

    if data == "status":
        sessions = get_sessions_by_user(user_id)
        if not sessions:
            await query.edit_message_text("📭 لا توجد جلسات.", reply_markup=main_keyboard(user_id))
            return
        session = sessions[-1]
        status = "🟢 تعمل" if session[5] == 1 else "🔴 متوقفة"
        text = (
            f"📊 *حالة الجلسة*\n"
            f"👤 {session[2]}\n"
            f"📌 الحالة: {status}\n"
            f"⚡ السرعة: {session[4]}ث\n"
            f"🏆 النقاط: `{session[6]:,}`\n"
            f"📋 المهام: {session[7]}\n"
            f"❌ الأخطاء: {session[8]}\n"
            f"⏰ بدأت: {session[10][:16] if session[10] else '—'}"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_keyboard(user_id))
        return

    if data == "my_sessions":
        sessions = get_sessions_by_user(user_id)
        if not sessions:
            await query.edit_message_text("📭 لا توجد جلسات.", reply_markup=main_keyboard(user_id))
            return
        text = "📋 *جلساتي:*\n\n"
        for s in sessions:
            status = "🟢" if s[5] == 1 else "🔴"
            text += f"{status} `{s[2]}` - نقاط: {s[6]:,} - مهام: {s[7]}\n"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_keyboard(user_id))
        return

    if data == "speed_menu":
        await query.edit_message_text(
            "⚡ *اختر السرعة:*",
            parse_mode="Markdown",
            reply_markup=speed_keyboard(user_id)
        )
        return

    if data.startswith("set_speed_"):
        speed = float(data.split("_")[2])
        sessions = get_sessions_by_user(user_id)
        if not sessions:
            await query.edit_message_text("⚠️ لا توجد جلسة لتعديل سرعتها.", reply_markup=main_keyboard(user_id))
            return
        session_id = sessions[-1][0]
        update_session(session_id, speed=speed)
        await query.edit_message_text(
            f"✅ تم ضبط السرعة إلى `{speed}ث`",
            parse_mode="Markdown",
            reply_markup=main_keyboard(user_id)
        )
        return

    # Admin panel
    if user_id == ADMIN_ID:
        if data == "admin_panel":
            await query.edit_message_text("⚙️ *لوحة الإدارة*", parse_mode="Markdown", reply_markup=admin_keyboard())
            return

        if data == "list_all_sessions":
            sessions = get_all_sessions()
            if not sessions:
                await query.edit_message_text("📭 لا توجد جلسات.", reply_markup=admin_keyboard())
                return
            text = "📋 *جميع الجلسات:*\n\n"
            for s in sessions:
                status = "🟢" if s[5] == 1 else "🔴"
                text += f"{status} المستخدم: {s[1]}, حساب: `{s[2]}`, نقاط: {s[6]:,}\n"
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=admin_keyboard())
            return

        if data == "manage_speeds":
            await query.edit_message_text("⚡ *إدارة السرعات*", parse_mode="Markdown", reply_markup=manage_speeds_keyboard())
            return

        if data.startswith("del_speed_"):
            speed = float(data.split("_")[2])
            delete_speed_preset(speed)
            await query.edit_message_text(f"✅ تم حذف السرعة `{speed}ث`", parse_mode="Markdown", reply_markup=manage_speeds_keyboard())
            return

        if data == "add_speed":
            await query.edit_message_text(
                "✏️ أرسل *السرعة* (رقم عشري) أولاً، ثم في الرسالة التالية أرسل *الاسم*.\nمثال: `0.1` ثم `⚡ سريع`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
            )
            context.user_data["add_speed_step"] = "value"
            return

        if data == "backup_db":
            backup_path = BACKUP_DIR + "/backup_latest.sqlite"
            shutil.copy(DB_PATH, backup_path)
            with open(backup_path, 'rb') as f:
                await context.bot.send_document(chat_id=chat_id, document=f, filename="data_backup.sqlite")
            await query.edit_message_text("✅ تم إرسال نسخة احتياطية.", reply_markup=admin_keyboard())
            return

        if data == "restore_db":
            await query.edit_message_text(
                "📂 أرسل ملف قاعدة البيانات (`.sqlite`) لاستعادته.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
            )
            context.user_data["awaiting_restore"] = True
            return

        if data == "update_headers":
            await query.edit_message_text(
                "🔧 أرسل الهيدرز الجديدة على شكل JSON.\n"
                "مثال:\n```json\n{\n  \"x-app-sig\": \"new_sig\",\n  \"x-app-ts\": \"new_ts\",\n  \"x-app-nonce\": \"new_nonce\",\n  \"x-csrf-token\": \"new_token\"\n}\n```\nسيتم تحديث جميع الهيدرز.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
            )
            context.user_data["awaiting_headers"] = True
            return

    if data == "back_main":
        await query.edit_message_text("🚀 *القائمة الرئيسية*", parse_mode="Markdown", reply_markup=main_keyboard(user_id))
        return

    if data == "cancel":
        await query.edit_message_text("❌ تم الإلغاء.", reply_markup=main_keyboard(user_id))
        return

    await query.edit_message_text("❌ أمر غير معروف.", reply_markup=main_keyboard(user_id))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if context.user_data.get("awaiting") == "username":
        context.user_data["username"] = text
        context.user_data["awaiting"] = "password"
        await update.message.reply_text(
            "🔑 أرسل *كلمة المرور*:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="cancel")]])
        )
        return

    if context.user_data.get("awaiting") == "password":
        username = context.user_data.get("username")
        password = text
        await update.message.reply_text("🔄 *جاري تسجيل الدخول...*", parse_mode="Markdown")
        success, msg, token = login_account(username, password)
        if not success:
            await update.message.reply_text(f"{msg}\nحاول مرة أخرى.", reply_markup=main_keyboard(user_id))
            context.user_data.pop("awaiting", None)
            context.user_data.pop("username", None)
            return

        session_id = add_session(user_id, username, token)
        bot = context.bot
        thread = threading.Thread(target=collector_loop, args=(session_id, bot, chat_id), daemon=True)
        thread.start()

        await update.message.reply_text(
            f"{msg}\n✅ بدأت الجلسة `{username}` (ID: {session_id})",
            parse_mode="Markdown",
            reply_markup=main_keyboard(user_id)
        )
        context.user_data.pop("awaiting", None)
        context.user_data.pop("username", None)
        return

    if context.user_data.get("awaiting_headers"):
        try:
            new_headers = json.loads(text)
            global LOGIN_HEADERS, FETCH_HEADERS, ACTION_HEADERS_TEMPLATE
            for key in ["x-app-sig", "x-app-ts", "x-app-nonce", "x-csrf-token"]:
                if key in new_headers:
                    val = new_headers[key]
                    LOGIN_HEADERS[key] = val
                    FETCH_HEADERS[key] = val
                    ACTION_HEADERS_TEMPLATE[key] = val
            await update.message.reply_text("✅ تم تحديث الهيدرز بنجاح!", reply_markup=admin_keyboard())
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ في JSON: {e}")
        context.user_data.pop("awaiting_headers", None)
        return

    if context.user_data.get("add_speed_step") == "value":
        try:
            speed = float(text)
            context.user_data["new_speed"] = speed
            context.user_data["add_speed_step"] = "label"
            await update.message.reply_text("✏️ الآن أرسل *اسم* هذه السرعة:", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ قيمة غير صالحة. أرسل رقماً عشرياً.")
        return

    if context.user_data.get("add_speed_step") == "label":
        label = text
        speed = context.user_data.get("new_speed")
        if speed is None:
            await update.message.reply_text("❌ حدث خطأ، ابدأ من جديد.")
            context.user_data.pop("add_speed_step", None)
            return
        conn = get_db_conn()
        c = conn.cursor()
        c.execute('SELECT MAX(display_order) FROM speed_presets')
        max_order = c.fetchone()[0] or 0
        add_speed_preset(speed, label, max_order + 1)
        conn.close()
        await update.message.reply_text(f"✅ تم إضافة السرعة `{speed}ث` باسم `{label}`", reply_markup=admin_keyboard())
        context.user_data.pop("add_speed_step", None)
        context.user_data.pop("new_speed", None)
        return

    if context.user_data.get("awaiting_restore"):
        if update.message.document:
            file = await update.message.document.get_file()
            file_path = "restored.sqlite"
            await file.download_to_drive(file_path)
            shutil.copy(file_path, DB_PATH)
            os.remove(file_path)
            await update.message.reply_text("✅ تم استعادة قاعدة البيانات!", reply_markup=admin_keyboard())
        else:
            await update.message.reply_text("❌ أرسل ملف قاعدة بيانات بصيغة .sqlite")
        context.user_data.pop("awaiting_restore", None)
        return

    await update.message.reply_text("❌ أمر غير معروف.", reply_markup=main_keyboard(user_id))

# ============================================================
#                     ⏰ النسخ الاحتياطي التلقائي
# ============================================================
def auto_backup(context):
    backup_count_file = BACKUP_DIR + "/backup_count.txt"
    if os.path.exists(backup_count_file):
        with open(backup_count_file, 'r') as f:
            count = int(f.read().strip())
    else:
        count = 1

    backup_name = f"backup_{count}.sqlite"
    backup_path = BACKUP_DIR + "/" + backup_name
    shutil.copy(DB_PATH, backup_path)

    try:
        with open(backup_path, 'rb') as f:
            context.bot.send_document(chat_id=ADMIN_ID, document=f, filename=backup_name)
    except Exception as e:
        logging.error(f"فشل إرسال النسخة الاحتياطية: {e}")

    prev_count = count - 1
    if prev_count >= 1:
        prev_path = BACKUP_DIR + f"/backup_{prev_count}.sqlite"
        if os.path.exists(prev_path):
            os.remove(prev_path)

    count += 1
    with open(backup_count_file, 'w') as f:
        f.write(str(count))

# ============================================================
#                     🚀 تشغيل البوت
# ============================================================

def main():
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))

    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(auto_backup, interval=timedelta(minutes=30), first=60)

    print("🚀 البوت يعمل... (اضغط Ctrl+C للإيقاف)")
    app.run_polling()

if __name__ == "__main__":
    main()
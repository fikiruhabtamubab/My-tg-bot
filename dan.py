import os
import logging
import sqlite3
import io
import random
import threading
from datetime import datetime, date
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    ReplyKeyboardMarkup, Update, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.error import BadRequest, Forbidden

# --- 1. CONFIGURATION & SECRETS ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "5815604554")
PORT = int(os.getenv("PORT", 8080))

try:
    ADMIN_ID = int(ADMIN_ID_RAW.strip())
except:
    ADMIN_ID = 5815604554

DB_FILE = "user_data.db"
REFERRAL_BONUS = 0.05
DAILY_BONUS = 0.05
MIN_WITHDRAWAL = 5.00

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

class State(Enum):
    # Task Add
    T_NAME = 1; T_ID = 2; T_URL = 3; T_REWARD = 4
    # Withdraw
    W_NET = 5; W_ADDR = 6; W_AMT = 7
    # Mail
    M_MSG = 8; M_BTN = 9; M_BTN_DATA = 10
    # Track Main
    TR_NAME = 11; TR_ID = 12; TR_URL = 13
    # Coupon
    C_BUDGET = 14; C_CLAIMS = 15; C_CODE = 16
    # Coupon Track
    CT_NAME = 17; CT_ID = 18; CT_URL = 19

# --- 2. DATABASE HELPER ---
def db_query(query, params=(), commit=False, fetchone=False, fetchall=False):
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        if commit: conn.commit()
        if fetchone: return cursor.fetchone()
        if fetchall: return cursor.fetchall()
        return cursor

def setup_database():
    queries = [
        "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0, last_bonus_claim DATE, referred_by INTEGER, referral_count INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS tasks (task_id INTEGER PRIMARY KEY AUTOINCREMENT, task_name TEXT, reward REAL, target_chat_id TEXT, task_url TEXT, status TEXT DEFAULT 'active')",
        "CREATE TABLE IF NOT EXISTS completed_tasks (user_id INTEGER, task_id INTEGER, PRIMARY KEY (user_id, task_id))",
        "CREATE TABLE IF NOT EXISTS withdrawals (withdrawal_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, network TEXT, wallet_address TEXT, status TEXT DEFAULT 'pending')",
        "CREATE TABLE IF NOT EXISTS forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')",
        "CREATE TABLE IF NOT EXISTS coupons (coupon_code TEXT PRIMARY KEY, budget REAL, max_claims INTEGER, claims_count INTEGER DEFAULT 0, status TEXT DEFAULT 'active')",
        "CREATE TABLE IF NOT EXISTS claimed_coupons (user_id INTEGER, coupon_code TEXT, PRIMARY KEY (user_id, coupon_code))",
        "CREATE TABLE IF NOT EXISTS coupon_forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')",
        "CREATE TABLE IF NOT EXISTS coupon_messages (coupon_code TEXT, chat_id INTEGER, message_id INTEGER, PRIMARY KEY (coupon_code, chat_id))"
    ]
    for q in queries: db_query(q, commit=True)

# --- 3. KEYBOARDS ---
def user_kb(uid):
    btns = [[KeyboardButton("💰 Balance"), KeyboardButton("👥 Referral")],
            [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📋 Tasks")],
            [KeyboardButton("💸 Withdraw"), KeyboardButton("🎟️ Coupon Code")]]
    if uid == ADMIN_ID: btns.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(btns, resize_keyboard=True)

def admin_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📧 Mailing"), KeyboardButton("📋 Task Management")],
        [KeyboardButton("🎟️ Coupon Management"), KeyboardButton("📊 Bot Stats")],
        [KeyboardButton("🏧 Withdrawals"), KeyboardButton("🔗 Main Track Management")],
        [KeyboardButton("⬅️ Back to User Menu")]
    ], resize_keyboard=True)

# --- 4. ACCESS CONTROL ---
async def get_unjoined(uid, context, table='forced_channels'):
    channels = db_query(f"SELECT channel_name, channel_id, channel_url FROM {table} WHERE status='active'", fetchall=True)
    unjoined = []
    for name, cid, url in channels:
        try:
            m = await context.bot.get_chat_member(cid, uid)
            if m.status not in ['member', 'administrator', 'creator']: unjoined.append({'name': name, 'url': url})
        except: unjoined.append({'name': name, 'url': url})
    return unjoined

async def gatekeeper(update, context):
    if update.effective_user.id == ADMIN_ID: return True
    unjoined = await get_unjoined(update.effective_user.id, context)
    if unjoined:
        kb = [[InlineKeyboardButton(f"Join {c['name']}", url=c['url'])] for c in unjoined]
        kb.append([InlineKeyboardButton("✅ Done, Try Again", callback_data="check_join")])
        msg = "⚠️ **Action Required**\nPlease join our channels to continue:"
        target = update.message or update.callback_query.message
        await target.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return False
    return True

# --- 5. USER HANDLERS ---
async def start(update, context):
    user = update.effective_user
    if context.args and not db_query("SELECT 1 FROM users WHERE user_id=?", (user.id,), fetchone=True):
        try:
            ref_id = int(context.args[0])
            if ref_id != user.id:
                db_query("INSERT OR IGNORE INTO users (user_id, username, balance, referred_by) VALUES (?,?,?,?)", (user.id, user.username, REFERRAL_BONUS, ref_id), commit=True)
                db_query("UPDATE users SET balance=balance+?, referral_count=referral_count+1 WHERE user_id=?", (REFERRAL_BONUS, ref_id), commit=True)
                try: await context.bot.send_message(ref_id, f"🎉 New Referral! You earned ${REFERRAL_BONUS}")
                except: pass
        except: pass
    db_query("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)", (user.id, user.username), commit=True)
    await update.message.reply_text(f"👋 Welcome {user.first_name}!", reply_markup=user_kb(user.id))

async def balance_cmd(update, context):
    if not await gatekeeper(update, context): return
    res = db_query("SELECT balance FROM users WHERE user_id=?", (update.effective_user.id,), fetchone=True)
    await update.message.reply_text(f"💰 Balance: **${res[0]:.2f}**", parse_mode='Markdown')

async def referral_cmd(update, context):
    if not await gatekeeper(update, context): return
    uid = update.effective_user.id
    res = db_query("SELECT referral_count FROM users WHERE user_id=?", (uid,), fetchone=True)
    bot = await context.bot.get_me()
    link = f"https://t.me/{bot.username}?start={uid}"
    await update.message.reply_text(f"👥 Referrals: {res[0]}\nLink: `{link}`", parse_mode='Markdown')

async def daily_cmd(update, context):
    if not await gatekeeper(update, context): return
    uid, today = update.effective_user.id, date.today().isoformat()
    res = db_query("SELECT last_bonus_claim FROM users WHERE user_id=?", (uid,), fetchone=True)
    if res and res[0] == today: await update.message.reply_text("❌ Already claimed today!"); return
    db_query("UPDATE users SET balance=balance+?, last_bonus_claim=? WHERE user_id=?", (DAILY_BONUS, today, uid), commit=True)
    await update.message.reply_text(f"🎁 Claimed ${DAILY_BONUS} bonus!")

# --- 6. TASK SYSTEM ---
async def task_list(update, context):
    if not await gatekeeper(update, context): return
    uid = update.effective_user.id
    t = db_query("SELECT task_id, task_name, reward, task_url FROM tasks WHERE status='active' AND task_id NOT IN (SELECT task_id FROM completed_tasks WHERE user_id=?) LIMIT 1", (uid,), fetchone=True)
    if not t: await update.message.reply_text("✅ All tasks completed!"); return
    kb = [[InlineKeyboardButton("➡️ Go to Task", url=t[3])], [InlineKeyboardButton("✅ Joined", callback_data=f"v_t_{t[0]}")]]
    await update.message.reply_text(f"📋 **{t[1]}**\nReward: ${t[2]:.2f}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# --- 7. ADMIN: MAILING CONVERSATION ---
async def m_start(update, context):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    await update.message.reply_text("Send broadcast message:", reply_markup=ReplyKeyboardRemove())
    return State.M_MSG

async def m_get_msg(update, context):
    context.user_data['m_msg'] = update.message; context.user_data['m_btns'] = []
    kb = [[InlineKeyboardButton("➕ Add Button", callback_data="m_btn"), InlineKeyboardButton("🚀 Send Now", callback_data="m_send")]]
    await update.message.reply_text("Add a URL button or send?", reply_markup=InlineKeyboardMarkup(kb))
    return State.M_BTN

async def m_get_btn(update, context):
    try:
        txt, url = update.message.text.split(' - ', 1)
        context.user_data['m_btns'].append(InlineKeyboardButton(txt.strip(), url=url.strip()))
        kb = [[InlineKeyboardButton("🚀 Send Now", callback_data="m_send")]]
        if len(context.user_data['m_btns']) < 3: kb[0].insert(0, InlineKeyboardButton("➕ Add Another", callback_data="m_btn"))
        await update.message.reply_text(f"Added {len(context.user_data['m_btns'])}/3", reply_markup=InlineKeyboardMarkup(kb))
        return State.M_BTN
    except: await update.message.reply_text("Use: Text - URL"); return State.M_BTN_DATA

# --- 8. ADMIN: TASK ADD CONVERSATION ---
async def t_start(update, context):
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("Task Name:", reply_markup=ReplyKeyboardRemove())
    return State.T_NAME

async def t_name(update, context):
    context.user_data['t_n'] = update.message.text
    await update.message.reply_text("Channel ID (e.g. @mychan):")
    return State.T_ID

async def t_id(update, context):
    context.user_data['t_i'] = update.message.text
    await update.message.reply_text("Public Link (https://t.me/...):")
    return State.T_URL

async def t_url(update, context):
    context.user_data['t_u'] = update.message.text
    await update.message.reply_text("Reward (e.g. 0.10):")
    return State.T_REWARD

async def t_final(update, context):
    try:
        r = float(update.message.text)
        db_query("INSERT INTO tasks (task_name, reward, target_chat_id, task_url) VALUES (?,?,?,?)", (context.user_data['t_n'], r, context.user_data['t_i'], context.user_data['t_u']), commit=True)
        await update.message.reply_text("✅ Task Added!", reply_markup=admin_kb())
        return ConversationHandler.END
    except: await update.message.reply_text("Enter a number:"); return State.T_REWARD

# --- 9. CALLBACK HANDLER (APPROVE/DELETE/ETC) ---
async def handle_callback(update, context):
    q = update.callback_query; d = q.data; uid = q.from_user.id
    await q.answer()
    if d == "check_join": await gatekeeper(update, context)
    elif d == "m_btn": await q.edit_message_text("Send: `Button Text - https://link.com`", parse_mode='Markdown'); return State.M_BTN_DATA
    elif d == "m_send":
        users = db_query("SELECT user_id FROM users", fetchall=True)
        kb = InlineKeyboardMarkup([context.user_data['m_btns']]) if context.user_data['m_btns'] else None
        s, f = 0, 0
        for (target,) in users:
            try: await context.user_data['m_msg'].copy(target, reply_markup=kb); s += 1
            except: f += 1
        await q.message.reply_text(f"📢 Done! S:{s} F:{f}", reply_markup=admin_kb())
    elif d.startswith("approve_") or d.startswith("reject_"):
        act, wid = d.split("_")
        row = db_query("SELECT user_id, amount FROM withdrawals WHERE withdrawal_id=?", (wid,), fetchone=True)
        if act == "approve":
            db_query("UPDATE withdrawals SET status='approved' WHERE withdrawal_id=?", (wid,), commit=True)
            try: await context.bot.send_message(row[0], f"✅ Withdrawal of ${row[1]} Approved!")
            except: pass
        else:
            db_query("UPDATE withdrawals SET status='rejected' WHERE withdrawal_id=?", (wid,), commit=True)
            db_query("UPDATE users SET balance=balance+? WHERE user_id=?", (row[1], row[0]), commit=True)
            try: await context.bot.send_message(row[0], "❌ Withdrawal Rejected. Funds returned.")
            except: pass
        await q.message.delete()
    elif d.startswith("v_t_"):
        tid = d.split("_")[2]
        info = db_query("SELECT target_chat_id, reward FROM tasks WHERE task_id=?", (tid,), fetchone=True)
        try:
            m = await context.bot.get_chat_member(info[0], uid)
            if m.status in ['member', 'administrator', 'creator']:
                db_query("INSERT OR IGNORE INTO completed_tasks (user_id, task_id) VALUES (?,?)", (uid, tid), commit=True)
                db_query("UPDATE users SET balance=balance+? WHERE user_id=?", (info[1], uid), commit=True)
                await q.edit_message_text(f"✅ Verified! +${info[1]:.2f}")
            else: await q.answer("❌ Not joined!", show_alert=True)
        except: await q.answer("Bot Error: Bot must be admin in target channel.", show_alert=True)
    elif d == "admin_export_users":
        ids = db_query("SELECT user_id FROM users", fetchall=True)
        out = io.BytesIO("\n".join([str(i[0]) for i in ids]).encode()); out.name = "users.txt"
        await context.bot.send_document(update.effective_chat.id, out)

# --- 10. HEALTH CHECK & MAIN ---
class Health(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
def run_h(): HTTPServer(('0.0.0.0', PORT), Health).serve_forever()

def main():
    if not BOT_TOKEN: return
    setup_database()
    threading.Thread(target=run_h, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation Handlers
    app.add_handler(ConversationHandler(entry_points=[MessageHandler(filters.Regex("^📧 Mailing$"), m_start)], states={State.M_MSG: [MessageHandler(filters.ALL, m_get_msg)], State.M_BTN: [CallbackQueryHandler(handle_callback)], State.M_BTN_DATA: [MessageHandler(filters.TEXT, m_get_btn)]}, fallbacks=[]))
    app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(t_start, pattern="^admin_add_task_start$")], states={State.T_NAME: [MessageHandler(filters.TEXT, t_name)], State.T_ID: [MessageHandler(filters.TEXT, t_id)], State.T_URL: [MessageHandler(filters.TEXT, t_url)], State.T_REWARD: [MessageHandler(filters.TEXT, t_final)]}, fallbacks=[]))

    # Static Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), balance_cmd))
    app.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), referral_cmd))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), daily_cmd))
    app.add_handler(MessageHandler(filters.Regex("^📋 Tasks$"), task_list))
    app.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), lambda u,c: u.message.reply_text("Admin", reply_markup=admin_kb()) if u.effective_user.id == ADMIN_ID else None))
    app.add_handler(MessageHandler(filters.Regex("^📊 Bot Stats$"), lambda u,c: u.message.reply_text("Stats", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📥 Export", callback_data="admin_export_users")]])) if u.effective_user.id == ADMIN_ID else None))
    app.add_handler(MessageHandler(filters.Regex("^📋 Task Management$"), lambda u,c: u.message.reply_text("Tasks", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add", callback_data="admin_add_task_start")]])) if u.effective_user.id == ADMIN_ID else None))
    app.add_handler(MessageHandler(filters.Regex("^🏧 Withdrawals$"), lambda u,c: [u.message.reply_text(f"ID:{w[0]} @{w[1]} Amt:{w[2]}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Approve", callback_data=f"approve_{w[0]}"), InlineKeyboardButton("Reject", callback_data=f"reject_{w[0]}")]])) for w in db_query("SELECT w.withdrawal_id, u.username, w.amount FROM withdrawals w JOIN users u ON w.user_id = u.user_id WHERE w.status = 'pending'", fetchall=True)] if u.effective_user.id == ADMIN_ID else None))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), start))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling()

if __name__ == "__main__": main() = logging.getLogger(__name__)

class State(Enum):
    GET_TASK_NAME = 1; GET_TARGET_CHAT_ID = 2; GET_TASK_URL = 3; GET_TASK_REWARD = 4
    CHOOSE_WITHDRAW_NETWORK = 5; GET_WALLET_ADDRESS = 6; GET_WITHDRAW_AMOUNT = 7
    GET_MAIL_MESSAGE = 8; AWAIT_BUTTON_OR_SEND = 9; GET_BUTTON_DATA = 10
    GET_TRACKED_NAME = 11; GET_TRACKED_ID = 12; GET_TRACKED_URL = 13
    GET_COUPON_BUDGET = 14; GET_COUPON_MAX_CLAIMS = 15; AWAIT_COUPON_CODE = 16
    GET_COUPON_TRACKED_NAME = 17; GET_COUPON_TRACKED_ID = 18; GET_COUPON_TRACKED_URL = 19

# --- 2. DATABASE SETUP ---
def setup_database():
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0, last_bonus_claim DATE, referred_by INTEGER, referral_count INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS tasks (task_id INTEGER PRIMARY KEY AUTOINCREMENT, task_name TEXT NOT NULL, reward REAL NOT NULL, target_chat_id TEXT NOT NULL, task_url TEXT NOT NULL, status TEXT DEFAULT 'active')")
        c.execute("CREATE TABLE IF NOT EXISTS completed_tasks (user_id INTEGER, task_id INTEGER, PRIMARY KEY (user_id, task_id))")
        c.execute("CREATE TABLE IF NOT EXISTS withdrawals (withdrawal_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL, network TEXT NOT NULL, wallet_address TEXT NOT NULL, status TEXT DEFAULT 'pending', request_date DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (user_id) REFERENCES users (user_id))")
        c.execute("CREATE TABLE IF NOT EXISTS forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')")
        c.execute("CREATE TABLE IF NOT EXISTS coupons (coupon_code TEXT PRIMARY KEY, budget REAL NOT NULL, max_claims INTEGER NOT NULL, claims_count INTEGER DEFAULT 0, status TEXT DEFAULT 'active', creation_date DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS claimed_coupons (user_id INTEGER, coupon_code TEXT, PRIMARY KEY (user_id, coupon_code))")
        c.execute("CREATE TABLE IF NOT EXISTS coupon_forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')")
        c.execute("CREATE TABLE IF NOT EXISTS coupon_messages (coupon_code TEXT, chat_id INTEGER, message_id INTEGER, PRIMARY KEY (coupon_code, chat_id))")
        conn.commit()

# --- 3. KEYBOARDS ---
def get_user_keyboard(user_id):
    user_buttons = [[KeyboardButton("💰 Balance"), KeyboardButton("👥 Referral")], [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📋 Tasks")], [KeyboardButton("💸 Withdraw"), KeyboardButton("🎟️ Coupon Code")]]
    if user_id == ADMIN_ID: user_buttons.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(user_buttons, resize_keyboard=True)

def get_admin_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📧 Mailing"), KeyboardButton("📋 Task Management")],
        [KeyboardButton("🎟️ Coupon Management"), KeyboardButton("📊 Bot Stats")],
        [KeyboardButton("🏧 Withdrawals"), KeyboardButton("🔗 Main Track Management")],
        [KeyboardButton("⬅️ Back to User Menu")],
    ], resize_keyboard=True)

# --- 4. FORCED JOIN LOGIC ---
async def get_unjoined_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE, table_name: str) -> list:
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        tracked_channels = conn.execute(f"SELECT channel_name, channel_id, channel_url FROM {table_name} WHERE status = 'active'").fetchall()
    unjoined = []
    for name, channel_id, url in tracked_channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                unjoined.append({'name': name, 'url': url})
        except: unjoined.append({'name': name, 'url': url})
    return unjoined

async def is_member_or_send_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_user.id == ADMIN_ID: return True
    unjoined = await get_unjoined_channels(update.effective_user.id, context, 'forced_channels')
    if unjoined:
        kb = [[InlineKeyboardButton(f"➡️ Join {c['name']}", url=c['url'])] for c in unjoined]
        kb.append([InlineKeyboardButton("✅ Done, Try Again", callback_data="clear_join_message")])
        target = update.message or update.callback_query.message
        await target.reply_text("⚠️ **Action Required**\nJoin our channel(s) to use the bot:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return False
    return True

async def gatekeeper_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_member_or_send_join_message(update, context): raise Application.END

# --- 5. USER HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if context.args:
        try:
            ref_id = int(context.args[0])
            if ref_id != user.id: context.user_data['referrer_id'] = ref_id
        except: pass
    await check_membership_and_grant_access(update, context, 'verify_membership', 'forced_channels')

async def check_membership_and_grant_access(update: Update, context: ContextTypes.DEFAULT_TYPE, verify_callback: str, table_name: str):
    user = update.effective_user or update.callback_query.from_user
    unjoined = await get_unjoined_channels(user.id, context, table_name)
    if unjoined:
        kb = [[InlineKeyboardButton(f"➡️ Join {c['name']}", url=c['url'])] for c in unjoined]
        kb.append([InlineKeyboardButton("✅ I Have Joined", callback_data=verify_callback)])
        msg = "⚠️ **Please join our channels to proceed:**"
        if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else: await update.effective_message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return 'CONTINUE'

    if update.callback_query: await update.callback_query.message.delete()
    
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        is_new = c.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,)).fetchone() is None
        if verify_callback != 'verify_coupon_membership':
            ref_id = context.user_data.get('referrer_id')
            if is_new and ref_id and c.execute("SELECT 1 FROM users WHERE user_id=?", (ref_id,)).fetchone():
                c.execute("INSERT INTO users (user_id, username, balance, referred_by) VALUES (?,?,?,?)", (user.id, user.username, REFERRAL_BONUS, ref_id))
                c.execute("UPDATE users SET balance=balance+?, referral_count=referral_count+1 WHERE user_id=?", (REFERRAL_BONUS, ref_id))
                try: await context.bot.send_message(ref_id, f"✅ New referral! You earned ${REFERRAL_BONUS}")
                except: pass
            else:
                c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)", (user.id, user.username))
            conn.commit()
            await update.effective_message.reply_text(f"👋 Welcome {user.first_name}!", reply_markup=get_user_keyboard(user.id))
    
    if verify_callback == 'verify_coupon_membership':
        await update.effective_message.reply_text("✅ Verified! Send the coupon code now:")
        return 'PROCEED_TO_CODE'
    return ConversationHandler.END

async def handle_balance(update, context):
    if not await is_member_or_send_join_message(update, context): return
    with sqlite3.connect(DB_FILE) as conn:
        bal = conn.execute("SELECT balance FROM users WHERE user_id=?", (update.effective_user.id,)).fetchone()[0]
    await update.message.reply_text(f"💰 Your balance is: **${bal:.2f}**", parse_mode='Markdown')

async def handle_referral(update, context):
    if not await is_member_or_send_join_message(update, context): return
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        cnt = conn.execute("SELECT referral_count FROM users WHERE user_id=?", (uid,)).fetchone()[0]
    link = f"https://t.me/{(await context.bot.get_me()).username}?start={uid}"
    await update.message.reply_text(f"🚀 Referral Program\nEarn ${REFERRAL_BONUS} per friend!\n\nLink: `{link}`\nFriends: {cnt}", parse_mode='Markdown')

async def handle_daily_bonus(update, context):
    if not await is_member_or_send_join_message(update, context): return
    uid, today = update.effective_user.id, date.today().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        last = conn.execute("SELECT last_bonus_claim FROM users WHERE user_id=?", (uid,)).fetchone()[0]
        if last == today: await update.message.reply_text("❌ Already claimed today!"); return
        conn.execute("UPDATE users SET balance=balance+?, last_bonus_claim=? WHERE user_id=?", (DAILY_BONUS, today, uid))
        conn.commit()
    await update.message.reply_text(f"🎁 You earned ${DAILY_BONUS} bonus!")

# --- 6. ADMIN PANEL FUNCTIONALITIES ---
async def admin_panel_start(update, context):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("👑 Admin Control Panel", reply_markup=get_admin_keyboard())

async def handle_admin_stats(update, context):
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE) as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    kb = [[InlineKeyboardButton("📥 Export Users (.txt)", callback_data="admin_export_users")]]
    await update.message.reply_text(f"📊 **Bot Stats**\nTotal Users: {count}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def handle_admin_tasks(update, context):
    if update.effective_user.id != ADMIN_ID: return
    kb = [[InlineKeyboardButton("➕ Add New Task", callback_data="admin_add_task_start")], [InlineKeyboardButton("🗑️ Delete Task", callback_data="admin_delete_task_list")]]
    await update.message.reply_text("📋 **Task Management**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def handle_admin_withdrawals(update, context):
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE) as conn:
        ws = conn.execute("SELECT w.withdrawal_id, u.username, w.amount, w.network, w.wallet_address FROM withdrawals w JOIN users u ON w.user_id = u.user_id WHERE w.status = 'pending'").fetchall()
    if not ws: await update.message.reply_text("🏧 No pending withdrawals."); return
    for wid, name, amt, net, addr in ws:
        msg = f"ID: {wid} | User: @{name}\nAmount: ${amt} ({net})\nAddress: `{addr}`"
        kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wid}"), InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wid}")]]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def handle_coupon_management(update, context):
    if update.effective_user.id != ADMIN_ID: return
    kb = [[InlineKeyboardButton("➕ Create Coupon", callback_data="admin_create_coupon_start")],
          [InlineKeyboardButton("📜 Coupon History", callback_data="admin_coupon_history")],
          [InlineKeyboardButton("➕ Add Tracking Channel", callback_data="admin_add_coupon_tracked_start")],
          [InlineKeyboardButton("🗑️ Remove Tracking Channel", callback_data="admin_remove_coupon_tracked_list")]]
    await update.message.reply_text("🎟️ **Coupon Management**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def handle_admin_tracking(update, context):
    if update.effective_user.id != ADMIN_ID: return
    kb = [[InlineKeyboardButton("➕ Add Channel", callback_data="admin_add_tracked_start")], [InlineKeyboardButton("🗑️ Remove Channel", callback_data="admin_remove_tracked_list")]]
    await update.message.reply_text("🔗 **Main Tracking Management**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# --- 7. CONVERSATION LOGIC (MAILING, TASKS, COUPONS) ---
async def mailing_start(update, context):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    await update.message.reply_text("Send the message you want to broadcast:", reply_markup=ReplyKeyboardRemove())
    return State.GET_MAIL_MESSAGE

async def get_mail_message(update, context):
    context.user_data['m_msg'] = update.message; context.user_data['m_btns'] = []
    kb = [[InlineKeyboardButton("➕ Add URL Button", callback_data="m_add_btn"), InlineKeyboardButton("🚀 Send Now", callback_data="m_send")]]
    await update.message.reply_text("Add a button or broadcast now?", reply_markup=InlineKeyboardMarkup(kb))
    return State.AWAIT_BUTTON_OR_SEND

async def get_button_data(update, context):
    try:
        txt, url = update.message.text.split(' - ', 1)
        context.user_data['m_btns'].append(InlineKeyboardButton(txt.strip(), url=url.strip()))
        kb = [[InlineKeyboardButton("🚀 Send Now", callback_data="m_send")]]
        if len(context.user_data['m_btns']) < 3: kb[0].insert(0, InlineKeyboardButton("➕ Add More", callback_data="m_add_btn"))
        await update.message.reply_text(f"Button added ({len(context.user_data['m_btns'])}/3).", reply_markup=InlineKeyboardMarkup(kb))
        return State.AWAIT_BUTTON_OR_SEND
    except: await update.message.reply_text("Invalid format. Use: Text - https://link.com"); return State.GET_BUTTON_DATA

# --- 8. TASK/COUPON ADMIN FLOWS ---
async def add_task_start(update, context):
    await update.callback_query.message.delete()
    await update.callback_query.message.reply_text("Enter Task Name:", reply_markup=ReplyKeyboardRemove())
    return State.GET_TASK_NAME

async def create_coupon_start(update, context):
    await update.callback_query.message.delete()
    await update.callback_query.message.reply_text("Enter Coupon Budget (e.g. 100):", reply_markup=ReplyKeyboardRemove())
    return State.GET_COUPON_BUDGET

# --- 9. CALLBACK MASTER ---
async def main_callback_handler(update, context):
    query = update.callback_query; data = query.data; uid = query.from_user.id
    await query.answer()

    if data == "verify_membership": await check_membership_and_grant_access(update, context, 'verify_membership', 'forced_channels')
    elif data == "clear_join_message": await query.message.delete()
    elif data == "m_add_btn": await query.edit_message_text("Send details: `Button Text - https://url.com`", parse_mode='Markdown'); return State.GET_BUTTON_DATA
    elif data == "m_send":
        await query.message.delete(); log_msg = await query.message.reply_text("Broadcasting...")
        with sqlite3.connect(DB_FILE) as conn: users = conn.execute("SELECT user_id FROM users").fetchall()
        kb = InlineKeyboardMarkup([context.user_data['m_btns']]) if context.user_data['m_btns'] else None
        s, f = 0, 0
        for (tid,) in users:
            try: await context.user_data['m_msg'].copy(tid, reply_markup=kb); s += 1
            except: f += 1
        await log_msg.edit_text(f"📢 Broadcast Complete\n✅ Success: {s} | ❌ Fail: {f}")
    elif data.startswith("approve_") or data.startswith("reject_"):
        act, wid = data.split("_")
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute("SELECT user_id, amount FROM withdrawals WHERE withdrawal_id=?", (wid,)).fetchone()
            if act == "approve":
                conn.execute("UPDATE withdrawals SET status='approved' WHERE withdrawal_id=?", (wid,))
                try: await context.bot.send_message(row[0], f"✅ Withdrawal of ${row[1]} Approved!")
                except: pass
            else:
                conn.execute("UPDATE withdrawals SET status='rejected' WHERE withdrawal_id=?", (wid,))
                conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (row[1], row[0]))
                try: await context.bot.send_message(row[0], f"❌ Withdrawal Rejected. Funds returned.")
                except: pass
            conn.commit()
        await query.message.delete()
    elif data == "admin_export_users":
        with sqlite3.connect(DB_FILE) as conn: ids = conn.execute("SELECT user_id FROM users").fetchall()
        content = "\n".join([str(i[0]) for i in ids])
        bio = io.BytesIO(content.encode()); bio.name = "users.txt"
        await context.bot.send_document(update.effective_chat.id, bio)

# --- 10. CHOREO HEALTH CHECK & MAIN ---
class Health(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
def run_h(): HTTPServer(('0.0.0.0', PORT), Health).serve_forever()

def main():
    if not BOT_API_KEY: return
    setup_database()
    threading.Thread(target=run_h, daemon=True).start()
    app = Application.builder().token(BOT_API_KEY).build()

    # Conversation Handlers
    mail_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📧 Mailing$"), mailing_start)],
        states={
            State.GET_MAIL_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, get_mail_message)],
            State.AWAIT_BUTTON_OR_SEND: [CallbackQueryHandler(main_callback_handler)],
            State.GET_BUTTON_DATA: [MessageHandler(filters.TEXT, get_button_data)]
        }, fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)]
    )

    # Basic App Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), handle_balance))
    app.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), handle_referral))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), handle_daily_bonus))
    app.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), admin_panel_start))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), start))
    app.add_handler(MessageHandler(filters.Regex("^📊 Bot Stats$"), handle_admin_stats))
    app.add_handler(MessageHandler(filters.Regex("^📋 Task Management$"), handle_admin_tasks))
    app.add_handler(MessageHandler(filters.Regex("^🏧 Withdrawals$"), handle_admin_withdrawals))
    app.add_handler(MessageHandler(filters.Regex("^🎟️ Coupon Management$"), handle_coupon_management))
    app.add_handler(MessageHandler(filters.Regex("^🔗 Main Track Management$"), handle_admin_tracking))
    
    app.add_handler(mail_conv)
    app.add_handler(CallbackQueryHandler(main_callback_handler))

    logger.info("Bot is active on Choreo")
    app.run_polling()

if __name__ == "__main__":
    main().basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

class State(Enum):
    GET_TASK_NAME = 1; GET_TARGET_CHAT_ID = 2; GET_TASK_URL = 3; GET_TASK_REWARD = 4
    CHOOSE_WITHDRAW_NETWORK = 5; GET_WALLET_ADDRESS = 6; GET_WITHDRAW_AMOUNT = 7
    GET_MAIL_MESSAGE = 8; AWAIT_BUTTON_OR_SEND = 9; GET_BUTTON_DATA = 10
    GET_TRACKED_NAME = 11; GET_TRACKED_ID = 12; GET_TRACKED_URL = 13
    GET_COUPON_BUDGET = 14; GET_COUPON_MAX_CLAIMS = 15; AWAIT_COUPON_CODE = 16
    GET_COUPON_TRACKED_NAME = 17; GET_COUPON_TRACKED_ID = 18; GET_COUPON_TRACKED_URL = 19

# --- 2. DATABASE SETUP ---
def setup_database():
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0, last_bonus_claim DATE, referred_by INTEGER, referral_count INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS tasks (task_id INTEGER PRIMARY KEY AUTOINCREMENT, task_name TEXT NOT NULL, reward REAL NOT NULL, target_chat_id TEXT NOT NULL, task_url TEXT NOT NULL, status TEXT DEFAULT 'active')")
        c.execute("CREATE TABLE IF NOT EXISTS completed_tasks (user_id INTEGER, task_id INTEGER, PRIMARY KEY (user_id, task_id))")
        c.execute("CREATE TABLE IF NOT EXISTS withdrawals (withdrawal_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL, network TEXT NOT NULL, wallet_address TEXT NOT NULL, status TEXT DEFAULT 'pending', request_date DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')")
        c.execute("CREATE TABLE IF NOT EXISTS coupons (coupon_code TEXT PRIMARY KEY, budget REAL NOT NULL, max_claims INTEGER NOT NULL, claims_count INTEGER DEFAULT 0, status TEXT DEFAULT 'active')")
        c.execute("CREATE TABLE IF NOT EXISTS claimed_coupons (user_id INTEGER, coupon_code TEXT, PRIMARY KEY (user_id, coupon_code))")
        c.execute("CREATE TABLE IF NOT EXISTS coupon_forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')")
        c.execute("CREATE TABLE IF NOT EXISTS coupon_messages (coupon_code TEXT, chat_id INTEGER, message_id INTEGER, PRIMARY KEY (coupon_code, chat_id))")
        conn.commit()

# --- 3. KEYBOARDS ---
def get_user_keyboard(user_id):
    btns = [[KeyboardButton("💰 Balance"), KeyboardButton("👥 Referral")], 
            [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📋 Tasks")], 
            [KeyboardButton("💸 Withdraw"), KeyboardButton("🎟️ Coupon Code")]]
    if user_id == ADMIN_ID: btns.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(btns, resize_keyboard=True)

def get_admin_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📧 Mailing"), KeyboardButton("📋 Task Management")],
        [KeyboardButton("🎟️ Coupon Management"), KeyboardButton("📊 Bot Stats")],
        [KeyboardButton("🏧 Withdrawals"), KeyboardButton("🔗 Main Track Management")],
        [KeyboardButton("⬅️ Back to User Menu")]
    ], resize_keyboard=True)

# --- 4. MEMBERSHIP CHECKERS ---
async def get_unjoined_channels(user_id, context, table_name):
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        channels = conn.execute(f"SELECT channel_name, channel_id, channel_url FROM {table_name} WHERE status = 'active'").fetchall()
    unjoined = []
    for name, cid, url in channels:
        try:
            m = await context.bot.get_chat_member(cid, user_id)
            if m.status not in ['member', 'administrator', 'creator']: unjoined.append({'name': name, 'url': url})
        except: unjoined.append({'name': name, 'url': url})
    return unjoined

async def is_member_or_send_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_user.id == ADMIN_ID: return True
    unjoined = await get_unjoined_channels(update.effective_user.id, context, 'forced_channels')
    if unjoined:
        kb = [[InlineKeyboardButton(f"➡️ Join {c['name']}", url=c['url'])] for c in unjoined]
        kb.append([InlineKeyboardButton("✅ Done, Try Again", callback_data="clear_join_message")])
        target = update.message or update.callback_query.message
        await target.reply_text("⚠️ **Action Required**\nJoin our channels to continue:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return False
    return True

async def gatekeeper_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_member_or_send_join_message(update, context): raise Application.END

# --- 5. USER HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if context.args and len(context.args) > 0:
        try:
            ref_id = int(context.args[0])
            if ref_id != user.id: context.user_data['referrer_id'] = ref_id
        except: pass
    await check_membership_and_grant_access(update, context, 'verify_membership', 'forced_channels')

async def check_membership_and_grant_access(update: Update, context: ContextTypes.DEFAULT_TYPE, verify_callback: str, table_name: str):
    user = update.effective_user or update.callback_query.from_user
    unjoined = await get_unjoined_channels(user.id, context, table_name)
    if unjoined:
        kb = [[InlineKeyboardButton(f"➡️ Join {c['name']}", url=c['url'])] for c in unjoined]
        kb.append([InlineKeyboardButton("✅ I Have Joined", callback_data=verify_callback)])
        msg = "⚠️ **Join the channel(s) below to proceed:**"
        if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        else: await update.effective_message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return 'CONTINUE'

    if update.callback_query: await update.callback_query.message.delete()
    
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        is_new = c.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,)).fetchone() is None
        if verify_callback != 'verify_coupon_membership':
            ref_id = context.user_data.get('referrer_id')
            if is_new and ref_id and c.execute("SELECT 1 FROM users WHERE user_id=?", (ref_id,)).fetchone():
                c.execute("INSERT INTO users (user_id, username, balance, referred_by) VALUES (?,?,?,?)", (user.id, user.username, REFERRAL_BONUS, ref_id))
                c.execute("UPDATE users SET balance=balance+?, referral_count=referral_count+1 WHERE user_id=?", (REFERRAL_BONUS, ref_id))
                try: await context.bot.send_message(ref_id, f"✅ User {user.first_name} joined! You earned ${REFERRAL_BONUS}")
                except: pass
            else:
                c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)", (user.id, user.username))
            conn.commit()
            await update.effective_message.reply_text(f"👋 Welcome {user.first_name}!", reply_markup=get_user_keyboard(user.id))
    
    if verify_callback == 'verify_coupon_membership':
        await update.effective_message.reply_text("✅ Verified! Please send the coupon code:")
        return 'PROCEED_TO_CODE'
    return ConversationHandler.END

async def handle_balance(update, context):
    if not await is_member_or_send_join_message(update, context): return
    with sqlite3.connect(DB_FILE) as conn:
        bal = conn.execute("SELECT balance FROM users WHERE user_id=?", (update.effective_user.id,)).fetchone()[0]
    await update.message.reply_text(f"💰 Balance: **${bal:.2f}**", parse_mode='Markdown')

async def handle_referral(update, context):
    if not await is_member_or_send_join_message(update, context): return
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        cnt = conn.execute("SELECT referral_count FROM users WHERE user_id=?", (uid,)).fetchone()[0]
    link = f"https://t.me/{(await context.bot.get_me()).username}?start={uid}"
    await update.message.reply_text(f"👥 Referrals: {cnt}\nLink: `{link}`", parse_mode='Markdown')

async def handle_daily_bonus(update, context):
    if not await is_member_or_send_join_message(update, context): return
    uid, today = update.effective_user.id, date.today().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        last = conn.execute("SELECT last_bonus_claim FROM users WHERE user_id=?", (uid,)).fetchone()[0]
        if last == today: await update.message.reply_text("❌ Claimed today already!"); return
        conn.execute("UPDATE users SET balance=balance+?, last_bonus_claim=? WHERE user_id=?", (DAILY_BONUS, today, uid))
        conn.commit()
    await update.message.reply_text(f"🎁 You earned ${DAILY_BONUS}!")

# --- 6. ADMIN HANDLERS ---
async def admin_panel_start(update, context):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("👑 Admin Mode", reply_markup=get_admin_keyboard())

async def handle_admin_stats(update, context):
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE) as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    kb = [[InlineKeyboardButton("📥 Export IDs", callback_data="admin_export_users")]]
    await update.message.reply_text(f"📊 Total Users: {users}", reply_markup=InlineKeyboardMarkup(kb))

async def handle_admin_tasks(update, context):
    if update.effective_user.id != ADMIN_ID: return
    kb = [[InlineKeyboardButton("➕ Add Task", callback_data="admin_add_task_start")], [InlineKeyboardButton("🗑️ Delete Task", callback_data="admin_delete_task_list")]]
    await update.message.reply_text("📋 Task Management", reply_markup=InlineKeyboardMarkup(kb))

async def handle_admin_withdrawals(update, context):
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE) as conn:
        ws = conn.execute("SELECT w.withdrawal_id, u.username, w.amount, w.network, w.wallet_address FROM withdrawals w JOIN users u ON w.user_id = u.user_id WHERE w.status = 'pending'").fetchall()
    if not ws: await update.message.reply_text("🏧 No pending withdrawals."); return
    for wid, name, amt, net, addr in ws:
        msg = f"ID: {wid} | @{name}\nAmt: ${amt} ({net})\nAddr: `{addr}`"
        kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wid}"), InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wid}")]]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# --- 7. CONVERSATIONS ---
async def mailing_start(update, context):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    await update.message.reply_text("Send message to broadcast:", reply_markup=ReplyKeyboardRemove())
    return State.GET_MAIL_MESSAGE

async def get_mail_message(update, context):
    context.user_data['mail_msg'] = update.message; context.user_data['mail_btns'] = []
    kb = [[InlineKeyboardButton("➕ Add Button", callback_data="mail_add_button"), InlineKeyboardButton("🚀 Send Now", callback_data="mail_send_now")]]
    await update.message.reply_text("Add button or send?", reply_markup=InlineKeyboardMarkup(kb))
    return State.AWAIT_BUTTON_OR_SEND

async def get_button_data(update, context):
    try:
        txt, url = update.message.text.split(' - ', 1)
        context.user_data['mail_btns'].append(InlineKeyboardButton(txt.strip(), url=url.strip()))
        kb = [[InlineKeyboardButton("🚀 Send Now", callback_data="mail_send_now")]]
        if len(context.user_data['mail_btns']) < 3: kb[0].insert(0, InlineKeyboardButton("➕ Add More", callback_data="mail_add_button"))
        await update.message.reply_text(f"Added. {len(context.user_data['mail_btns'])}/3", reply_markup=InlineKeyboardMarkup(kb))
        return State.AWAIT_BUTTON_OR_SEND
    except: await update.message.reply_text("Format: Text - URL"); return State.GET_BUTTON_DATA

async def withdraw_start(update, context):
    if not await is_member_or_send_join_message(update, context): return ConversationHandler.END
    with sqlite3.connect(DB_FILE) as conn:
        bal = conn.execute("SELECT balance FROM users WHERE user_id=?", (update.effective_user.id,)).fetchone()[0]
    if bal < MIN_WITHDRAWAL_LIMIT: await update.message.reply_text(f"❌ Min Withdraw is ${MIN_WITHDRAWAL_LIMIT}"); return ConversationHandler.END
    kb = [[InlineKeyboardButton("BEP20", callback_data="w_net_BEP20"), InlineKeyboardButton("TRC20", callback_data="w_net_TRC20")]]
    await update.message.reply_text("Select Network:", reply_markup=InlineKeyboardMarkup(kb))
    return State.CHOOSE_WITHDRAW_NETWORK

# --- 8. CALLBACK MASTER ---
async def callback_handler(update, context):
    query = update.callback_query; data = query.data; uid = query.from_user.id
    await query.answer()
    
    if data == "verify_membership": await check_membership_and_grant_access(update, context, 'verify_membership', 'forced_channels')
    elif data == "clear_join_message": await query.message.delete()
    elif data.startswith("approve_") or data.startswith("reject_"):
        act, wid = data.split("_")
        with sqlite3.connect(DB_FILE) as conn:
            uid_w, amt = conn.execute("SELECT user_id, amount FROM withdrawals WHERE withdrawal_id=?", (wid,)).fetchone()
            if act == "approve":
                conn.execute("UPDATE withdrawals SET status='approved' WHERE withdrawal_id=?", (wid,))
                try: await context.bot.send_message(uid_w, f"🎉 Withdrawal of ${amt} Approved!")
                except: pass
            else:
                conn.execute("UPDATE withdrawals SET status='rejected' WHERE withdrawal_id=?", (wid,))
                conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, uid_w))
                try: await context.bot.send_message(uid_w, f"❌ Withdrawal Rejected. Funds returned.")
                except: pass
            conn.commit()
        await query.message.delete()
    elif data == "mail_send_now":
        await query.message.delete(); msg = await query.message.reply_text("Broadcasting...")
        with sqlite3.connect(DB_FILE) as conn: users = conn.execute("SELECT user_id FROM users").fetchall()
        reply_markup = InlineKeyboardMarkup([context.user_data['mail_btns']]) if context.user_data['mail_btns'] else None
        s, f = 0, 0
        for (target,) in users:
            try: await context.user_data['mail_msg'].copy(target, reply_markup=reply_markup); s += 1
            except: f += 1
        await msg.edit_text(f"Done! Sent: {s} | Fail: {f}")
        await query.message.reply_text("Admin Mode", reply_markup=get_admin_keyboard())
    elif data == "admin_export_users":
        with sqlite3.connect(DB_FILE) as conn: ids = conn.execute("SELECT user_id FROM users").fetchall()
        out = io.BytesIO(("\n".join([str(i[0]) for i in ids])).encode()); out.name = "users.txt"
        await context.bot.send_document(update.effective_chat.id, out)

# --- 9. HEALTH CHECK (CHOREO) ---
class Health(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
def run_h(): HTTPServer(('0.0.0.0', PORT), Health).serve_forever()

# --- 10. MAIN ---
def main():
    if not BOT_TOKEN: return
    setup_database()
    threading.Thread(target=run_h, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Conversations
    mail_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📧 Mailing$"), mailing_start)],
        states={
            State.GET_MAIL_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, get_mail_message)],
            State.AWAIT_BUTTON_OR_SEND: [CallbackQueryHandler(lambda u,c: State.GET_BUTTON_DATA, pattern="^mail_add_button$"), CallbackQueryHandler(callback_handler, pattern="^mail_send_now$")],
            State.GET_BUTTON_DATA: [MessageHandler(filters.TEXT, get_button_data)]
        }, fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)]
    )
    
    withdraw_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_start)],
        states={
            State.CHOOSE_WITHDRAW_NETWORK: [CallbackQueryHandler(lambda u,c: State.GET_WALLET_ADDRESS, pattern="^w_net_")],
            State.GET_WALLET_ADDRESS: [MessageHandler(filters.TEXT, lambda u,c: State.GET_WITHDRAW_AMOUNT)],
            State.GET_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT, lambda u,c: ConversationHandler.END)]
        }, fallbacks=[]
    )

    # Basic
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), handle_balance))
    app.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), handle_referral))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), handle_daily_bonus))
    app.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), admin_panel_start))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), start))
    app.add_handler(MessageHandler(filters.Regex("^📊 Bot Stats$"), handle_admin_stats))
    app.add_handler(MessageHandler(filters.Regex("^📋 Task Management$"), handle_admin_tasks))
    app.add_handler(MessageHandler(filters.Regex("^🏧 Withdrawals$"), handle_admin_withdrawals))
    app.add_handler(mail_conv); app.add_handler(withdraw_conv)
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.run_polling()

if __name__ == "__main__": main() = logging.getLogger(__name__)

class State(Enum):
    TASK_NAME = 1; TASK_CHAT = 2; TASK_URL = 3; TASK_REWARD = 4
    W_NET = 5; W_ADDR = 6; W_AMT = 7
    MAIL_MSG = 8; MAIL_BTN = 9; MAIL_BTN_DATA = 10
    TRK_NAME = 11; TRK_ID = 12; TRK_URL = 13
    CPN_BUDGET = 14; CPN_CLAIMS = 15; CPN_CODE = 16
    CPN_TRK_NAME = 17; CPN_TRK_ID = 18; CPN_TRK_URL = 19

# --- 2. DATABASE HELPER ---
def db_query(query, params=(), commit=False, fetchone=False, fetchall=False):
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        if commit: conn.commit()
        if fetchone: return cursor.fetchone()
        if fetchall: return cursor.fetchall()
        return cursor

def setup_database():
    queries = [
        "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0, last_bonus_claim DATE, referred_by INTEGER, referral_count INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS tasks (task_id INTEGER PRIMARY KEY AUTOINCREMENT, task_name TEXT, reward REAL, target_chat_id TEXT, task_url TEXT, status TEXT DEFAULT 'active')",
        "CREATE TABLE IF NOT EXISTS completed_tasks (user_id INTEGER, task_id INTEGER, PRIMARY KEY (user_id, task_id))",
        "CREATE TABLE IF NOT EXISTS withdrawals (withdrawal_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, network TEXT, wallet_address TEXT, status TEXT DEFAULT 'pending')",
        "CREATE TABLE IF NOT EXISTS forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')",
        "CREATE TABLE IF NOT EXISTS coupons (coupon_code TEXT PRIMARY KEY, budget REAL, max_claims INTEGER, claims_count INTEGER DEFAULT 0, status TEXT DEFAULT 'active')",
        "CREATE TABLE IF NOT EXISTS claimed_coupons (user_id INTEGER, coupon_code TEXT, PRIMARY KEY (user_id, coupon_code))",
        "CREATE TABLE IF NOT EXISTS coupon_forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')",
        "CREATE TABLE IF NOT EXISTS coupon_messages (coupon_code TEXT, chat_id INTEGER, message_id INTEGER, PRIMARY KEY (coupon_code, chat_id))"
    ]
    for q in queries: db_query(q, commit=True)

# --- 3. KEYBOARDS ---
def user_kb(uid):
    btns = [[KeyboardButton("💰 Balance"), KeyboardButton("👥 Referral")],
            [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📋 Tasks")],
            [KeyboardButton("💸 Withdraw"), KeyboardButton("🎟️ Coupon Code")]]
    if uid == ADMIN_ID: btns.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(btns, resize_keyboard=True)

def admin_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📧 Mailing"), KeyboardButton("📋 Task Management")],
        [KeyboardButton("🎟️ Coupon Management"), KeyboardButton("📊 Bot Stats")],
        [KeyboardButton("🏧 Withdrawals"), KeyboardButton("🔗 Main Track Management")],
        [KeyboardButton("⬅️ Back to User Menu")]
    ], resize_keyboard=True)

# --- 4. JOIN CHECK LOGIC ---
async def get_unjoined(uid, context, table='forced_channels'):
    channels = db_query(f"SELECT channel_name, channel_id, channel_url FROM {table} WHERE status='active'", fetchall=True)
    unjoined = []
    for name, cid, url in channels:
        try:
            m = await context.bot.get_chat_member(cid, uid)
            if m.status not in ['member', 'administrator', 'creator']: unjoined.append({'name': name, 'url': url})
        except: unjoined.append({'name': name, 'url': url})
    return unjoined

async def gatekeeper(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID: return True
    unjoined = await get_unjoined(uid, context)
    if unjoined:
        kb = [[InlineKeyboardButton(f"Join {c['name']}", url=c['url'])] for c in unjoined]
        kb.append([InlineKeyboardButton("✅ I have joined!", callback_data="verify_main")])
        target = update.message or update.callback_query.message
        await target.reply_text("⚠️ **Join Required**\nPlease join our channels:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return False
    return True

# --- 5. USER HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if context.args and not db_query("SELECT 1 FROM users WHERE user_id=?", (user.id,), fetchone=True):
        try:
            ref_id = int(context.args[0])
            if ref_id != user.id:
                db_query("INSERT OR IGNORE INTO users (user_id, username, balance, referred_by) VALUES (?,?,?,?)", (user.id, user.username, REFERRAL_BONUS, ref_id), commit=True)
                db_query("UPDATE users SET balance=balance+?, referral_count=referral_count+1 WHERE user_id=?", (REFERRAL_BONUS, ref_id), commit=True)
                try: await context.bot.send_message(ref_id, f"🎉 Referral joined! +${REFERRAL_BONUS}")
                except: pass
        except: pass
    db_query("INSERT OR IGNORE INTO users (user_id, username) VALUES (?,?)", (user.id, user.username), commit=True)
    await update.message.reply_text(f"👋 Welcome {user.first_name}!", reply_markup=user_kb(user.id))

async def handle_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gatekeeper(update, context): return
    res = db_query("SELECT balance FROM users WHERE user_id=?", (update.effective_user.id,), fetchone=True)
    bal = res[0] if res else 0.0
    await update.message.reply_text(f"💰 Balance: **${bal:.2f}**", parse_mode='Markdown')

async def handle_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gatekeeper(update, context): return
    uid, today = update.effective_user.id, date.today().isoformat()
    last = db_query("SELECT last_bonus_claim FROM users WHERE user_id=?", (uid,), fetchone=True)[0]
    if last == today: await update.message.reply_text("❌ Come back tomorrow!"); return
    db_query("UPDATE users SET balance=balance+?, last_bonus_claim=? WHERE user_id=?", (DAILY_BONUS, today, uid), commit=True)
    await update.message.reply_text(f"🎁 Claimed ${DAILY_BONUS} bonus!")

# --- 6. TASK LOGIC ---
async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gatekeeper(update, context): return
    uid = update.effective_user.id
    task = db_query("SELECT task_id, task_name, reward, task_url FROM tasks WHERE status='active' AND task_id NOT IN (SELECT task_id FROM completed_tasks WHERE user_id=?) LIMIT 1", (uid,), fetchone=True)
    if not task: await update.message.reply_text("✅ No more tasks!"); return
    kb = [[InlineKeyboardButton("Join Channel", url=task[3])], [InlineKeyboardButton("✅ I Have Joined", callback_data=f"v_task_{task[0]}")]]
    await update.message.reply_text(f"📋 **{task[1]}**\nReward: ${task[2]:.2f}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# --- 7. WITHDRAW CONV ---
async def w_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gatekeeper(update, context): return ConversationHandler.END
    bal = db_query("SELECT balance FROM users WHERE user_id=?", (update.effective_user.id,), fetchone=True)[0]
    if bal < MIN_WITHDRAWAL: await update.message.reply_text(f"❌ Min Withdraw ${MIN_WITHDRAWAL}"); return ConversationHandler.END
    kb = [[InlineKeyboardButton("BEP20", callback_data="W_BEP20"), InlineKeyboardButton("TRC20", callback_data="W_TRC20")]]
    await update.message.reply_text("Select Network:", reply_markup=InlineKeyboardMarkup(kb))
    return State.W_NET

async def w_net(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['net'] = query.data.split("_")[1]
    await query.edit_message_text(f"Send your {context.user_data['net']} address:")
    return State.W_ADDR

async def w_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['addr'] = update.message.text
    await update.message.reply_text("Amount:")
    return State.W_AMT

async def w_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt, uid = float(update.message.text), update.effective_user.id
        bal = db_query("SELECT balance FROM users WHERE user_id=?", (uid,), fetchone=True)[0]
        if amt < 1 or amt > bal: raise ValueError
        db_query("UPDATE users SET balance=balance-? WHERE user_id=?", (amt, uid), commit=True)
        db_query("INSERT INTO withdrawals (user_id, amount, network, wallet_address) VALUES (?,?,?,?)", (uid, amt, context.user_data['net'], context.user_data['addr']), commit=True)
        await update.message.reply_text("✅ Requested!")
        await context.bot.send_message(ADMIN_ID, f"🏧 New Withdrawal: ${amt:.2f}")
        return ConversationHandler.END
    except: await update.message.reply_text("Invalid amount."); return State.W_AMT

# --- 8. COUPON CLAIM ---
async def cpn_claim_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    unjoined = await get_unjoined(update.effective_user.id, context, 'coupon_forced_channels')
    if unjoined:
        kb = [[InlineKeyboardButton(f"Join {c['name']}", url=c['url'])] for c in unjoined]
        await update.message.reply_text("⚠️ Join these to claim:", reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END
    await update.message.reply_text("Enter Coupon Code:")
    return State.CPN_CODE

async def cpn_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code, uid = update.message.text.strip().upper(), update.effective_user.id
    cp = db_query("SELECT budget, max_claims, claims_count, status FROM coupons WHERE coupon_code=?", (code,), fetchone=True)
    if not cp or cp[3] != 'active': await update.message.reply_text("❌ Expired"); return ConversationHandler.END
    if db_query("SELECT 1 FROM claimed_coupons WHERE user_id=? AND coupon_code=?", (uid, code), fetchone=True): await update.message.reply_text("❌ Already claimed"); return ConversationHandler.END
    reward = cp[0] / cp[1]
    db_query("UPDATE users SET balance=balance+? WHERE user_id=?", (reward, uid), commit=True)
    db_query("INSERT INTO claimed_coupons (user_id, coupon_code) VALUES (?,?)", (uid, code), commit=True)
    db_query("UPDATE coupons SET claims_count=claims_count+1 WHERE coupon_code=?", (code,), commit=True)
    await update.message.reply_text(f"🎁 Success! +${reward:.2f}")
    return ConversationHandler.END

# --- 9. CALLBACK HANDLER ---
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; data = query.data
    if data == "verify_main":
        if await gatekeeper(update, context): await query.edit_message_text("✅ Verified!")
        else: await query.answer("❌ Not joined yet!", show_alert=True)
    elif data.startswith("v_task_"):
        tid = int(data.split("_")[2])
        target_cid = db_query("SELECT target_chat_id, reward FROM tasks WHERE task_id=?", (tid,), fetchone=True)
        try:
            m = await context.bot.get_chat_member(target_cid[0], query.from_user.id)
            if m.status in ['member', 'administrator', 'creator']:
                db_query("INSERT OR IGNORE INTO completed_tasks (user_id, task_id) VALUES (?,?)", (query.from_user.id, tid), commit=True)
                db_query("UPDATE users SET balance=balance+? WHERE user_id=?", (target_cid[1], query.from_user.id), commit=True)
                await query.edit_message_text(f"✅ Verified! +${target_cid[1]:.2f}")
            else: await query.answer("❌ Not joined!", show_alert=True)
        except: await query.answer("Bot is not admin in target chat!", show_alert=True)

# --- 10. HEALTH SERVER & MAIN ---
class Health(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
def run_h(): 
    try:
        httpd = HTTPServer(('0.0.0.0', PORT), Health)
        httpd.serve_forever()
    except: pass

def main():
    if not BOT_TOKEN: return
    setup_database()
    threading.Thread(target=run_h, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversations
    app.add_handler(ConversationHandler(entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), w_start)], states={State.W_NET: [CallbackQueryHandler(w_net)], State.W_ADDR: [MessageHandler(filters.TEXT, w_addr)], State.W_AMT: [MessageHandler(filters.TEXT, w_final)]}, fallbacks=[]))
    app.add_handler(ConversationHandler(entry_points=[MessageHandler(filters.Regex("^🎟️ Coupon Code$"), cpn_claim_start)], states={State.CPN_CODE: [MessageHandler(filters.TEXT, cpn_verify)]}, fallbacks=[]))

    # Basic Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), handle_balance))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), handle_daily))
    app.add_handler(MessageHandler(filters.Regex("^📋 Tasks$"), show_tasks))
    app.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), handle_referral))
    app.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), lambda u,c: u.message.reply_text("Admin", reply_markup=admin_kb()) if u.effective_user.id == ADMIN_ID else None))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), lambda u,c: u.message.reply_text("User Mode", reply_markup=user_kb(u.effective_user.id))))
    app.add_handler(CallbackQueryHandler(button_click))

    logger.info("Bot started...")
    app.run_polling()

async def handle_referral(update, context):
    bot_info = await context.bot.get_me()
    await update.message.reply_text(f"👥 Link: https://t.me/{bot_info.username}?start={update.effective_user.id}")

if __name__ == "__main__": main()

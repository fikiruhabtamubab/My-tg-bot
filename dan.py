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
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "0")
PORT = int(os.getenv("PORT", 8080))

try:
    ADMIN_ID = int(ADMIN_ID_RAW.strip())
except:
    ADMIN_ID = 0

DB_FILE = "user_data.db"
REFERRAL_BONUS = 0.05
DAILY_BONUS = 0.05
MIN_WITHDRAWAL = 5.00

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

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
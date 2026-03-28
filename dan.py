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
# Use environment variables for security. 
# On Choreo, add these in the "Config & Secrets" section.
TOKEN = os.getenv("BOT_TOKEN", "8355685878:AAFHxGMTs8aAA71XQmk4oztuIn-6YaOVJFE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "5815604554"))
PORT = int(os.getenv("PORT", 8080))  # Required for Choreo health checks

DB_FILE = "user_data.db"
REFERRAL_BONUS = 0.05
DAILY_BONUS = 0.05
MIN_WITHDRAWAL = 5.00

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

class State(Enum):
    # Task States
    TASK_NAME = 1; TASK_CHAT = 2; TASK_URL = 3; TASK_REWARD = 4
    # Withdraw States
    W_NETWORK = 5; W_ADDRESS = 6; W_AMOUNT = 7
    # Mail States
    MAIL_MSG = 8; MAIL_BUTTONS = 9; MAIL_BTN_DATA = 10
    # Track States
    TRK_NAME = 11; TRK_ID = 12; TRK_URL = 13
    # Coupon States
    CPN_BUDGET = 14; CPN_CLAIMS = 15; CPN_CODE = 16
    CPN_TRK_NAME = 17; CPN_TRK_ID = 18; CPN_TRK_URL = 19

# --- 2. DATABASE LOGIC ---
def db_query(query, params=(), commit=False, fetchone=False, fetchall=False):
    """Helper to handle DB connections safely."""
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
        "CREATE TABLE IF NOT EXISTS withdrawals (withdrawal_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, network TEXT, wallet_address TEXT, status TEXT DEFAULT 'pending', request_date DATETIME DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')",
        "CREATE TABLE IF NOT EXISTS coupons (coupon_code TEXT PRIMARY KEY, budget REAL, max_claims INTEGER, claims_count INTEGER DEFAULT 0, status TEXT DEFAULT 'active', creation_date DATETIME DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS claimed_coupons (user_id INTEGER, coupon_code TEXT, PRIMARY KEY (user_id, coupon_code))",
        "CREATE TABLE IF NOT EXISTS coupon_forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')",
        "CREATE TABLE IF NOT EXISTS coupon_messages (coupon_code TEXT, chat_id INTEGER, message_id INTEGER, PRIMARY KEY (coupon_code, chat_id))"
    ]
    for q in queries: db_query(q, commit=True)

# --- 3. KEYBOARDS ---
def user_kb(user_id):
    btns = [
        [KeyboardButton("💰 Balance"), KeyboardButton("👥 Referral")],
        [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📋 Tasks")],
        [KeyboardButton("💸 Withdraw"), KeyboardButton("🎟️ Coupon Code")]
    ]
    if user_id == ADMIN_ID: btns.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(btns, resize_keyboard=True)

def admin_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📧 Mailing"), KeyboardButton("📋 Task Management")],
        [KeyboardButton("🎟️ Coupon Management"), KeyboardButton("📊 Bot Stats")],
        [KeyboardButton("🏧 Withdrawals"), KeyboardButton("🔗 Main Track Management")],
        [KeyboardButton("⬅️ Back to User Menu")]
    ], resize_keyboard=True)

# --- 4. MEMBERSHIP CHECKERS ---
async def get_unjoined(user_id, context, table):
    channels = db_query(f"SELECT channel_name, channel_id, channel_url FROM {table} WHERE status = 'active'", fetchall=True)
    unjoined = []
    for name, cid, url in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=cid, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                unjoined.append({'name': name, 'url': url})
        except Exception: unjoined.append({'name': name, 'url': url})
    return unjoined

async def force_join_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id == ADMIN_ID: return True
    
    unjoined = await get_unjoined(user.id, context, 'forced_channels')
    if unjoined:
        kb = [[InlineKeyboardButton(f"Join {c['name']}", url=c['url'])] for c in unjoined]
        kb.append([InlineKeyboardButton("✅ Checked, let's go!", callback_data="check_join")])
        msg = "⚠️ **Access Denied**\nPlease join our channels to unlock the bot features:"
        target = update.message or update.callback_query.message
        await target.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return False
    return True

# --- 5. USER HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Handle Referral
    if context.args and not db_query("SELECT 1 FROM users WHERE user_id=?", (user.id,), fetchone=True):
        try:
            ref_id = int(context.args[0])
            if ref_id != user.id:
                db_query("INSERT OR IGNORE INTO users (user_id, username, balance, referred_by) VALUES (?, ?, ?, ?)", 
                         (user.id, user.username, REFERRAL_BONUS, ref_id), commit=True)
                db_query("UPDATE users SET balance = balance + ?, referral_count = referral_count + 1 WHERE user_id = ?", 
                         (REFERRAL_BONUS, ref_id), commit=True)
                try: await context.bot.send_message(ref_id, f"🎁 New referral! You earned ${REFERRAL_BONUS}")
                except: pass
        except: pass

    db_query("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username), commit=True)
    await update.message.reply_text(f"👋 Welcome {user.first_name}!", reply_markup=user_kb(user.id))

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_gate(update, context): return
    row = db_query("SELECT balance FROM users WHERE user_id=?", (update.effective_user.id,), fetchone=True)
    await update.message.reply_text(f"💰 Balance: **${row[0]:.2f}**", parse_mode='Markdown')

async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_gate(update, context): return
    uid = update.effective_user.id
    row = db_query("SELECT referral_count FROM users WHERE user_id=?", (uid,), fetchone=True)
    link = f"https://t.me/{(await context.bot.get_me()).username}?start={uid}"
    await update.message.reply_text(f"👥 Referrals: {row[0]}\nLink: `{link}`", parse_mode='Markdown')

# --- 6. WITHDRAWAL CONVERSATION ---
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_gate(update, context): return State.END
    bal = db_query("SELECT balance FROM users WHERE user_id=?", (update.effective_user.id,), fetchone=True)[0]
    if bal < MIN_WITHDRAWAL:
        await update.message.reply_text(f"❌ Min withdraw is ${MIN_WITHDRAWAL:.2f}")
        return ConversationHandler.END
    kb = [[InlineKeyboardButton("Binance (BEP20)", callback_data="W_BEP20"), 
           InlineKeyboardButton("Binance (TRC20)", callback_data="W_TRC20")]]
    await update.message.reply_text("Choose network:", reply_markup=InlineKeyboardMarkup(kb))
    return State.W_NETWORK

async def withdraw_net(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['net'] = query.data.replace("W_", "")
    await query.edit_message_text(f"Send your {context.user_data['net']} address:")
    return State.W_ADDRESS

async def withdraw_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['addr'] = update.message.text
    await update.message.reply_text("Amount to withdraw:")
    return State.W_AMOUNT

async def withdraw_final(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text)
        uid = update.effective_user.id
        bal = db_query("SELECT balance FROM users WHERE user_id=?", (uid,), fetchone=True)[0]
        if amt < 1 or amt > bal:
            await update.message.reply_text("Invalid amount."); return State.W_AMOUNT
        
        db_query("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amt, uid), commit=True)
        db_query("INSERT INTO withdrawals (user_id, amount, network, wallet_address) VALUES (?, ?, ?, ?)",
                 (uid, amt, context.user_data['net'], context.user_data['addr']), commit=True)
        
        await update.message.reply_text("✅ Request sent to admin.")
        await context.bot.send_message(ADMIN_ID, "🏧 New Withdrawal Request!")
        return ConversationHandler.END
    except:
        await update.message.reply_text("Enter a valid number.")
        return State.W_AMOUNT

# --- 7. CHOREO HEALTH CHECK ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Bot is alive")

def run_health_server():
    httpd = HTTPServer(('0.0.0.0', PORT), HealthHandler)
    httpd.serve_forever()

# --- 8. MAIN ---
def main():
    setup_database()
    
    # Start health check thread for Choreo
    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()

    # Conversation Handlers
    w_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_start)],
        states={
            State.W_NETWORK: [CallbackQueryHandler(withdraw_net, pattern="^W_")],
            State.W_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_addr)],
            State.W_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_final)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)]
    )

    # Register Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), balance))
    app.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), referral))
    app.add_handler(w_conv)
    
    # Admin Toggle
    app.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), 
        lambda u, c: u.message.reply_text("Admin Mode", reply_markup=admin_kb()) if u.effective_user.id == ADMIN_ID else None))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), 
        lambda u, c: u.message.reply_text("User Mode", reply_markup=user_kb(u.effective_user.id))))

    logger.info("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
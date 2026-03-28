import logging
import sqlite3
import io
import os
import random
import asyncio
from datetime import datetime, date
from enum import Enum

from telegram import (
    ReplyKeyboardMarkup, Update, KeyboardButton, 
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters
)

# --- Configuration ---
BOT_API_KEY = os.environ.get("BOT_API_KEY") 
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

REFERRAL_BONUS = 0.05
DAILY_BONUS = 0.05
MIN_WITHDRAWAL_LIMIT = 5.00

DATA_DIR = os.environ.get('DATA_DIR', '.')
DB_FILE = os.path.join(DATA_DIR, "user_data.db")

# --- Setup Logging & States ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

class State(Enum):
    GET_TASK_NAME = 1
    GET_TARGET_CHAT_ID = 2
    GET_TASK_URL = 3
    GET_TASK_REWARD = 4
    CHOOSE_WITHDRAW_NETWORK = 5
    GET_WALLET_ADDRESS = 6
    GET_WITHDRAW_AMOUNT = 7
    GET_MAIL_MESSAGE = 8
    AWAIT_BUTTON_OR_SEND = 9
    AWAIT_COUPON_CODE = 10

# --- Database Initialization ---
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
        conn.commit()

# --- Keyboards ---
def get_user_keyboard(user_id):
    buttons = [
        [KeyboardButton("💰 Balance"), KeyboardButton("👥 Referral")],
        [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📋 Tasks")],
        [KeyboardButton("💸 Withdraw"), KeyboardButton("🎟️ Coupon Code")]
    ]
    if user_id == ADMIN_ID:
        buttons.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_admin_keyboard():
    buttons = [
        [KeyboardButton("📧 Mailing"), KeyboardButton("📋 Task Management")],
        [KeyboardButton("🎟️ Coupon Management"), KeyboardButton("📊 Bot Stats")],
        [KeyboardButton("🏧 Withdrawals"), KeyboardButton("🔗 Main Track Management")],
        [KeyboardButton("⬅️ Back to User Menu")]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# --- Helper Logic ---
async def is_member(user_id, context):
    if user_id == ADMIN_ID: return True
    with sqlite3.connect(DB_FILE) as conn:
        channels = conn.cursor().execute("SELECT channel_id FROM forced_channels WHERE status = 'active'").fetchall()
    for (cid,) in channels:
        try:
            member = await context.bot.get_chat_member(cid, user_id)
            if member.status not in ['member', 'administrator', 'creator']: return False
        except: continue
    return True

# --- User Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username))
        if context.args:
            try:
                ref_id = int(context.args[0])
                if ref_id != user.id:
                    c.execute("UPDATE users SET balance = balance + ?, referral_count = referral_count + 1 WHERE user_id = ?", (REFERRAL_BONUS, ref_id))
            except: pass
        conn.commit()
    await update.message.reply_text(f"👋 Welcome {user.first_name}!", reply_markup=get_user_keyboard(user.id))

async def handle_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_FILE) as conn:
        bal = conn.cursor().execute("SELECT balance FROM users WHERE user_id = ?", (update.effective_user.id,)).fetchone()[0]
    await update.message.reply_text(f"💰 Balance: **${bal:.2f}**", parse_mode='Markdown')

async def handle_daily_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    today = date.today().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        last = c.execute("SELECT last_bonus_claim FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
        if last == today:
            await update.message.reply_text("❌ Already claimed today!")
            return
        c.execute("UPDATE users SET balance = balance + ?, last_bonus_claim = ? WHERE user_id = ?", (DAILY_BONUS, today, uid))
        conn.commit()
    await update.message.reply_text(f"🎁 Bonus added: +${DAILY_BONUS}")

# --- Admin Stats Handler (The Missing Function) ---
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        bal = c.execute("SELECT SUM(balance) FROM users").fetchone()[0] or 0
        tasks = c.execute("SELECT COUNT(*) FROM tasks WHERE status='active'").fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'").fetchone()[0]
    
    msg = (f"📊 **Bot Stats**\n\n"
           f"👥 Users: {users}\n"
           f"💰 Total Bal: ${bal:.2f}\n"
           f"📋 Tasks: {tasks}\n"
           f"🏧 Pending: {pending}")
    await update.message.reply_text(msg, parse_mode='Markdown')

# --- Withdrawal Flow ---
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_FILE) as conn:
        bal = conn.cursor().execute("SELECT balance FROM users WHERE user_id = ?", (update.effective_user.id,)).fetchone()[0]
    if bal < MIN_WITHDRAWAL_LIMIT:
        await update.message.reply_text(f"❌ Min payout is ${MIN_WITHDRAWAL_LIMIT}")
        return ConversationHandler.END
    
    kb = [[InlineKeyboardButton("BEP20", callback_data="w_net_BEP20"), InlineKeyboardButton("TRC20", callback_data="w_net_TRC20")]]
    await update.message.reply_text("Select network:", reply_markup=InlineKeyboardMarkup(kb))
    return State.CHOOSE_WITHDRAW_NETWORK

async def save_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text)
        uid = update.effective_user.id
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            bal = c.execute("SELECT balance FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
            if amt > bal or amt < 1:
                await update.message.reply_text("Invalid amount.")
                return State.GET_WITHDRAW_AMOUNT
            c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amt, uid))
            c.execute("INSERT INTO withdrawals (user_id, amount, network, wallet_address) VALUES (?,?,?,?)", (uid, amt, context.user_data['net'], context.user_data['adr']))
            conn.commit()
        await update.message.reply_text("✅ Request Submitted!")
        return ConversationHandler.END
    except:
        await update.message.reply_text("Send a number.")
        return State.GET_WITHDRAW_AMOUNT

# --- Mailing Flow ---
async def admin_mailing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    await update.message.reply_text("Send message to broadcast:")
    return State.GET_MAIL_MESSAGE

async def broadcast_mailing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    with sqlite3.connect(DB_FILE) as conn:
        users = conn.cursor().execute("SELECT user_id FROM users").fetchall()
    count = 0
    for (uid,) in users:
        try:
            await msg.copy(chat_id=uid)
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    await update.message.reply_text(f"📢 Sent to {count} users.")
    return ConversationHandler.END

# --- Main App ---
def main():
    setup_database()
    app = Application.builder().token(BOT_API_KEY).build()

    # Converstations
    withdraw_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_start)],
        states={
            State.CHOOSE_WITHDRAW_NETWORK: [CallbackQueryHandler(lambda u,c: (c.user_data.update({'net': u.callback_query.data}), u.callback_query.message.reply_text("Address:"), State.GET_WALLET_ADDRESS)[2])],
            State.GET_WALLET_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: (c.user_data.update({'adr': u.message.text}), u.message.reply_text("Amount:"), State.GET_WITHDRAW_AMOUNT)[2])],
            State.GET_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_withdraw_amount)]
        },
        fallbacks=[CommandHandler("start", start)]
    )

    mail_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📧 Mailing$"), admin_mailing_start)],
        states={State.GET_MAIL_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_mailing)]},
        fallbacks=[CommandHandler("start", start)]
    )

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), handle_balance))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), handle_daily_bonus))
    app.add_handler(MessageHandler(filters.Regex("^📊 Bot Stats$"), admin_stats))
    app.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), lambda u,c: u.message.reply_text("Admin Panel", reply_markup=get_admin_keyboard()) if u.effective_user.id == ADMIN_ID else None))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), start))
    
    app.add_handler(withdraw_conv)
    app.add_handler(mail_conv)

    print("Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()

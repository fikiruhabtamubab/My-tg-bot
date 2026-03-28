import logging
import sqlite3
import io
import os
import random
from datetime import datetime, date
from enum import Enum

from telegram import (
    ReplyKeyboardMarkup, Update, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.error import BadRequest, Forbidden

# --- Configuration (Choreo Ready) ---
BOT_API_KEY = os.environ.get("BOT_API_KEY") 
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

REFERRAL_BONUS = 0.05
DAILY_BONUS = 0.05
MIN_WITHDRAWAL_LIMIT = 5.00

# Persistence Setup
DATA_DIR = os.environ.get('DATA_DIR', '.')
DB_FILE = os.path.join(DATA_DIR, "user_data.db")

# --- Setup Logging & States ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

class State(Enum):
    GET_TASK_NAME = 1; GET_TARGET_CHAT_ID = 2; GET_TASK_URL = 3; GET_TASK_REWARD = 4
    CHOOSE_WITHDRAW_NETWORK = 5; GET_WALLET_ADDRESS = 6; GET_WITHDRAW_AMOUNT = 7
    GET_MAIL_MESSAGE = 8; AWAIT_BUTTON_OR_SEND = 9; GET_BUTTON_DATA = 10
    GET_TRACKED_NAME = 11; GET_TRACKED_ID = 12; GET_TRACKED_URL = 13
    GET_COUPON_BUDGET = 14; GET_COUPON_MAX_CLAIMS = 15; AWAIT_COUPON_CODE = 16
    GET_PROOF_CHANNEL = 17 # New State

# --- Database Setup ---
def setup_database():
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0, last_bonus_claim DATE, referred_by INTEGER, referral_count INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS tasks (task_id INTEGER PRIMARY KEY AUTOINCREMENT, task_name TEXT NOT NULL, reward REAL NOT NULL, target_chat_id TEXT NOT NULL, task_url TEXT NOT NULL, status TEXT DEFAULT 'active')")
        c.execute("CREATE TABLE IF NOT EXISTS completed_tasks (user_id INTEGER, task_id INTEGER, PRIMARY KEY (user_id, task_id))")
        c.execute("CREATE TABLE IF NOT EXISTS withdrawals (withdrawal_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL, network TEXT NOT NULL, wallet_address TEXT NOT NULL, status TEXT DEFAULT 'pending', request_date DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')")
        c.execute("CREATE TABLE IF NOT EXISTS coupons (coupon_code TEXT PRIMARY KEY, budget REAL NOT NULL, max_claims INTEGER NOT NULL, claims_count INTEGER DEFAULT 0, status TEXT DEFAULT 'active', creation_date DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS claimed_coupons (user_id INTEGER, coupon_code TEXT, PRIMARY KEY (user_id, coupon_code))")
        c.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT)")
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
        [KeyboardButton("📢 Proof Channel"), KeyboardButton("⬅️ Back to User Menu")],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# --- Verification Helper ---
async def is_member_or_send_join_message(update, context):
    user_id = update.effective_user.id
    if user_id == ADMIN_ID: return True
    with sqlite3.connect(DB_FILE) as conn:
        chans = conn.cursor().execute("SELECT channel_name, channel_id, channel_url FROM forced_channels WHERE status='active'").fetchall()
    unjoined = []
    for name, cid, url in chans:
        try:
            m = await context.bot.get_chat_member(cid, user_id)
            if m.status not in ['member', 'administrator', 'creator']: unjoined.append({'name': name, 'url': url})
        except: continue
    if unjoined:
        kb = [[InlineKeyboardButton(f"➡️ Join {c['name']}", url=c['url'])] for c in unjoined]
        kb.append([InlineKeyboardButton("✅ Checked, Continue", callback_data="verify_membership")])
        target = update.message or update.callback_query.message
        await target.reply_text("⚠️ **Join our channels to continue:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return False
    return True

# --- User Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username))
        if context.args and not c.execute("SELECT referred_by FROM users WHERE user_id=?", (user.id,)).fetchone()[0]:
            try:
                ref_id = int(context.args[0])
                if ref_id != user.id:
                    c.execute("UPDATE users SET balance=balance+?, referred_by=? WHERE user_id=?", (REFERRAL_BONUS, ref_id, user.id))
                    c.execute("UPDATE users SET balance=balance+?, referral_count=referral_count+1 WHERE user_id=?", (REFERRAL_BONUS, ref_id))
                    conn.commit()
                    try: await context.bot.send_message(ref_id, f"✅ **New Referral!** +${REFERRAL_BONUS}")
                    except: pass
            except: pass
        conn.commit()
    if await is_member_or_send_join_message(update, context):
        await update.message.reply_text(f"👋 Welcome {user.first_name}!", reply_markup=get_user_keyboard(user.id))

async def handle_balance(update, context):
    with sqlite3.connect(DB_FILE) as conn:
        bal = conn.cursor().execute("SELECT balance FROM users WHERE user_id=?", (update.effective_user.id,)).fetchone()[0]
    await update.message.reply_text(f"💰 Balance: **${bal:.2f}**", parse_mode='Markdown')

async def tasks_entry(update, context):
    if not await is_member_or_send_join_message(update, context): return
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        task = conn.cursor().execute("SELECT task_id, task_name, reward, task_url FROM tasks WHERE status='active' AND task_id NOT IN (SELECT task_id FROM completed_tasks WHERE user_id=?) ORDER BY RANDOM() LIMIT 1", (uid,)).fetchone()
    if not task: await update.message.reply_text("🎉 All tasks completed!"); return
    tid, name, reward, url = task
    kb = [[InlineKeyboardButton("➡️ Open Link", url=url)], [InlineKeyboardButton("✅ Verify", callback_data=f"task_v_{tid}"), InlineKeyboardButton("⏭️ Skip", callback_data=f"task_s_{tid}")]]
    await update.message.reply_text(f"📋 **Task**: {name}\n💰 **Reward**: ${reward:.2f}", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# --- Admin Panel Logic ---
async def admin_panel(update, context):
    if update.effective_user.id == ADMIN_ID: await update.message.reply_text("👑 Admin Panel", reply_markup=get_admin_keyboard())

async def set_proof_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("Send the Proof Channel ID (e.g. `@MyProofChannel` or `-100...`):")
    return State.GET_PROOF_CHANNEL

async def save_proof_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.message.text.strip()
    with sqlite3.connect(DB_FILE) as conn:
        conn.cursor().execute("INSERT OR REPLACE INTO settings (name, value) VALUES ('proof_channel', ?)", (cid,))
        conn.commit()
    await update.message.reply_text(f"✅ Proof Channel set to: `{cid}`", reply_markup=get_admin_keyboard())
    return ConversationHandler.END

# --- Withdrawal Flow ---
async def withdraw_start(update, context):
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        bal = conn.cursor().execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()[0]
    if bal < MIN_WITHDRAWAL_LIMIT: await update.message.reply_text(f"❌ Min payout: ${MIN_WITHDRAWAL_LIMIT}"); return ConversationHandler.END
    kb = [[InlineKeyboardButton("BEP20", callback_data="w_net_BEP20"), InlineKeyboardButton("TRC20", callback_data="w_net_TRC20")]]
    await update.message.reply_text("Select Network:", reply_markup=InlineKeyboardMarkup(kb))
    return State.CHOOSE_WITHDRAW_NETWORK

async def save_withdraw_final(update, context):
    try:
        amt = float(update.message.text); uid = update.effective_user.id
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor(); bal = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()[0]
            if amt < 1.0 or amt > bal: await update.message.reply_text("Invalid amount."); return State.GET_WITHDRAW_AMOUNT
            c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amt, uid))
            c.execute("INSERT INTO withdrawals (user_id, amount, network, wallet_address) VALUES (?,?,?,?)", (uid, amt, context.user_data['net'], update.message.text))
            wid = c.lastrowid; conn.commit()
        await update.message.reply_text("✅ Request Sent!", reply_markup=get_user_keyboard(uid))
        await context.bot.send_message(ADMIN_ID, f"🏧 **New Request**\nID: `{wid}`\nAmt: `${amt}`")
        return ConversationHandler.END
    except: await update.message.reply_text("Enter a number:"); return

# --- Callback Router (The Brain) ---
async def global_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; data = query.data; uid = query.from_user.id
    
    if data.startswith("task_"):
        await query.answer(); act, tid = data.split("_")[1], int(data.split("_")[2])
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor(); task = c.execute("SELECT reward, target_chat_id FROM tasks WHERE task_id=?", (tid,)).fetchone()
            if act == 's': c.execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?,?)", (uid, tid)); conn.commit(); await query.message.delete()
            else:
                try:
                    m = await context.bot.get_chat_member(task[1], uid)
                    if m.status in ['member', 'administrator', 'creator']:
                        c.execute("UPDATE users SET balance=balance+?, user_id=?", (task[0], uid))
                        c.execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?,?)", (uid, tid))
                        conn.commit(); await query.answer(f"✅ Success! +${task[0]}", show_alert=True); await query.message.delete()
                except: await query.answer("Verification failed.")

    elif data.startswith("p_"): # Approval & Proof Logic
        act, wid = data.split("_")[1], int(data.split("_")[2])
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor(); req = c.execute("SELECT user_id, amount, network, wallet_address FROM withdrawals WHERE withdrawal_id=?", (wid,)).fetchone()
            if act == 'app':
                c.execute("UPDATE withdrawals SET status='approved' WHERE withdrawal_id=?", (wid,))
                await context.bot.send_message(req[0], f"✅ Payout of ${req[1]} Approved!")
                
                # POST PROOF
                proof_cid = c.execute("SELECT value FROM settings WHERE name='proof_channel'").fetchone()
                if proof_cid:
                    u_info = await context.bot.get_chat(req[0])
                    name = u_info.first_name if u_info.first_name else "User"
                    now = datetime.now().strftime("%-m/%-d/%Y %-I:%M:%S %p")
                    proof_text = (
                        f"🔎 *Withdrawal Details*\n"
                        f"🆔 ID: {wid}\n"
                        f"👤 User: {name} ({req[0]})\n"
                        f"💰 Amount: {req[1]}\n"
                        f"⛓ Network: {req[2]}\n"
                        f"📍 Address: {req[3]}\n"
                        f"⏰ Time: {now}\n"
                        f"✅ Status : Paid"
                    )
                    try: await context.bot.send_message(proof_cid[0], proof_text, parse_mode='Markdown')
                    except: pass
            else:
                c.execute("UPDATE withdrawals SET status='rejected' WHERE withdrawal_id=?", (wid,))
                c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (req[1], req[0]))
                await context.bot.send_message(req[0], f"❌ Payout Rejected. Funds returned.")
            conn.commit(); await query.message.delete()

    elif data == "verify_membership":
        if await is_member_or_send_join_message(update, context): await query.message.delete(); await query.message.reply_text("✅ Access Granted!", reply_markup=get_user_keyboard(uid))

# --- Main Setup ---
def main():
    setup_database(); app = Application.builder().token(BOT_API_KEY).build()

    # Conversations
    proof_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📢 Proof Channel$"), set_proof_channel_start)],
        states={State.GET_PROOF_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_proof_channel)]},
        fallbacks=[]
    )
    withdraw_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_start)],
        states={State.CHOOSE_WITHDRAW_NETWORK: [CallbackQueryHandler(lambda u,c: (c.user_data.update({'net':u.callback_query.data.split("_")[2]}), u.callback_query.edit_message_text("Wallet:"), State.GET_WALLET_ADDRESS)[2], pattern="^w_net_")],
                State.GET_WALLET_ADDRESS: [MessageHandler(filters.TEXT, lambda u,c: (c.user_data.update({'adr':u.message.text}), u.message.reply_text("Amount:"), State.GET_WITHDRAW_AMOUNT)[2])],
                State.GET_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT, save_withdraw_final)]},
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), handle_balance))
    app.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), handle_referral))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), handle_daily_bonus))
    app.add_handler(MessageHandler(filters.Regex("^📋 Tasks$"), tasks_entry))
    app.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), admin_panel))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), start))
    
    app.add_handler(proof_conv); app.add_handler(withdraw_conv)
    app.add_handler(CallbackQueryHandler(global_callback))

    print("Bot started..."); app.run_polling()

if __name__ == "__main__":
    main()

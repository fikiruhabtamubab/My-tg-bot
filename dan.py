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
ADMIN_ID = int(os.environ.get("ADMIN_ID"))

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
    # Admin Tasks
    GET_TASK_NAME = 1; GET_TARGET_CHAT_ID = 2; GET_TASK_URL = 3; GET_TASK_REWARD = 4
    # Withdrawals
    CHOOSE_WITHDRAW_NETWORK = 5; GET_WALLET_ADDRESS = 6; GET_WITHDRAW_AMOUNT = 7
    # Mailing
    GET_MAIL_MESSAGE = 8; AWAIT_BUTTON_OR_SEND = 9; GET_BUTTON_DATA = 10
    # Tracking
    GET_TRACKED_NAME = 11; GET_TRACKED_ID = 12; GET_TRACKED_URL = 13
    # Coupons
    GET_COUPON_BUDGET = 14; GET_COUPON_MAX_CLAIMS = 15; AWAIT_COUPON_CODE = 16
    GET_COUPON_TRACKED_NAME = 17; GET_COUPON_TRACKED_ID = 18; GET_COUPON_TRACKED_URL = 19

# --- Database Initialization ---
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
        c.execute("CREATE TABLE IF NOT EXISTS coupon_forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')")
        c.execute("CREATE TABLE IF NOT EXISTS coupon_messages (coupon_code TEXT, chat_id INTEGER, message_id INTEGER, PRIMARY KEY (coupon_code, chat_id))")
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

# --- Membership & Helper Functions ---
async def get_unjoined_channels(user_id, context, table_name):
    with sqlite3.connect(DB_FILE) as conn:
        channels = conn.cursor().execute(f"SELECT channel_name, channel_id, channel_url FROM {table_name} WHERE status = 'active'").fetchall()
    unjoined = []
    for name, cid, url in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=cid, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                unjoined.append({'name': name, 'url': url})
        except: continue
    return unjoined

async def is_member_or_send_join_message(update, context, table='forced_channels'):
    user_id = update.effective_user.id
    if user_id == ADMIN_ID: return True
    unjoined = await get_unjoined_channels(user_id, context, table)
    if unjoined:
        text = "⚠️ **Action Required**\n\nYou must join our channels to continue:"
        kb = [[InlineKeyboardButton(f"➡️ Join {c['name']}", url=c['url'])] for c in unjoined]
        kb.append([InlineKeyboardButton("✅ Checked, Continue", callback_data="verify_membership")])
        target = update.message or update.callback_query.message
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return False
    return True

# --- User Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username))
        
        # Referral Logic
        if context.args and not c.execute("SELECT referred_by FROM users WHERE user_id = ?", (user.id,)).fetchone()[0]:
            try:
                ref_id = int(context.args[0])
                if ref_id != user.id:
                    c.execute("UPDATE users SET balance = balance + ?, referred_by = ? WHERE user_id = ?", (REFERRAL_BONUS, ref_id, user.id))
                    c.execute("UPDATE users SET balance = balance + ?, referral_count = referral_count + 1 WHERE user_id = ?", (REFERRAL_BONUS, ref_id))
                    conn.commit()
                    try: await context.bot.send_message(ref_id, f"✅ **New Referral!**\nYou earned ${REFERRAL_BONUS:.2f}", parse_mode='Markdown')
                    except: pass
            except: pass
        conn.commit()

    if await is_member_or_send_join_message(update, context):
        await update.message.reply_text(f"👋 Welcome {user.first_name}!", reply_markup=get_user_keyboard(user.id))

async def handle_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_FILE) as conn:
        bal = conn.cursor().execute("SELECT balance FROM users WHERE user_id = ?", (update.effective_user.id,)).fetchone()[0]
    await update.message.reply_text(f"💰 Your balance: **${bal:.2f}**", parse_mode='Markdown')

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        count = conn.cursor().execute("SELECT referral_count FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
    bot = await context.bot.get_me()
    link = f"https://t.me/{bot.username}?start={uid}"
    await update.message.reply_text(f"👥 **Referral Link**\n\nEarn ${REFERRAL_BONUS} per invite.\nInvites: {count}\n\nLink: `{link}`", parse_mode='Markdown')

async def handle_daily_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    today = date.today().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        last = c.execute("SELECT last_bonus_claim FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
        if last == today: await update.message.reply_text("❌ Claimed already!"); return
        c.execute("UPDATE users SET balance = balance + ?, last_bonus_claim = ? WHERE user_id = ?", (DAILY_BONUS, today, uid))
        conn.commit()
    await update.message.reply_text(f"🎁 Daily Bonus: **+${DAILY_BONUS}**!")

# --- Task Flow (Direct) ---
async def tasks_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_member_or_send_join_message(update, context):
        await show_next_task(update, context)

async def show_next_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        task = conn.cursor().execute("""
            SELECT task_id, task_name, reward, task_url FROM tasks 
            WHERE status = 'active' AND task_id NOT IN (SELECT task_id FROM completed_tasks WHERE user_id = ?)
            ORDER BY RANDOM() LIMIT 1
        """, (uid,)).fetchone()
    
    if not task:
        msg = "🎉 All tasks completed! Check back later."
        if update.callback_query: await update.callback_query.edit_message_text(msg)
        else: await update.message.reply_text(msg)
        return

    tid, name, reward, url = task
    text = f"📋 **Task**: {name}\n💰 **Reward**: ${reward:.2f}"
    kb = [
        [InlineKeyboardButton("➡️ Open Link", url=url)],
        [InlineKeyboardButton("✅ Verify", callback_data=f"task_v_{tid}"),
         InlineKeyboardButton("⏭️ Skip", callback_data=f"task_s_{tid}")]
    ]
    if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# --- Withdrawal System ---
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        bal = conn.cursor().execute("SELECT balance FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
    if bal < MIN_WITHDRAWAL_LIMIT:
        await update.message.reply_text(f"❌ Minimum payout is ${MIN_WITHDRAWAL_LIMIT}.\nBalance: ${bal:.2f}")
        return ConversationHandler.END
    
    kb = [[InlineKeyboardButton("🔶 Binance (BEP20)", callback_data="w_net_BEP20"), 
           InlineKeyboardButton("🔷 TRON (TRC20)", callback_data="w_net_TRC20")]]
    await update.message.reply_text("Select payout network:", reply_markup=InlineKeyboardMarkup(kb))
    return State.CHOOSE_WITHDRAW_NETWORK

async def save_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text)
        uid = update.effective_user.id
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            bal = c.execute("SELECT balance FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
            if amt < 1.0 or amt > bal: await update.message.reply_text("Invalid amount."); return State.GET_WITHDRAW_AMOUNT
            
            c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amt, uid))
            c.execute("INSERT INTO withdrawals (user_id, amount, network, wallet_address) VALUES (?,?,?,?)", 
                      (uid, amt, context.user_data['net'], context.user_data['adr']))
            wid = c.lastrowid
            conn.commit()
            
        await update.message.reply_text("✅ Withdrawal Request Sent!", reply_markup=get_user_keyboard(uid))
        await context.bot.send_message(ADMIN_ID, f"🏧 **New Request**\nID: `{wid}`\nUser: `{uid}`\nAmt: `${amt:.2f}`\nWallet: `{context.user_data['adr']}`", parse_mode='Markdown')
        return ConversationHandler.END
    except: await update.message.reply_text("Enter a number:"); return State.GET_WITHDRAW_AMOUNT

# --- Coupon System ---
async def generate_coupon_text(code, budget, max_c, claims):
    return f"🎁 **COUPON CODE** 🎁\n\nCode: `{code}`\nBudget: ${budget:.2f}\nClaims: {claims}/{max_c}\n\n➡️ @{(await Application.get_instance().bot.get_me()).username}"

async def receive_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        data = c.execute("SELECT budget, max_claims, claims_count, status FROM coupons WHERE coupon_code = ?", (code,)).fetchone()
        if not data or data[3] != 'active' or data[2] >= data[1]: await update.message.reply_text("❌ Invalid/Expired."); return ConversationHandler.END
        if c.execute("SELECT 1 FROM claimed_coupons WHERE user_id = ? AND coupon_code = ?", (uid, code)).fetchone(): await update.message.reply_text("⚠️ Already claimed."); return ConversationHandler.END
        
        reward = data[0] / data[1]
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, uid))
        c.execute("INSERT INTO claimed_coupons (user_id, coupon_code) VALUES (?, ?)", (uid, code))
        c.execute("UPDATE coupons SET claims_count = claims_count + 1 WHERE coupon_code = ?", (code,))
        conn.commit()
        await update.message.reply_text(f"🎉 Claimed! +${reward:.2f}")
        
        # Live update channel message
        msg = c.execute("SELECT chat_id, message_id FROM coupon_messages WHERE coupon_code = ?", (code,)).fetchone()
        if msg:
            try: await context.bot.edit_message_text(await generate_coupon_text(code, data[0], data[1], data[2]+1), chat_id=msg[0], message_id=msg[1], parse_mode='Markdown')
            except: pass
    return ConversationHandler.END

# --- Admin Logic (Stats, Management, Mailing) ---
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("👑 Admin Mode", reply_markup=get_admin_keyboard())

async def admin_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE) as conn:
        reqs = conn.cursor().execute("SELECT withdrawal_id, user_id, amount, network, wallet_address FROM withdrawals WHERE status = 'pending'").fetchall()
    if not reqs: await update.message.reply_text("No pending withdrawals."); return
    for r in reqs:
        txt = f"ID: {r[0]}\nUser: {r[1]}\nAmt: ${r[2]}\nNet: {r[3]}\nWallet: `{r[4]}`"
        kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"p_app_{r[0]}"), 
               InlineKeyboardButton("❌ Reject", callback_data=f"p_rej_{r[0]}")]]
        await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def admin_mailing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    await update.message.reply_text("Send the message for broadcast (Text/Photo/Video):")
    return State.GET_MAIL_MESSAGE

async def broadcast_mailing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    with sqlite3.connect(DB_FILE) as conn: users = conn.cursor().execute("SELECT user_id FROM users").fetchall()
    msg = context.user_data['mail_msg']
    kb = InlineKeyboardMarkup([context.user_data['btns']]) if 'btns' in context.user_data else None
    s, f = 0, 0
    for (uid,) in users:
        try: await msg.copy(chat_id=uid, reply_markup=kb); s += 1
        except: f += 1
    await query.message.reply_text(f"📢 Done!\nSuccess: {s} | Fail: {f}")
    return ConversationHandler.END

# --- Admin Task & Tracking Logic ---
async def admin_task_mgmt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    kb = [[InlineKeyboardButton("➕ Add Task", callback_data="a_t_add"), InlineKeyboardButton("🗑️ Delete Task", callback_data="a_t_del")]]
    await update.message.reply_text("📋 Task Management:", reply_markup=InlineKeyboardMarkup(kb))

async def admin_track_mgmt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    kb = [[InlineKeyboardButton("➕ Add Main Channel", callback_data="a_tr_add"), InlineKeyboardButton("🗑️ Remove Channel", callback_data="a_tr_del")]]
    await update.message.reply_text("🔗 Forced Join Management:", reply_markup=InlineKeyboardMarkup(kb))

# --- Global Callback Router (The Brain) ---
async def global_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    uid = query.from_user.id
    
    # Task Callbacks
    if data.startswith("task_"):
        await query.answer()
        act, tid = data.split("_")[1], int(data.split("_")[2])
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            if act == 's': c.execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?,?)", (uid, tid)); conn.commit(); await show_next_task(update, context)
            else:
                task = c.execute("SELECT reward, target_chat_id FROM tasks WHERE task_id = ?", (tid,)).fetchone()
                try:
                    m = await context.bot.get_chat_member(task[1], uid)
                    if m.status in ['member', 'administrator', 'creator']:
                        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (task[0], uid))
                        c.execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?,?)", (uid, tid))
                        conn.commit(); await query.answer("Success!", show_alert=True); await show_next_task(update, context)
                    else: await query.answer("❌ Not joined!", show_alert=True)
                except: await query.answer("Bot needs Admin in channel.")

    # Withdrawal Approval
    elif data.startswith("p_"):
        act, wid = data.split("_")[1], int(data.split("_")[2])
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            req = c.execute("SELECT user_id, amount FROM withdrawals WHERE withdrawal_id = ?", (wid,)).fetchone()
            if act == 'app':
                c.execute("UPDATE withdrawals SET status = 'approved' WHERE withdrawal_id = ?", (wid,))
                await context.bot.send_message(req[0], f"✅ Withdrawal of ${req[1]} approved!")
            else:
                c.execute("UPDATE withdrawals SET status = 'rejected' WHERE withdrawal_id = ?", (wid,))
                c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (req[1], req[0]))
                await context.bot.send_message(req[0], f"❌ Withdrawal of ${req[1]} rejected. Funds returned.")
            conn.commit(); await query.message.delete()

    # Track Management Delete
    elif data == "a_tr_del":
        with sqlite3.connect(DB_FILE) as conn:
            chans = conn.cursor().execute("SELECT id, channel_name FROM forced_channels WHERE status = 'active'").fetchall()
        kb = [[InlineKeyboardButton(f"🗑️ {c[1]}", callback_data=f"tr_rem_{c[0]}")] for c in chans]
        await query.edit_message_text("Select channel to remove:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("tr_rem_"):
        cid = int(data.split("_")[2])
        with sqlite3.connect(DB_FILE) as conn: conn.cursor().execute("UPDATE forced_channels SET status = 'deleted' WHERE id = ?", (cid,)); conn.commit()
        await query.answer("Removed."); await query.message.delete()

    # Task Management Delete
    elif data == "a_t_del":
        with sqlite3.connect(DB_FILE) as conn:
            tsks = conn.cursor().execute("SELECT task_id, task_name FROM tasks WHERE status = 'active'").fetchall()
        kb = [[InlineKeyboardButton(f"🗑️ {t[1]}", callback_data=f"t_rem_{t[0]}")] for t in tsks]
        await query.edit_message_text("Select task to delete:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("t_rem_"):
        tid = int(data.split("_")[2])
        with sqlite3.connect(DB_FILE) as conn: conn.cursor().execute("UPDATE tasks SET status = 'deleted' WHERE task_id = ?", (tid,)); conn.commit()
        await query.answer("Deleted."); await query.message.delete()

    # Misc
    elif data == "verify_membership":
        if await is_member_or_send_join_message(update, context): await query.message.delete(); await query.message.reply_text("✅ Access Granted!", reply_markup=get_user_keyboard(uid))

# --- Main App ---
def main():
    setup_database()
    app = Application.builder().token(BOT_API_KEY).build()

    # Admin Conversation Handlers
    task_add = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u,c: u.callback_query.message.reply_text("Task Name:"), pattern="^a_t_add$")],
        states={
            State.GET_TASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: (c.user_data.update({'tn':u.message.text}), u.message.reply_text("Channel ID:"), State.GET_TARGET_CHAT_ID)[2])],
            State.GET_TARGET_CHAT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: (c.user_data.update({'tc':u.message.text}), u.message.reply_text("Task URL:"), State.GET_TASK_URL)[2])],
            State.GET_TASK_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: (c.user_data.update({'tu':u.message.text}), u.message.reply_text("Reward:"), State.GET_TASK_REWARD)[2])],
            State.GET_TASK_REWARD: [MessageHandler(filters.TEXT, lambda u,c: (sqlite3.connect(DB_FILE).cursor().execute("INSERT INTO tasks (task_name, reward, target_chat_id, task_url) VALUES (?,?,?,?)", (c.user_data['tn'], float(u.message.text), c.user_data['tc'], c.user_data['tu'])), sqlite3.connect(DB_FILE).commit(), u.message.reply_text("✅ Done!", reply_markup=get_admin_keyboard()), ConversationHandler.END)[3])]
        }, fallbacks=[]
    )

    withdraw_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_start)],
        states={
            State.CHOOSE_WITHDRAW_NETWORK: [CallbackQueryHandler(lambda u,c: (c.user_data.update({'net':u.callback_query.data.split("_")[2]}), u.callback_query.edit_message_text("Wallet:"), State.GET_WALLET_ADDRESS)[2], pattern="^w_net_")],
            State.GET_WALLET_ADDRESS: [MessageHandler(filters.TEXT, lambda u,c: (c.user_data.update({'adr':u.message.text}), u.message.reply_text("Amount:"), State.GET_WITHDRAW_AMOUNT)[2])],
            State.GET_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT, save_withdraw_amount)]
        }, fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), handle_balance))
    app.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), handle_referral))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), handle_daily_bonus))
    app.add_handler(MessageHandler(filters.Regex("^📋 Tasks$"), tasks_entry))
    app.add_handler(MessageHandler(filters.Regex("^🎟️ Coupon Code$"), lambda u,c: (u.message.reply_text("Enter Code:"), State.AWAIT_COUPON_CODE)[1]))
    app.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), admin_panel))
    app.add_handler(MessageHandler(filters.Regex("^📧 Mailing$"), admin_mailing_start))
    app.add_handler(MessageHandler(filters.Regex("^🏧 Withdrawals$"), admin_withdrawals))
    app.add_handler(MessageHandler(filters.Regex("^📋 Task Management$"), admin_task_mgmt))
    app.add_handler(MessageHandler(filters.Regex("^🔗 Main Track Management$"), admin_track_mgmt))
    app.add_handler(MessageHandler(filters.Regex("^📊 Bot Stats$"), admin_stats))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), start))

    app.add_handler(task_add)
    app.add_handler(withdraw_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_coupon)) # For Coupon logic
    app.add_handler(CallbackQueryHandler(global_callback))

    print("Bot is starting...")
    app.run_polling()

if __name__ == "__main__":
    main()

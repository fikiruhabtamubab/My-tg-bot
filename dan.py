import logging
import sqlite3
import io
import os
import random
import asyncio
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
    # Admin Tasks
    GET_TASK_NAME = 1; GET_TARGET_CHAT_ID = 2; GET_TASK_URL = 3; GET_TASK_REWARD = 4
    # Withdrawals
    CHOOSE_WITHDRAW_NETWORK = 5; GET_WALLET_ADDRESS = 6; GET_WITHDRAW_AMOUNT = 7
    # Mailing
    GET_MAIL_MESSAGE = 8; AWAIT_BUTTON_OR_SEND = 9; GET_BUTTON_DATA = 10
    # Tracking (Forced Join)
    GET_TRACKED_NAME = 11; GET_TRACKED_ID = 12; GET_TRACKED_URL = 13
    # Coupons
    GET_COUPON_BUDGET = 14; GET_COUPON_MAX_CLAIMS = 15; AWAIT_COUPON_CODE = 16

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

# --- Membership Helpers ---
async def get_unjoined_channels(user_id, context):
    with sqlite3.connect(DB_FILE) as conn:
        channels = conn.cursor().execute("SELECT channel_name, channel_id, channel_url FROM forced_channels WHERE status = 'active'").fetchall()
    unjoined = []
    for name, cid, url in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=cid, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                unjoined.append({'name': name, 'url': url})
        except: continue
    return unjoined

async def is_member_or_send_join_message(update, context):
    user_id = update.effective_user.id
    if user_id == ADMIN_ID: return True
    unjoined = await get_unjoined_channels(user_id, context)
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
        
        if context.args: # Referral logic
            try:
                ref_id = int(context.args[0])
                existing_ref = c.execute("SELECT referred_by FROM users WHERE user_id = ?", (user.id,)).fetchone()
                if ref_id != user.id and (not existing_ref or existing_ref[0] is None):
                    c.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (ref_id, user.id))
                    c.execute("UPDATE users SET balance = balance + ?, referral_count = referral_count + 1 WHERE user_id = ?", (REFERRAL_BONUS, ref_id))
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

# --- Task Flow ---
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
    kb = [[InlineKeyboardButton("➡️ Open Link", url=url)],
          [InlineKeyboardButton("✅ Verify", callback_data=f"task_v_{tid}"), InlineKeyboardButton("⏭️ Skip", callback_data=f"task_s_{tid}")]]
    
    if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# --- Withdrawal Flow ---
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
            if amt < 1.0 or amt > bal:
                await update.message.reply_text(f"Invalid amount. Max: {bal}")
                return State.GET_WITHDRAW_AMOUNT
            
            c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amt, uid))
            c.execute("INSERT INTO withdrawals (user_id, amount, network, wallet_address) VALUES (?,?,?,?)", 
                      (uid, amt, context.user_data['net'], context.user_data['adr']))
            wid = c.lastrowid
            conn.commit()
            
        await update.message.reply_text("✅ Withdrawal Request Sent!", reply_markup=get_user_keyboard(uid))
        await context.bot.send_message(ADMIN_ID, f"🏧 **New Request**\nID: `{wid}`\nAmt: `${amt:.2f}`", parse_mode='Markdown')
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return State.GET_WITHDRAW_AMOUNT

# --- Admin Function: Stats (THE FIX) ---
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        total_u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_b = c.execute("SELECT SUM(balance) FROM users").fetchone()[0] or 0
        active_t = c.execute("SELECT COUNT(*) FROM tasks WHERE status = 'active'").fetchone()[0]
        pend_w = c.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'").fetchone()[0]
    
    stats = (f"📊 **Bot Stats**\n\n"
             f"👥 Total Users: {total_u}\n"
             f"💰 Total Balance: ${total_b:.2f}\n"
             f"📋 Active Tasks: {active_t}\n"
             f"🏧 Pending Withdrawals: {pend_w}")
    await update.message.reply_text(stats, parse_mode='Markdown')

# --- Admin Function: Mailing ---
async def admin_mailing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    await update.message.reply_text("Send the message you want to broadcast (Text/Image/Video):")
    return State.GET_MAIL_MESSAGE

async def receive_mail_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['broadcast_msg'] = update.message
    kb = [[InlineKeyboardButton("Add Button", callback_data="mail_add_btn"), 
           InlineKeyboardButton("🚀 Send Now", callback_data="mail_send_now")]]
    await update.message.reply_text("Preview saved. Add a button or send?", reply_markup=InlineKeyboardMarkup(kb))
    return State.AWAIT_BUTTON_OR_SEND

async def broadcast_mailing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Broadcasting...")
    with sqlite3.connect(DB_FILE) as conn:
        users = conn.cursor().execute("SELECT user_id FROM users").fetchall()
    
    msg = context.user_data['broadcast_msg']
    btn_kb = context.user_data.get('broadcast_kb')
    
    success, fail = 0, 0
    for (uid,) in users:
        try:
            await msg.copy(chat_id=uid, reply_markup=btn_kb)
            success += 1
            await asyncio.sleep(0.05) # Rate limit protection
        except: fail += 1
    
    await query.message.reply_text(f"📢 Broadcast Complete\n✅ Success: {success}\n❌ Failed: {fail}")
    return ConversationHandler.END

# --- Coupon Management ---
async def coupon_mgmt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    kb = [[InlineKeyboardButton("➕ Create Coupon", callback_data="c_add")]]
    await update.message.reply_text("🎟️ Coupon Management", reply_markup=InlineKeyboardMarkup(kb))

async def handle_coupon_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        coupon = c.execute("SELECT budget, max_claims, claims_count FROM coupons WHERE coupon_code = ? AND status = 'active'", (code,)).fetchone()
        if not coupon: 
            await update.message.reply_text("❌ Invalid or expired code.")
            return
        
        already = c.execute("SELECT 1 FROM claimed_coupons WHERE user_id = ? AND coupon_code = ?", (uid, code)).fetchone()
        if already:
            await update.message.reply_text("⚠️ You already claimed this!")
            return

        if coupon[2] >= coupon[1]:
            await update.message.reply_text("❌ Coupon limit reached.")
            return

        reward = coupon[0] / coupon[1]
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, uid))
        c.execute("INSERT INTO claimed_coupons (user_id, coupon_code) VALUES (?,?)", (uid, code))
        c.execute("UPDATE coupons SET claims_count = claims_count + 1 WHERE coupon_code = ?", (code,))
        conn.commit()
        await update.message.reply_text(f"🎉 Success! You received ${reward:.2f}")

# --- Global Callback Router ---
async def global_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    uid = query.from_user.id

    if data.startswith("task_v_"): # Task Verify
        tid = int(data.split("_")[2])
        with sqlite3.connect(DB_FILE) as conn:
            target_id = conn.cursor().execute("SELECT target_chat_id, reward FROM tasks WHERE task_id = ?", (tid,)).fetchone()
            try:
                member = await context.bot.get_chat_member(target_id[0], uid)
                if member.status in ['member', 'administrator', 'creator']:
                    conn.cursor().execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?,?)", (uid, tid))
                    conn.cursor().execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (target_id[1], uid))
                    conn.commit()
                    await query.answer("✅ Verified! Reward added.", show_alert=True)
                    await show_next_task(update, context)
                else: await query.answer("❌ You haven't joined yet!", show_alert=True)
            except: await query.answer("Error verifying. Ensure bot is admin in channel.")

    elif data == "mail_send_now":
        return await broadcast_mailing(update, context)
    
    elif data == "verify_membership":
        if await is_member_or_send_join_message(update, context):
            await query.message.delete()
            await query.message.reply_text("✅ Access Granted!", reply_markup=get_user_keyboard(uid))

# --- Main App Execution ---
def main():
    setup_database()
    app = Application.builder().token(BOT_API_KEY).build()

    # --- Conversation Handlers ---
    
    # Withdrawal
    withdraw_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_start)],
        states={
            State.CHOOSE_WITHDRAW_NETWORK: [CallbackQueryHandler(lambda u,c: (c.user_data.update({'net':u.callback_query.data.split("_")[2]}), u.callback_query.edit_message_text("Send Wallet Address:"), State.GET_WALLET_ADDRESS)[2], pattern="^w_net_")],
            State.GET_WALLET_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: (c.user_data.update({'adr':u.message.text}), u.message.reply_text("Enter Amount:"), State.GET_WITHDRAW_AMOUNT)[2])],
            State.GET_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_withdraw_amount)]
        }, fallbacks=[CommandHandler("cancel", start)]
    )

    # Mailing
    mailing_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📧 Mailing$"), admin_mailing_start)],
        states={
            State.GET_MAIL_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_mail_content)],
            State.AWAIT_BUTTON_OR_SEND: [CallbackQueryHandler(global_callback)]
        }, fallbacks=[CommandHandler("cancel", start)]
    )

    # Task Add
    task_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u,c: (u.callback_query.message.reply_text("Task Name:"), State.GET_TASK_NAME)[1], pattern="^a_t_add$")],
        states={
            State.GET_TASK_NAME: [MessageHandler(filters.TEXT, lambda u,c: (c.user_data.update({'tn':u.message.text}), u.message.reply_text("Channel ID:"), State.GET_TARGET_CHAT_ID)[2])],
            State.GET_TARGET_CHAT_ID: [MessageHandler(filters.TEXT, lambda u,c: (c.user_data.update({'tc':u.message.text}), u.message.reply_text("Task URL:"), State.GET_TASK_URL)[2])],
            State.GET_TASK_URL: [MessageHandler(filters.TEXT, lambda u,c: (c.user_data.update({'tu':u.message.text}), u.message.reply_text("Reward:"), State.GET_TASK_REWARD)[2])],
            State.GET_TASK_REWARD: [MessageHandler(filters.TEXT, lambda u,c: (sqlite3.connect(DB_FILE).cursor().execute("INSERT INTO tasks (task_name, reward, target_chat_id, task_url) VALUES (?,?,?,?)", (c.user_data['tn'], float(u.message.text), c.user_data['tc'], c.user_data['tu'])), sqlite3.connect(DB_FILE).commit(), u.message.reply_text("✅ Task Added!"), ConversationHandler.END)[3])]
        }, fallbacks=[]
    )

    # --- Register Handlers ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), handle_balance))
    app.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), handle_referral))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), handle_daily_bonus))
    app.add_handler(MessageHandler(filters.Regex("^📋 Tasks$"), tasks_entry))
    app.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), lambda u,c: u.message.reply_text("👑 Admin Panel", reply_markup=get_admin_keyboard()) if u.effective_user.id == ADMIN_ID else None))
    app.add_handler(MessageHandler(filters.Regex("^📊 Bot Stats$"), admin_stats))
    app.add_handler(MessageHandler(filters.Regex("^📋 Task Management$"), lambda u,c: u.message.reply_text("Tasks:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Task", callback_data="a_t_add")]])) if u.effective_user.id == ADMIN_ID else None))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), start))
    app.add_handler(MessageHandler(filters.Regex("^🎟️ Coupon Code$"), lambda u,c: u.message.reply_text("Enter your coupon code:")))
    
    # Specific Message Handler for Coupons (Catch-all text that isn't a command/button)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE, handle_coupon_redeem))

    app.add_handler(withdraw_conv)
    app.add_handler(mailing_conv)
    app.add_handler(task_add_conv)
    app.add_handler(CallbackQueryHandler(global_callback))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()       [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📋 Tasks")],
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

# --- Membership Helpers ---
async def get_unjoined_channels(user_id, context):
    with sqlite3.connect(DB_FILE) as conn:
        channels = conn.cursor().execute("SELECT channel_name, channel_id, channel_url FROM forced_channels WHERE status = 'active'").fetchall()
    unjoined = []
    for name, cid, url in channels:
        try:
            member = await context.bot.get_chat_member(chat_id=cid, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                unjoined.append({'name': name, 'url': url})
        except: continue
    return unjoined

async def is_member_or_send_join_message(update, context):
    user_id = update.effective_user.id
    if user_id == ADMIN_ID: return True
    unjoined = await get_unjoined_channels(user_id, context)
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
        
        if context.args: # Referral logic
            try:
                ref_id = int(context.args[0])
                existing_ref = c.execute("SELECT referred_by FROM users WHERE user_id = ?", (user.id,)).fetchone()
                if ref_id != user.id and (not existing_ref or existing_ref[0] is None):
                    c.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (ref_id, user.id))
                    c.execute("UPDATE users SET balance = balance + ?, referral_count = referral_count + 1 WHERE user_id = ?", (REFERRAL_BONUS, ref_id))
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

# --- Task Flow ---
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
    kb = [[InlineKeyboardButton("➡️ Open Link", url=url)],
          [InlineKeyboardButton("✅ Verify", callback_data=f"task_v_{tid}"), InlineKeyboardButton("⏭️ Skip", callback_data=f"task_s_{tid}")]]
    
    if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# --- Withdrawal Flow ---
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
            if amt < 1.0 or amt > bal:
                await update.message.reply_text(f"Invalid amount. Max: {bal}")
                return State.GET_WITHDRAW_AMOUNT
            
            c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amt, uid))
            c.execute("INSERT INTO withdrawals (user_id, amount, network, wallet_address) VALUES (?,?,?,?)", 
                      (uid, amt, context.user_data['net'], context.user_data['adr']))
            wid = c.lastrowid
            conn.commit()
            
        await update.message.reply_text("✅ Withdrawal Request Sent!", reply_markup=get_user_keyboard(uid))
        await context.bot.send_message(ADMIN_ID, f"🏧 **New Request**\nID: `{wid}`\nAmt: `${amt:.2f}`", parse_mode='Markdown')
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return State.GET_WITHDRAW_AMOUNT

# --- Admin Function: Stats (THE FIX) ---
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        total_u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_b = c.execute("SELECT SUM(balance) FROM users").fetchone()[0] or 0
        active_t = c.execute("SELECT COUNT(*) FROM tasks WHERE status = 'active'").fetchone()[0]
        pend_w = c.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'").fetchone()[0]
    
    stats = (f"📊 **Bot Stats**\n\n"
             f"👥 Total Users: {total_u}\n"
             f"💰 Total Balance: ${total_b:.2f}\n"
             f"📋 Active Tasks: {active_t}\n"
             f"🏧 Pending Withdrawals: {pend_w}")
    await update.message.reply_text(stats, parse_mode='Markdown')

# --- Admin Function: Mailing ---
async def admin_mailing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    await update.message.reply_text("Send the message you want to broadcast (Text/Image/Video):")
    return State.GET_MAIL_MESSAGE

async def receive_mail_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['broadcast_msg'] = update.message
    kb = [[InlineKeyboardButton("Add Button", callback_data="mail_add_btn"), 
           InlineKeyboardButton("🚀 Send Now", callback_data="mail_send_now")]]
    await update.message.reply_text("Preview saved. Add a button or send?", reply_markup=InlineKeyboardMarkup(kb))
    return State.AWAIT_BUTTON_OR_SEND

async def broadcast_mailing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Broadcasting...")
    with sqlite3.connect(DB_FILE) as conn:
        users = conn.cursor().execute("SELECT user_id FROM users").fetchall()
    
    msg = context.user_data['broadcast_msg']
    btn_kb = context.user_data.get('broadcast_kb')
    
    success, fail = 0, 0
    for (uid,) in users:
        try:
            await msg.copy(chat_id=uid, reply_markup=btn_kb)
            success += 1
            await asyncio.sleep(0.05) # Rate limit protection
        except: fail += 1
    
    await query.message.reply_text(f"📢 Broadcast Complete\n✅ Success: {success}\n❌ Failed: {fail}")
    return ConversationHandler.END

# --- Coupon Management ---
async def coupon_mgmt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    kb = [[InlineKeyboardButton("➕ Create Coupon", callback_data="c_add")]]
    await update.message.reply_text("🎟️ Coupon Management", reply_markup=InlineKeyboardMarkup(kb))

async def handle_coupon_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        coupon = c.execute("SELECT budget, max_claims, claims_count FROM coupons WHERE coupon_code = ? AND status = 'active'", (code,)).fetchone()
        if not coupon: 
            await update.message.reply_text("❌ Invalid or expired code.")
            return
        
        already = c.execute("SELECT 1 FROM claimed_coupons WHERE user_id = ? AND coupon_code = ?", (uid, code)).fetchone()
        if already:
            await update.message.reply_text("⚠️ You already claimed this!")
            return

        if coupon[2] >= coupon[1]:
            await update.message.reply_text("❌ Coupon limit reached.")
            return

        reward = coupon[0] / coupon[1]
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, uid))
        c.execute("INSERT INTO claimed_coupons (user_id, coupon_code) VALUES (?,?)", (uid, code))
        c.execute("UPDATE coupons SET claims_count = claims_count + 1 WHERE coupon_code = ?", (code,))
        conn.commit()
        await update.message.reply_text(f"🎉 Success! You received ${reward:.2f}")

# --- Global Callback Router ---
async def global_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    uid = query.from_user.id

    if data.startswith("task_v_"): # Task Verify
        tid = int(data.split("_")[2])
        with sqlite3.connect(DB_FILE) as conn:
            target_id = conn.cursor().execute("SELECT target_chat_id, reward FROM tasks WHERE task_id = ?", (tid,)).fetchone()
            try:
                member = await context.bot.get_chat_member(target_id[0], uid)
                if member.status in ['member', 'administrator', 'creator']:
                    conn.cursor().execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?,?)", (uid, tid))
                    conn.cursor().execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (target_id[1], uid))
                    conn.commit()
                    await query.answer("✅ Verified! Reward added.", show_alert=True)
                    await show_next_task(update, context)
                else: await query.answer("❌ You haven't joined yet!", show_alert=True)
            except: await query.answer("Error verifying. Ensure bot is admin in channel.")

    elif data == "mail_send_now":
        return await broadcast_mailing(update, context)
    
    elif data == "verify_membership":
        if await is_member_or_send_join_message(update, context):
            await query.message.delete()
            await query.message.reply_text("✅ Access Granted!", reply_markup=get_user_keyboard(uid))

# --- Main App Execution ---
def main():
    setup_database()
    app = Application.builder().token(BOT_API_KEY).build()

    # --- Conversation Handlers ---
    
    # Withdrawal
    withdraw_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_start)],
        states={
            State.CHOOSE_WITHDRAW_NETWORK: [CallbackQueryHandler(lambda u,c: (c.user_data.update({'net':u.callback_query.data.split("_")[2]}), u.callback_query.edit_message_text("Send Wallet Address:"), State.GET_WALLET_ADDRESS)[2], pattern="^w_net_")],
            State.GET_WALLET_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: (c.user_data.update({'adr':u.message.text}), u.message.reply_text("Enter Amount:"), State.GET_WITHDRAW_AMOUNT)[2])],
            State.GET_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_withdraw_amount)]
        }, fallbacks=[CommandHandler("cancel", start)]
    )

    # Mailing
    mailing_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📧 Mailing$"), admin_mailing_start)],
        states={
            State.GET_MAIL_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_mail_content)],
            State.AWAIT_BUTTON_OR_SEND: [CallbackQueryHandler(global_callback)]
        }, fallbacks=[CommandHandler("cancel", start)]
    )

    # Task Add
    task_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u,c: (u.callback_query.message.reply_text("Task Name:"), State.GET_TASK_NAME)[1], pattern="^a_t_add$")],
        states={
            State.GET_TASK_NAME: [MessageHandler(filters.TEXT, lambda u,c: (c.user_data.update({'tn':u.message.text}), u.message.reply_text("Channel ID:"), State.GET_TARGET_CHAT_ID)[2])],
            State.GET_TARGET_CHAT_ID: [MessageHandler(filters.TEXT, lambda u,c: (c.user_data.update({'tc':u.message.text}), u.message.reply_text("Task URL:"), State.GET_TASK_URL)[2])],
            State.GET_TASK_URL: [MessageHandler(filters.TEXT, lambda u,c: (c.user_data.update({'tu':u.message.text}), u.message.reply_text("Reward:"), State.GET_TASK_REWARD)[2])],
            State.GET_TASK_REWARD: [MessageHandler(filters.TEXT, lambda u,c: (sqlite3.connect(DB_FILE).cursor().execute("INSERT INTO tasks (task_name, reward, target_chat_id, task_url) VALUES (?,?,?,?)", (c.user_data['tn'], float(u.message.text), c.user_data['tc'], c.user_data['tu'])), sqlite3.connect(DB_FILE).commit(), u.message.reply_text("✅ Task Added!"), ConversationHandler.END)[3])]
        }, fallbacks=[]
    )

    # --- Register Handlers ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), handle_balance))
    app.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), handle_referral))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), handle_daily_bonus))
    app.add_handler(MessageHandler(filters.Regex("^📋 Tasks$"), tasks_entry))
    app.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), lambda u,c: u.message.reply_text("👑 Admin Panel", reply_markup=get_admin_keyboard()) if u.effective_user.id == ADMIN_ID else None))
    app.add_handler(MessageHandler(filters.Regex("^📊 Bot Stats$"), admin_stats))
    app.add_handler(MessageHandler(filters.Regex("^📋 Task Management$"), lambda u,c: u.message.reply_text("Tasks:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Task", callback_data="a_t_add")]])) if u.effective_user.id == ADMIN_ID else None))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), start))
    app.add_handler(MessageHandler(filters.Regex("^🎟️ Coupon Code$"), lambda u,c: u.message.reply_text("Enter your coupon code:")))
    
    # Specific Message Handler for Coupons (Catch-all text that isn't a command/button)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE, handle_coupon_redeem))

    app.add_handler(withdraw_conv)
    app.add_handler(mailing_conv)
    app.add_handler(task_add_conv)
    app.add_handler(CallbackQueryHandler(global_callback))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()

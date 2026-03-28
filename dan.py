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

# --- Configuration ---
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
        c.execute("CREATE TABLE IF NOT EXISTS coupon_forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')")
        c.execute("CREATE TABLE IF NOT EXISTS coupon_messages (coupon_code TEXT, chat_id INTEGER, message_id INTEGER, PRIMARY KEY (coupon_code, chat_id))")
        conn.commit()

# --- Keyboard Definitions ---
def get_user_keyboard(user_id):
    user_buttons = [
        [KeyboardButton("💰 Balance"), KeyboardButton("👥 Referral")],
        [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📋 Tasks")],
        [KeyboardButton("💸 Withdraw"), KeyboardButton("🎟️ Coupon Code")]
    ]
    if user_id == ADMIN_ID:
        user_buttons.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(user_buttons, resize_keyboard=True)

def get_admin_keyboard():
    admin_buttons = [
        [KeyboardButton("📧 Mailing"), KeyboardButton("📋 Task Management")],
        [KeyboardButton("🎟️ Coupon Management"), KeyboardButton("📊 Bot Stats")],
        [KeyboardButton("🏧 Withdrawals"), KeyboardButton("🔗 Main Track Management")],
        [KeyboardButton("⬅️ Back to User Menu")],
    ]
    return ReplyKeyboardMarkup(admin_buttons, resize_keyboard=True)

# --- Membership Helpers ---
async def get_unjoined_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE, table_name: str) -> list:
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        tracked_channels = conn.cursor().execute(f"SELECT channel_name, channel_id, channel_url FROM {table_name} WHERE status = 'active'").fetchall()
    unjoined = []
    for name, channel_id, url in tracked_channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                unjoined.append({'name': name, 'url': url})
        except (BadRequest, Forbidden): continue
    return unjoined

async def is_member_or_send_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user or user.id == ADMIN_ID: return True
    unjoined = await get_unjoined_channels(user.id, context, 'forced_channels')
    if unjoined:
        message_text = "⚠️ **Access Denied**\n\nYou must join our channels to use the bot:"
        keyboard = [[InlineKeyboardButton(f"➡️ Join {channel['name']}", url=channel['url'])] for channel in unjoined]
        keyboard.append([InlineKeyboardButton("✅ Checked, Try Again", callback_data="verify_membership")])
        target_message = update.message or update.callback_query.message
        await target_message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return False
    return True

# --- Main Entry ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id:
                context.user_data['referrer_id'] = referrer_id
        except (ValueError, IndexError): pass
    
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username))
        
        is_new = c.execute("SELECT referred_by FROM users WHERE user_id = ?", (user.id,)).fetchone()[0] is None
        referrer_id = context.user_data.get('referrer_id')
        
        if is_new and referrer_id:
            c.execute("UPDATE users SET balance = balance + ?, referred_by = ? WHERE user_id = ?", (REFERRAL_BONUS, referrer_id, user.id))
            c.execute("UPDATE users SET balance = balance + ?, referral_count = referral_count + 1 WHERE user_id = ?", (REFERRAL_BONUS, referrer_id))
            conn.commit()
            try: await context.bot.send_message(referrer_id, f"✅ **New Referral!**\nYou earned ${REFERRAL_BONUS:.2f}!", parse_mode='Markdown')
            except: pass

    if await is_member_or_send_join_message(update, context):
        await update.message.reply_text(f"👋 Welcome, {user.first_name}!", reply_markup=get_user_keyboard(user.id))

# --- Task System (Direct Flow) ---
async def tasks_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await is_member_or_send_join_message(update, context):
        await show_next_task(update, context)

async def show_next_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    query = update.callback_query
    
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        task = conn.cursor().execute("""
            SELECT task_id, task_name, reward, task_url FROM tasks 
            WHERE status = 'active' AND task_id NOT IN (SELECT task_id FROM completed_tasks WHERE user_id = ?)
            ORDER BY RANDOM() LIMIT 1
        """, (user_id,)).fetchone()

    if not task:
        msg = "🎉 **All tasks completed!**\nPlease check back later for more tasks."
        if query: await query.edit_message_text(msg, parse_mode='Markdown')
        else: await update.message.reply_text(msg, parse_mode='Markdown')
        return

    tid, name, reward, url = task
    message_text = f"📋 **New Task**\n\n{name}\n\n💰 Reward: **${reward:.2f}**"
    keyboard = [
        [InlineKeyboardButton("➡️ Open Task Link", url=url)],
        [InlineKeyboardButton("✅ Verify Join", callback_data=f"task_v_{tid}"),
         InlineKeyboardButton("⏭️ Skip", callback_data=f"task_s_{tid}")]
    ]
    
    if query: await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else: await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- User Profile & Bonus ---
async def handle_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        balance = conn.cursor().execute("SELECT balance FROM users WHERE user_id = ?", (update.effective_user.id,)).fetchone()[0]
    await update.message.reply_text(f"💰 Your balance: **${balance:.2f}**", parse_mode='Markdown')

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        count = conn.cursor().execute("SELECT referral_count FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
    bot = await context.bot.get_me()
    link = f"https://t.me/{bot.username}?start={user_id}"
    await update.message.reply_text(f"👥 **Referral Program**\n\nEarn **${REFERRAL_BONUS:.2f}** per friend invited!\n\nYour Link: `{link}`\n\nTotal Referrals: **{count}**", parse_mode='Markdown')

async def handle_daily_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    today = date.today().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        last = c.execute("SELECT last_bonus_claim FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
        if last == today:
            await update.message.reply_text("❌ You have already claimed your bonus today!")
        else:
            c.execute("UPDATE users SET balance = balance + ?, last_bonus_claim = ? WHERE user_id = ?", (DAILY_BONUS, today, user_id))
            conn.commit()
            await update.message.reply_text(f"🎁 **Daily Bonus Received!**\nYou earned **${DAILY_BONUS:.2f}**.", parse_mode='Markdown')

# --- Withdrawal Flow ---
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    if not await is_member_or_send_join_message(update, context): return ConversationHandler.END
    user_id = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        balance = conn.cursor().execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
    if balance < MIN_WITHDRAWAL_LIMIT:
        await update.message.reply_text(f"❌ Minimum withdrawal is **${MIN_WITHDRAWAL_LIMIT:.2f}**.\nYour balance: ${balance:.2f}")
        return ConversationHandler.END
    
    keyboard = [[InlineKeyboardButton("🔶 Binance (BEP20)", callback_data="w_net_BEP20"), 
                 InlineKeyboardButton("🔷 TRON (TRC20)", callback_data="w_net_TRC20")]]
    await update.message.reply_text("Select your payout network:", reply_markup=InlineKeyboardMarkup(keyboard))
    return State.CHOOSE_WITHDRAW_NETWORK

async def get_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        amount = float(update.message.text)
        user_id = update.effective_user.id
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            balance = c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
            if amount < 1.0 or amount > balance:
                await update.message.reply_text(f"Invalid amount. Max: ${balance:.2f}"); return State.GET_WITHDRAW_AMOUNT
            
            c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
            c.execute("INSERT INTO withdrawals (user_id, amount, network, wallet_address) VALUES (?, ?, ?, ?)", 
                      (user_id, amount, context.user_data['network'], context.user_data['address']))
            wid = c.lastrowid
            conn.commit()
        
        await update.message.reply_text("✅ **Request Sent!**\nAdmin will review your payout soon.", reply_markup=get_user_keyboard(user_id), parse_mode='Markdown')
        await context.bot.send_message(ADMIN_ID, f"🏧 **New Withdrawal**\nID: `{wid}`\nUser: `{user_id}`\nAmount: `${amount:.2f}`\nWallet: `{context.user_data['address']}`", parse_mode='Markdown')
        return ConversationHandler.END
    except:
        await update.message.reply_text("Enter a valid numeric amount:"); return State.GET_WITHDRAW_AMOUNT

# --- Coupon Engine ---
async def generate_coupon_text(code, budget, max_c, current_c):
    status = "Active" if current_c < max_c else "Expired"
    return (f"🎁 **PROMO COUPON** 🎁\n\nCode: `{code}`\nBudget: ${budget:.2f}\nClaims: {current_c}/{max_c}\nStatus: {status}")

async def claim_coupon_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    if not await is_member_or_send_join_message(update, context): return ConversationHandler.END
    await update.message.reply_text("Please enter the Coupon Code:")
    return State.AWAIT_COUPON_CODE

async def receive_coupon_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    user_id = update.effective_user.id
    code = update.message.text.strip().upper()
    
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        data = c.execute("SELECT budget, max_claims, claims_count, status FROM coupons WHERE coupon_code = ?", (code,)).fetchone()
        
        if not data:
            await update.message.reply_text("❌ Invalid Code."); return ConversationHandler.END
        
        budget, max_c, current_c, status = data
        if c.execute("SELECT 1 FROM claimed_coupons WHERE user_id = ? AND coupon_code = ?", (user_id, code)).fetchone():
            await update.message.reply_text("⚠️ Already claimed."); return ConversationHandler.END
            
        if status != 'active' or current_c >= max_c:
            await update.message.reply_text("❌ Coupon Expired."); return ConversationHandler.END
            
        reward = budget / max_c
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, user_id))
        c.execute("INSERT INTO claimed_coupons (user_id, coupon_code) VALUES (?, ?)", (user_id, code))
        c.execute("UPDATE coupons SET claims_count = claims_count + 1 WHERE coupon_code = ?", (code,))
        conn.commit()
        
        await update.message.reply_text(f"🎉 **Claimed!**\nYou received **${reward:.2f}**.", parse_mode='Markdown')
        # Update Channel Message
        msg_data = c.execute("SELECT chat_id, message_id FROM coupon_messages WHERE coupon_code = ?", (code,)).fetchone()
        if msg_data:
            new_text = await generate_coupon_text(code, budget, max_c, current_c + 1)
            try: await context.bot.edit_message_text(new_text, chat_id=msg_data[0], message_id=msg_data[1], parse_mode='Markdown')
            except: pass
    return ConversationHandler.END

# --- Admin Section ---
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("👑 **Admin Panel Loaded**", reply_markup=get_admin_keyboard(), parse_mode='Markdown')

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_bal = c.execute("SELECT SUM(balance) FROM users").fetchone()[0] or 0
        tasks = c.execute("SELECT COUNT(*) FROM tasks WHERE status = 'active'").fetchone()[0]
    await update.message.reply_text(f"📊 **Bot Stats**\n\nTotal Users: {users}\nUser Wealth: ${total_bal:.2f}\nActive Tasks: {tasks}", parse_mode='Markdown')

# --- Admin Task Creation ---
async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    await update.message.reply_text("Step 1: Send Task Name (e.g., Join our Channel)")
    return State.GET_TASK_NAME

async def save_task_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        reward = float(update.message.text)
        d = context.user_data
        with sqlite3.connect(DB_FILE) as conn:
            conn.cursor().execute("INSERT INTO tasks (task_name, reward, target_chat_id, task_url) VALUES (?,?,?,?)",
                                (d['tn'], reward, d['tc'], d['tu']))
            conn.commit()
        await update.message.reply_text("✅ Task Added Successfully!", reply_markup=get_admin_keyboard())
        return ConversationHandler.END
    except: await update.message.reply_text("Invalid amount. Send again:"); return State.GET_TASK_REWARD

# --- Admin Mailing Engine ---
async def mailing_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    await update.message.reply_text("Send the message you want to broadcast (Text/Photo/Video):")
    return State.GET_MAIL_MESSAGE

async def broadcast_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query; await query.answer()
    with sqlite3.connect(DB_FILE) as conn:
        users = conn.cursor().execute("SELECT user_id FROM users").fetchall()
    
    msg = context.user_data['mail_msg']
    btn = InlineKeyboardMarkup([context.user_data['buttons']]) if context.user_data.get('buttons') else None
    
    success, fail = 0, 0
    for (uid,) in users:
        try:
            await msg.copy(chat_id=uid, reply_markup=btn)
            success += 1
        except: fail += 1
    
    await query.message.reply_text(f"📢 **Broadcast Finished**\n✅ Sent: {success}\n❌ Failed: {fail}", parse_mode='Markdown')
    return ConversationHandler.END

# --- Callback Router ---
async def button_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    
    if data.startswith("task_"):
        await query.answer()
        action, tid = data.split("_")[1], int(data.split("_")[2])
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            if action == 's':
                c.execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?, ?)", (user_id, tid))
                conn.commit()
                await show_next_task(update, context)
            elif action == 'v':
                task = c.execute("SELECT reward, target_chat_id FROM tasks WHERE task_id = ?", (tid,)).fetchone()
                try:
                    member = await context.bot.get_chat_member(task[1], user_id)
                    if member.status in ['member', 'administrator', 'creator']:
                        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (task[0], user_id))
                        c.execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?, ?)", (user_id, tid))
                        conn.commit()
                        await query.answer(f"✅ Success! +${task[0]}", show_alert=True)
                        await show_next_task(update, context)
                    else: await query.answer("❌ You haven't joined yet!", show_alert=True)
                except: await query.answer("Bot is not Admin in that channel.", show_alert=True)

    elif data == "verify_membership":
        if await is_member_or_send_join_message(update, context):
            await query.message.delete()
            await query.message.reply_text("✅ Access Granted!", reply_markup=get_user_keyboard(user_id))
            
    elif data.startswith("w_net_"):
        context.user_data['network'] = data.split("_")[2]
        await query.edit_message_text(f"Network: {context.user_data['network']}\nSend your Wallet Address:")
        context.user_data['state'] = 'wallet'

# --- Conversation Handlers ---
def main():
    setup_database()
    app = Application.builder().token(BOT_API_KEY).build()

    # Admin Task Conv
    task_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📋 Task Management$"), lambda u,c: u.message.reply_text("➕ Add Task?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Yes", callback_data="admin_add_t")]])))],
        states={
            State.GET_TASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: (c.user_data.update({'tn': u.message.text}), u.message.reply_text("Send Channel ID:"), State.GET_TARGET_CHAT_ID)[2])],
            State.GET_TARGET_CHAT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: (c.user_data.update({'tc': u.message.text}), u.message.reply_text("Send Link:"), State.GET_TASK_URL)[2])],
            State.GET_TASK_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: (c.user_data.update({'tu': u.message.text}), u.message.reply_text("Send Reward:"), State.GET_TASK_REWARD)[2])],
            State.GET_TASK_REWARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_task_reward)]
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)]
    )

    # Withdraw Conv
    with_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_start)],
        states={
            State.CHOOSE_WITHDRAW_NETWORK: [CallbackQueryHandler(lambda u,c: (c.user_data.update({'network': u.callback_query.data.split("_")[2]}), u.callback_query.edit_message_text("Send Wallet Address:"), State.GET_WALLET_ADDRESS)[2], pattern="^w_net_")],
            State.GET_WALLET_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: (c.user_data.update({'address': u.message.text}), u.message.reply_text("Amount to withdraw:"), State.GET_WITHDRAW_AMOUNT)[2])],
            State.GET_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_withdraw_amount)]
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)]
    )

    # Coupon Conv
    coup_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🎟️ Coupon Code$"), claim_coupon_entry)],
        states={State.AWAIT_COUPON_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_coupon_code)]},
        fallbacks=[]
    )

    # Mailing Conv
    mail_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📧 Mailing$"), mailing_start)],
        states={
            State.GET_MAIL_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, lambda u,c: (c.user_data.update({'mail_msg': u.message}), u.message.reply_text("Add button? Format: `Text - Link` or send `No`"), State.AWAIT_BUTTON_OR_SEND)[2])],
            State.AWAIT_BUTTON_OR_SEND: [MessageHandler(filters.TEXT, lambda u,c: (c.user_data.update({'buttons': InlineKeyboardButton(u.message.text.split(" - ")[0], url=u.message.text.split(" - ")[1])}), u.message.reply_text("Added! Send now?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Send Now", callback_data="mail_go")]]))) if " - " in u.message.text else u.message.reply_text("Ready?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Send Now", callback_data="mail_go")]])))],
        },
        fallbacks=[CallbackQueryHandler(broadcast_now, pattern="^mail_go$")]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), handle_balance))
    app.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), handle_referral))
    app.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), handle_daily_bonus))
    app.add_handler(MessageHandler(filters.Regex("^📋 Tasks$"), tasks_entry))
    app.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), admin_panel))
    app.add_handler(MessageHandler(filters.Regex("^📊 Bot Stats$"), admin_stats))
    app.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), start))
    
    app.add_handler(task_conv)
    app.add_handler(with_conv)
    app.add_handler(coup_conv)
    app.add_handler(mail_conv)
    app.add_handler(CallbackQueryHandler(add_task_start, pattern="^admin_add_t$"))
    app.add_handler(CallbackQueryHandler(button_router))

    print("Bot is alive...")
    app.run_polling()

if __name__ == "__main__":
    main()

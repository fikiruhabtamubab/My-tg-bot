import logging
import sqlite3
import io
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, date
from enum import Enum

from telegram import (
    ReplyKeyboardMarkup, Update, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.error import BadRequest, Forbidden

# --- Configuration (Choreo Environment Variables) ---
BOT_API_KEY = os.getenv("BOT_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
PORT = int(os.getenv("PORT", 8080)) # Choreo provides this automatically

REFERRAL_BONUS = 0.05
DAILY_BONUS = 0.05
MIN_WITHDRAWAL_LIMIT = 5.00
DB_FILE = os.getenv("DB_PATH", "user_data.db") # Ensure this path is in a Persistent Volume

# --- Health Check Server for Choreo ---
# Choreo expects the container to listen on the $PORT to pass health checks.
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        return # Disable logging for health checks

def run_health_check():
    server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
    server.serve_forever()

# --- Setup Logging & States ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

class State(Enum):
    GET_TASK_NAME = 1; GET_TARGET_CHAT_ID = 2; GET_TASK_URL = 3; GET_TASK_REWARD = 4
    CHOOSE_WITHDRAW_NETWORK = 5; GET_WALLET_ADDRESS = 6; GET_WITHDRAW_AMOUNT = 7
    GET_MAIL_MESSAGE = 8; AWAIT_BUTTON_OR_SEND = 9; GET_BUTTON_DATA = 10
    GET_TRACKED_NAME = 11; GET_TRACKED_ID = 12; GET_TRACKED_URL = 13
    GET_COUPON_BUDGET = 14; GET_COUPON_MAX_CLAIMS = 15
    AWAIT_COUPON_CODE = 16
    GET_COUPON_TRACKED_NAME = 17; GET_COUPON_TRACKED_ID = 18; GET_COUPON_TRACKED_URL = 19

# --- Database Setup ---
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

# --- Keyboard Definitions ---
def get_user_keyboard(user_id):
    user_buttons = [[KeyboardButton("💰 Balance"), KeyboardButton("👥 Referral")], [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📋 Tasks")], [KeyboardButton("💸 Withdraw"), KeyboardButton("🎟️ Coupon Code")]]
    if user_id == ADMIN_ID: user_buttons.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(user_buttons, resize_keyboard=True)

def get_admin_keyboard():
    admin_buttons = [
        [KeyboardButton("📧 Mailing"), KeyboardButton("📋 Task Management")],
        [KeyboardButton("🎟️ Coupon Management"), KeyboardButton("📊 Bot Stats")],
        [KeyboardButton("🏧 Withdrawals"), KeyboardButton("🔗 Main Track Management")],
        [KeyboardButton("⬅️ Back to User Menu")],
    ]
    return ReplyKeyboardMarkup(admin_buttons, resize_keyboard=True)

# === FORCED JOIN LOGIC ===
async def get_unjoined_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE, table_name: str) -> list:
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        tracked_channels = c.execute(f"SELECT channel_name, channel_id, channel_url FROM {table_name} WHERE status = 'active'").fetchall()
    if not tracked_channels: return []
    unjoined = []
    for name, channel_id, url in tracked_channels:
        try:
            member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                unjoined.append({'name': name, 'url': url})
        except (BadRequest, Forbidden) as e:
            logger.error(f"Error checking membership for {channel_id}: {e}. Bot might not be admin."); continue
    return unjoined

async def is_member_or_send_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user or user.id == ADMIN_ID: return True

    unjoined = await get_unjoined_channels(user.id, context, 'forced_channels')
    if unjoined:
        message_text = "⚠️ **Action Required**\n\nTo use the bot, you must remain in our channel(s):"
        keyboard = [[InlineKeyboardButton(f"➡️ Join {channel['name']}", url=channel['url'])] for channel in unjoined]
        keyboard.append([InlineKeyboardButton("✅ Done, Try Again", callback_data="clear_join_message")])
        
        target_message = update.message or update.callback_query.message
        await target_message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return False
    
    return True

async def gatekeeper_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_member_or_send_join_message(update, context):
        raise Application.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if context.args and len(context.args) > 0:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id:
                context.user_data['referrer_id'] = referrer_id
                logger.info(f"User {user.id} started with referrer ID {referrer_id}.")
        except (ValueError, IndexError):
            logger.warning(f"Invalid referrer ID in /start command: {context.args}")
            
    await check_membership_and_grant_access(update, context, 'verify_membership', 'forced_channels')

async def check_membership_and_grant_access(update: Update, context: ContextTypes.DEFAULT_TYPE, verify_callback: str, table_name: str):
    user = update.effective_user
    if not user and update.callback_query: user = update.callback_query.from_user

    unjoined = await get_unjoined_channels(user.id, context, table_name)
    if unjoined:
        message_text = "⚠️ **To proceed, you must join the following channel(s):**"
        keyboard = [[InlineKeyboardButton(f"➡️ Join {channel['name']}", url=channel['url'])] for channel in unjoined]
        keyboard.append([InlineKeyboardButton("✅ I Have Joined", callback_data=verify_callback)])
        
        target_message = update.callback_query.message if update.callback_query else update.effective_message
        if update.callback_query:
            await update.callback_query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        else:
            await target_message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return 'CONTINUE'

    if update.callback_query: await update.callback_query.message.delete()

    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        is_new_user = c.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,)).fetchone() is None
        
        if verify_callback == 'verify_coupon_membership': pass
        else:
            welcome_message = f"✅ Thank you for joining!\n\n👋 Welcome, {user.first_name}!";
            if "from_admin_back" in context.user_data:
                welcome_message = "⬅️ Switched back to User Mode."; del context.user_data["from_admin_back"]
            
            referrer_id = context.user_data.get('referrer_id')
            if is_new_user and referrer_id:
                if c.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,)).fetchone():
                    c.execute("INSERT INTO users (user_id, username, balance, referred_by) VALUES (?, ?, ?, ?)", (user.id, user.username, REFERRAL_BONUS, referrer_id))
                    c.execute("UPDATE users SET balance = balance + ?, referral_count = referral_count + 1 WHERE user_id = ?", (REFERRAL_BONUS, referrer_id))
                    conn.commit()
                    welcome_message = f"🎉 Welcome aboard, {user.first_name}!\nYou joined via a referral link and have received a welcome bonus of **${REFERRAL_BONUS:.2f}**!"
                    try:
                        await context.bot.send_message(chat_id=referrer_id, text=f"✅ Success! User *{user.first_name}* joined using your link.\nYou have been awarded **${REFERRAL_BONUS:.2f}**!", parse_mode='Markdown')
                    except (Forbidden, BadRequest) as e: logger.warning(f"Could not send referral notification to {referrer_id}: {e}")
                else:
                    c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username))
                    conn.commit()
                del context.user_data['referrer_id']
            else:
                c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username))
                conn.commit()

            await update.effective_message.reply_text(welcome_message, reply_markup=get_user_keyboard(user.id), parse_mode='Markdown')

    if verify_callback == 'verify_coupon_membership':
        await prompt_for_code(update, context)
        return 'PROCEED_TO_CODE'

    return ConversationHandler.END

# === USER & ADMIN HANDLERS ===
async def handle_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_member_or_send_join_message(update, context): return
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        balance = c.execute("SELECT balance FROM users WHERE user_id = ?", (update.effective_user.id,)).fetchone()[0]
    await update.message.reply_text(f"💰 Your current balance is: **${balance:.2f}**.", parse_mode='Markdown')

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_member_or_send_join_message(update, context): return
    user_id = update.effective_user.id
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, update.effective_user.username)); conn.commit()
        result = c.execute("SELECT referral_count FROM users WHERE user_id = ?", (user_id,)).fetchone()
        referral_count = result[0] if result else 0
        
    bot_username = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start={user_id}"
    await update.message.reply_text(f"🚀 Invite friends and earn **${REFERRAL_BONUS:.2f}** for each friend who joins!\n\nYour referral link is:\n`{referral_link}`\n\n👥 You have successfully referred **{referral_count}** friends.", parse_mode='Markdown')

async def handle_daily_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_member_or_send_join_message(update, context): return
    user_id = update.effective_user.id
    today = date.today()
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        last_claim_str = c.execute("SELECT last_bonus_claim FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
        if last_claim_str and date.fromisoformat(last_claim_str) >= today:
            await update.message.reply_text("You have already claimed your daily bonus today. Try again tomorrow!")
        else:
            c.execute("UPDATE users SET balance = balance + ?, last_bonus_claim = ? WHERE user_id = ?", (DAILY_BONUS, today.isoformat(), user_id)); conn.commit()
            await update.message.reply_text(f"🎉 You have received ${DAILY_BONUS:.2f} as a daily bonus!", parse_mode='Markdown')

async def display_next_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        task = c.execute("SELECT task_id, task_name, reward, task_url FROM tasks WHERE status = 'active' AND task_id NOT IN (SELECT task_id FROM completed_tasks WHERE user_id = ?) ORDER BY task_id ASC LIMIT 1", (user_id,)).fetchone()
    if not task:
        await update.effective_message.reply_text("🎉 You have completed all available tasks! Please check back later for new ones.")
        return
    task_id, name, reward, url = task
    keyboard = [[InlineKeyboardButton("➡️ Go to Channel/Group", url=url), InlineKeyboardButton("✅ I Have Joined", callback_data=f"verify_join_{task_id}")]]
    await update.effective_message.reply_text(f"**{name}**\nReward: **${reward:.2f}**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def handle_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_member_or_send_join_message(update, context): return
    await display_next_task(update, context)

async def admin_panel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("👑 Switched to Admin Mode.", reply_markup=get_admin_keyboard())

async def admin_back_to_user_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    context.user_data["from_admin_back"] = True
    await start(update, context)

async def handle_admin_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    keyboard = [[InlineKeyboardButton("➕ Add New Task", callback_data="admin_add_task_start")], [InlineKeyboardButton("🗑️ Delete Task", callback_data="admin_delete_task_list")]]
    message_text = "📋 *Task Management*"
    
    target_message = update.callback_query.message if update.callback_query else update.message
    if update.callback_query:
        await target_message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await target_message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def handle_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        total_users = c.execute("SELECT COUNT(user_id) FROM users").fetchone()[0]
    keyboard = [[InlineKeyboardButton("📥 Export User IDs (.xml)", callback_data="admin_export_users")]]
    await update.message.reply_text(f"📊 *Bot Statistics*\nTotal Users: **{total_users}**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def handle_admin_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        withdrawals = c.execute("SELECT w.withdrawal_id, u.username, w.amount, w.network, w.wallet_address FROM withdrawals w JOIN users u ON w.user_id = u.user_id WHERE w.status = 'pending'").fetchall()
    if not withdrawals: await update.message.reply_text("🏧 No pending withdrawals."); return
    await update.message.reply_text("--- 🏧 Pending Withdrawals ---")
    for w_id, u_name, amount, network, address in withdrawals:
        message = f"ID: `{w_id}` | User: @{u_name or 'N/A'}\nAmount: **${amount:.2f}** ({network})\nAddress: `{address}`"
        keyboard = [[InlineKeyboardButton(f"✅ Approve #{w_id}", callback_data=f"approve_{w_id}"), InlineKeyboardButton(f"❌ Reject #{w_id}", callback_data=f"reject_{w_id}")]]
        await update.message.reply_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def handle_admin_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    keyboard = [
        [InlineKeyboardButton("➕ Add Main Channel", callback_data="admin_add_tracked_start")],
        [InlineKeyboardButton("🗑️ Remove Main Channel", callback_data="admin_remove_tracked_list")]
    ]
    message_text = "🔗 *Main Forced Join Management*\n\nThese channels are required for general bot use."
    
    target_message = update.callback_query.message if update.callback_query else update.message
    if update.callback_query:
        await target_message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await target_message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def mailing_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    await update.message.reply_text("Please send the message you want to broadcast.", reply_markup=ReplyKeyboardRemove())
    return State.GET_MAIL_MESSAGE

async def get_mail_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['mail_message'] = update.message; context.user_data['buttons'] = []
    keyboard = [[InlineKeyboardButton("➕ Add URL Button", callback_data="mail_add_button"), InlineKeyboardButton("🚀 Send Now", callback_data="mail_send_now")]]
    await update.message.reply_text("Message received. Add a URL button or send now?", reply_markup=InlineKeyboardMarkup(keyboard))
    return State.AWAIT_BUTTON_OR_SEND

async def await_button_or_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query
    if len(context.user_data.get('buttons', [])) >= 3:
        await query.answer("Maximum of 3 buttons reached.", show_alert=True)
        return State.AWAIT_BUTTON_OR_SEND
    await query.edit_message_text("Please send button details in the format:\n`Button Text - https://your.link.com`")
    return State.GET_BUTTON_DATA

async def get_button_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        text, url = update.message.text.split(' - ', 1)
        context.user_data['buttons'].append(InlineKeyboardButton(text.strip(), url=url.strip()))
        num_buttons = len(context.user_data['buttons'])
        keyboard_options = [InlineKeyboardButton("🚀 Send Now", callback_data="mail_send_now")]
        if num_buttons < 3: keyboard_options.insert(0, InlineKeyboardButton("➕ Add Another Button", callback_data="mail_add_button"))
        await update.message.reply_text(f"Button added. You have {num_buttons}/3 buttons.", reply_markup=InlineKeyboardMarkup([keyboard_options]))
        return State.AWAIT_BUTTON_OR_SEND
    except ValueError:
        await update.message.reply_text("Invalid format. Use `Button Text - https://your.link.com`.")
        return State.GET_BUTTON_DATA

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query; await query.message.delete(); progress_msg = await query.message.reply_text("Broadcasting... Please wait.")
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        user_ids = c.execute("SELECT user_id FROM users").fetchall()
    message_to_send, buttons = context.user_data['mail_message'], context.user_data.get('buttons', [])
    reply_markup = InlineKeyboardMarkup([buttons]) if buttons else None; success, fail = 0, 0
    for user_id_tuple in user_ids:
        try: await message_to_send.copy(chat_id=user_id_tuple[0], reply_markup=reply_markup); success += 1
        except (Forbidden, BadRequest): fail += 1
    await progress_msg.edit_text(f"📢 Broadcast complete!\n✅ Sent: {success} | ❌ Failed: {fail}")
    await query.message.reply_text("Resuming Admin Mode.", reply_markup=get_admin_keyboard())
    context.user_data.clear(); return ConversationHandler.END

async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    await update.callback_query.message.delete()
    await update.callback_query.message.reply_text("Enter the display name for the task.", reply_markup=ReplyKeyboardRemove())
    return State.GET_TASK_NAME

async def get_task_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['task_name'] = update.message.text; await update.message.reply_text("Enter the Channel/Group ID (e.g., `@mychannel`)."); return State.GET_TARGET_CHAT_ID

async def get_target_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['target_chat_id'] = update.message.text; await update.message.reply_text("Enter the full public link (e.g., `https://t.me/mychannel`)."); return State.GET_TASK_URL

async def get_task_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['task_url'] = update.message.text; await update.message.reply_text("Enter the numerical reward (e.g., `0.10`)."); return State.GET_TASK_REWARD

async def get_task_reward_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        reward = float(update.message.text); task_data = context.user_data
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO tasks (task_name, reward, target_chat_id, task_url) VALUES (?, ?, ?, ?)", (task_data['task_name'], reward, task_data['target_chat_id'], task_data['task_url'])); conn.commit()
        await update.message.reply_text(f"✅ Task '{task_data['task_name']}' with reward ${reward:.2f} added.", reply_markup=get_admin_keyboard())
        context.user_data.clear(); await broadcast_new_task_notification(context); return ConversationHandler.END
    except ValueError: await update.message.reply_text("Invalid number. Please enter the reward again."); return State.GET_TASK_REWARD

async def broadcast_new_task_notification(context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        user_ids = c.execute("SELECT user_id FROM users WHERE user_id != ?", (ADMIN_ID,)).fetchall()
    for user_id_tuple in user_ids:
        try: await context.bot.send_message(chat_id=user_id_tuple[0], text="🔔 A new task is available! Click '📋 Tasks' to see it.")
        except (Forbidden, BadRequest): pass

async def delete_task_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        tasks = c.execute("SELECT task_id, task_name FROM tasks WHERE status = 'active'").fetchall()
    if not tasks: await query.edit_message_text("There are no active tasks to delete.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin_tasks")]])); return
    keyboard = [[InlineKeyboardButton(f"❌ {name}", callback_data=f"delete_task_{task_id}")] for task_id, name in tasks]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin_tasks")])
    await query.edit_message_text("Select a task to delete:", reply_markup=InlineKeyboardMarkup(keyboard))

async def export_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer("Generating file...")
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        user_ids = c.execute("SELECT user_id FROM users").fetchall()
    xml_content = "<users>\n" + "".join([f"  <user><id>{uid[0]}</id></user>\n" for uid in user_ids]) + "</users>"
    xml_file = io.BytesIO(xml_content.encode('utf-8')); xml_file.name = f"user_ids_{datetime.now().strftime('%Y-%m-%d')}.xml"
    await context.bot.send_document(chat_id=update.effective_chat.id, document=xml_file)

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    if not await is_member_or_send_join_message(update, context): return ConversationHandler.END
    user_id = update.effective_user.id
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        balance = c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
    if balance < MIN_WITHDRAWAL_LIMIT:
        await update.message.reply_text(f"❌ You need at least ${MIN_WITHDRAWAL_LIMIT:.2f} to withdraw. Your balance is ${balance:.2f}.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton("🔶 Binance (BEP20)", callback_data="w_net_BEP20"), InlineKeyboardButton("🔷 Binance (TRC20)", callback_data="w_net_TRC20")]];
    await update.message.reply_text("Please choose your withdrawal network:", reply_markup=InlineKeyboardMarkup(keyboard))
    return State.CHOOSE_WITHDRAW_NETWORK

async def choose_withdraw_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query; context.user_data['network'] = query.data.split("_")[2]; await query.answer()
    await query.edit_message_text(f"Selected **{context.user_data['network']}**. Please send your {context.user_data['network']} wallet address."); return State.GET_WALLET_ADDRESS

async def get_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['address'] = update.message.text; await update.message.reply_text("Address received. Now, please enter the amount to withdraw."); return State.GET_WITHDRAW_AMOUNT

async def get_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    user_id = update.effective_user.id
    try:
        amount = float(update.message.text)
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            c = conn.cursor()
            balance = c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
            if amount <= 0 or amount > balance:
                await update.message.reply_text(f"Invalid amount. You can withdraw between $0.01 and ${balance:.2f}."); return State.GET_WITHDRAW_AMOUNT
            network, address = context.user_data['network'], context.user_data['address']
            c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
            c.execute("INSERT INTO withdrawals (user_id, amount, network, wallet_address) VALUES (?, ?, ?, ?)", (user_id, amount, network, address)); withdrawal_id = c.lastrowid
            conn.commit()
        await update.message.reply_text("✅ Your withdrawal request has been submitted and is pending approval.")
        admin_message = f"🔔 *New Withdrawal Request* 🔔\nID: `{withdrawal_id}`\nUser: @{update.effective_user.username or 'N/A'}\nAmount: **${amount:.2f}** ({network})\nWallet: `{address}`"
        admin_keyboard = [[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{withdrawal_id}"), InlineKeyboardButton("❌ Reject", callback_data=f"reject_{withdrawal_id}")]]
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_message, reply_markup=InlineKeyboardMarkup(admin_keyboard), parse_mode='Markdown')
        return ConversationHandler.END
    except ValueError: await update.message.reply_text("That's not a valid number. Please enter the amount again."); return State.GET_WITHDRAW_AMOUNT

async def add_tracked_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    await update.callback_query.message.delete()
    await update.callback_query.message.reply_text("Enter the display name for the main channel (e.g., 'Main News').", reply_markup=ReplyKeyboardRemove())
    return State.GET_TRACKED_NAME

async def get_tracked_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['tracked_name'] = update.message.text; await update.message.reply_text("Enter the Channel/Group ID (e.g., `@mychannel`)."); return State.GET_TRACKED_ID

async def get_tracked_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['tracked_id'] = update.message.text; await update.message.reply_text("Enter the full public link (e.g., `https://t.me/mychannel`)."); return State.GET_TRACKED_URL

async def get_tracked_url_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    data = context.user_data
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO forced_channels (channel_name, channel_id, channel_url, status) VALUES (?, ?, ?, 'active')", (data['tracked_name'], data['tracked_id'], update.message.text));
            conn.commit()
            await update.message.reply_text(f"✅ Main channel '{data['tracked_name']}' is now being tracked.", reply_markup=get_admin_keyboard())
        except sqlite3.IntegrityError: await update.message.reply_text("❗️ This Channel ID is already being tracked.", reply_markup=get_admin_keyboard())
    context.user_data.clear(); return ConversationHandler.END

async def remove_tracked_channel_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        channels = c.execute("SELECT id, channel_name FROM forced_channels WHERE status = 'active'").fetchall()
    if not channels:
        await query.edit_message_text("There are no main channels to remove.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin_tracking")]])); return
    keyboard = [[InlineKeyboardButton(f"❌ {name}", callback_data=f"delete_tracked_{ch_id}")] for ch_id, name in channels]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin_tracking")])
    await query.edit_message_text("Select a main channel to stop tracking:", reply_markup=InlineKeyboardMarkup(keyboard))

# --- COUPON FUNCTIONS ---
async def generate_coupon_message_text(context: ContextTypes.DEFAULT_TYPE, coupon_code: str, budget: float, max_claims: int, claims_count: int) -> str:
    bot_username = (await context.bot.get_me()).username
    status = "✅ Status: Active" if claims_count < max_claims else "❌ Status: Expired"
    return (f"🎁 **Today Coupon Code** 🎁\n\n"
            f"**Code** : `{coupon_code}`\n"
            f"**Total Budget** : ${budget:.2f}\n"
            f"**Max Claims** : {max_claims}\n"
            f"**Total Claim** : {claims_count} / {max_claims}\n"
            f"{status}\n\n"
            f"➡️ Get your reward at: @{bot_username}")

async def handle_coupon_management(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    keyboard = [
        [InlineKeyboardButton("➕ Create Coupon", callback_data="admin_create_coupon_start")],
        [InlineKeyboardButton("📜 Coupon History", callback_data="admin_coupon_history")],
        [InlineKeyboardButton("➕ Add Tracked Channel (Coupon)", callback_data="admin_add_coupon_tracked_start")],
        [InlineKeyboardButton("🗑️ Remove Tracked Channel (Coupon)", callback_data="admin_remove_coupon_tracked_list")]]
    
    message_text = "🎟️ *Coupon Management*\n\nThese channels are required only for claiming coupons."
    
    target_message = update.callback_query.message if update.callback_query else update.message
    if update.callback_query:
        await target_message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await target_message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def handle_coupon_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        coupons = c.execute("SELECT coupon_code, budget, max_claims, claims_count, status FROM coupons ORDER BY creation_date DESC").fetchall()
    if not coupons: await query.edit_message_text("No coupons have been created yet.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_coupon_menu")]])); return
    response = "📜 **Coupon History**\n\n"
    for code, budget, max_c, claims_c, status in coupons:
        response += f"Code: `{code}`\nBudget: ${budget:.2f} | Claims: {claims_c}/{max_c} | Status: {status.title()}\n---------------------\n"
    await query.edit_message_text(response, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_coupon_menu")]]))

async def create_coupon_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    await update.callback_query.message.delete()
    await update.callback_query.message.reply_text("Enter the total budget for this coupon (e.g., `100`).", reply_markup=ReplyKeyboardRemove())
    return State.GET_COUPON_BUDGET

async def get_coupon_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        budget = float(update.message.text)
        if budget <= 0: raise ValueError("Budget must be positive.")
        context.user_data['coupon_budget'] = budget
        await update.message.reply_text(f"Budget set to ${budget:.2f}. Now, enter the maximum number of users who can claim this coupon (e.g., `50`).")
        return State.GET_COUPON_MAX_CLAIMS
    except ValueError:
        await update.message.reply_text("Invalid number. Please enter a valid budget amount (e.g., `100`).")
        return State.GET_COUPON_BUDGET

async def get_coupon_max_claims_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        max_claims = int(update.message.text)
        if max_claims <= 0: raise ValueError("Max claims must be positive.")
    except ValueError:
        await update.message.reply_text("Invalid number. Please enter a valid whole number for max claims (e.g., `50`).")
        return State.GET_COUPON_MAX_CLAIMS

    budget = context.user_data['coupon_budget']; coupon_code = ""
    try:
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            c = conn.cursor()
            while True:
                coupon_code = f"C-{random.randint(10000000, 99999999)}"
                if not c.execute("SELECT 1 FROM coupons WHERE coupon_code = ?", (coupon_code,)).fetchone(): break
            c.execute("INSERT INTO coupons (coupon_code, budget, max_claims) VALUES (?, ?, ?)", (coupon_code, budget, max_claims)); conn.commit()
        await update.message.reply_text(f"✅ Coupon `{coupon_code}` created successfully!\n\nNow broadcasting to channels...",parse_mode='Markdown',reply_markup=get_admin_keyboard())
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            c = conn.cursor()
            tracked_channels = c.execute("SELECT channel_id FROM coupon_forced_channels WHERE status = 'active'").fetchall()
            if not tracked_channels:
                await context.bot.send_message(chat_id=ADMIN_ID, text="⚠️ Note: No tracked coupon channels are set up. The coupon was created but not broadcasted.")
            else:
                message_text = await generate_coupon_message_text(context, coupon_code, budget, max_claims, 0)
                sent_count, failed_count = 0, 0; messages_to_save = []
                for (channel_id,) in tracked_channels:
                    try:
                        sent_message = await context.bot.send_message(chat_id=channel_id, text=message_text, parse_mode='Markdown')
                        messages_to_save.append((coupon_code, sent_message.chat_id, sent_message.message_id)); sent_count += 1
                    except (BadRequest, Forbidden) as e:
                        logger.error(f"Failed to send coupon to {channel_id}: {e}"); failed_count += 1
                if messages_to_save:
                    c.executemany("INSERT OR IGNORE INTO coupon_messages (coupon_code, chat_id, message_id) VALUES (?, ?, ?)", messages_to_save); conn.commit()
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"📢 Broadcast complete!\n✅ Sent to: {sent_count} channels | ❌ Failed for: {failed_count} channels.")
    except Exception as e:
        logger.error(f"Error during coupon creation: {e}")
        await update.message.reply_text(f"❌ An error occurred.", reply_markup=get_admin_keyboard())
    context.user_data.clear(); return ConversationHandler.END

async def add_coupon_tracked_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    await update.callback_query.message.delete()
    await update.callback_query.message.reply_text("Enter the display name for the coupon channel (e.g., 'Coupon Drops').", reply_markup=ReplyKeyboardRemove())
    return State.GET_COUPON_TRACKED_NAME

async def get_coupon_tracked_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['coupon_tracked_name'] = update.message.text; await update.message.reply_text("Enter the Channel ID (e.g., `@mychannel`)."); return State.GET_COUPON_TRACKED_ID

async def get_coupon_tracked_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['coupon_tracked_id'] = update.message.text; await update.message.reply_text("Enter the full public link (e.g., `https://t.me/mychannel`)."); return State.GET_COUPON_TRACKED_URL

async def get_coupon_tracked_url_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    data = context.user_data
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO coupon_forced_channels (channel_name, channel_id, channel_url, status) VALUES (?, ?, ?, 'active')", (data['coupon_tracked_name'], data['coupon_tracked_id'], update.message.text));
            conn.commit()
            await update.message.reply_text(f"✅ Coupon channel '{data['coupon_tracked_name']}' is now being tracked.", reply_markup=get_admin_keyboard())
        except sqlite3.IntegrityError: await update.message.reply_text("❗️ This Channel ID is already being tracked for coupons.", reply_markup=get_admin_keyboard())
    context.user_data.clear(); return ConversationHandler.END

async def remove_coupon_tracked_channel_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        channels = c.execute("SELECT id, channel_name FROM coupon_forced_channels WHERE status = 'active'").fetchall()
    if not channels: await query.edit_message_text("There are no coupon channels to remove.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_coupon_menu")]])); return
    keyboard = [[InlineKeyboardButton(f"❌ {name}", callback_data=f"delete_coupon_tracked_{ch_id}")] for ch_id, name in channels]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_coupon_menu")])
    await query.edit_message_text("Select a coupon channel to stop tracking:", reply_markup=InlineKeyboardMarkup(keyboard))

async def claim_coupon_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    if not await is_member_or_send_join_message(update, context): return ConversationHandler.END
    result = await check_membership_and_grant_access(update, context, 'verify_coupon_membership', 'coupon_forced_channels')
    if result in ['CONTINUE', 'PROCEED_TO_CODE']: return State.AWAIT_COUPON_CODE
    return ConversationHandler.END

async def prompt_for_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    message = update.message or update.callback_query.message
    await message.reply_text("✅ Membership verified! Please send me the coupon code to claim your reward.")
    return State.AWAIT_COUPON_CODE

async def receive_coupon_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    user_id = update.effective_user.id
    code = update.message.text.strip().upper()
    
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        coupon_data = c.execute("SELECT budget, max_claims, claims_count, status FROM coupons WHERE coupon_code = ?", (code,)).fetchone()
        if not coupon_data:
            await update.message.reply_text("❌ Invalid code."); return State.AWAIT_COUPON_CODE

        budget, max_claims, claims_count, status = coupon_data
        if c.execute("SELECT 1 FROM claimed_coupons WHERE user_id = ? AND coupon_code = ?", (user_id, code)).fetchone():
            await update.message.reply_text("⚠️ Already claimed."); return ConversationHandler.END

        if status != 'active' or claims_count >= max_claims:
            await update.message.reply_text("😥 Expired."); return ConversationHandler.END
        
        total_weight = max_claims * (max_claims + 1) / 2
        user_weight = max_claims - claims_count
        reward = (user_weight / total_weight) * budget
        
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, user_id))
        c.execute("INSERT INTO claimed_coupons (user_id, coupon_code) VALUES (?, ?)", (user_id, code))
        c.execute("UPDATE coupons SET claims_count = claims_count + 1 WHERE coupon_code = ?", (code,)); conn.commit()
        
        claims_count += 1
        messages_to_update = c.execute("SELECT chat_id, message_id FROM coupon_messages WHERE coupon_code = ?", (code,)).fetchall()
        await update.message.reply_text(f"✅ Claimed ${reward:.2f}!", parse_mode='Markdown')

    if messages_to_update:
        new_text = await generate_coupon_message_text(context, code, budget, max_claims, claims_count)
        for chat_id, message_id in messages_to_update:
            try: await context.bot.edit_message_text(text=new_text, chat_id=chat_id, message_id=message_id, parse_mode='Markdown')
            except: pass
    return ConversationHandler.END

# --- GENERIC HANDLERS ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer(); data = query.data; user_id = query.from_user.id
    if data == "verify_membership": await check_membership_and_grant_access(update, context, 'verify_membership', 'forced_channels')
    elif data == "clear_join_message": await query.message.delete()
    elif data.startswith("verify_join_"):
        task_id = int(data.split("_")[2])
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            c = conn.cursor()
            task_info = c.execute("SELECT reward, target_chat_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task_info: await query.edit_message_text("Unavailable."); return
            try:
                member = await context.bot.get_chat_member(chat_id=task_info[1], user_id=user_id)
                if member.status in ['member', 'administrator', 'creator']:
                    c.execute("INSERT OR IGNORE INTO completed_tasks (user_id, task_id) VALUES (?, ?)", (user_id, task_id))
                    if c.rowcount > 0:
                        reward = task_info[0]; c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, user_id)); conn.commit()
                        await query.edit_message_text(f"✅ Earned ${reward:.2f}"); await display_next_task(update, context)
                    else: await query.edit_message_text("Already completed.")
                else: await query.answer("⚠️ Not joined yet.", show_alert=True)
            except BadRequest: await query.answer("❗️ Admin error.", show_alert=True)
    elif data.startswith("approve_") or data.startswith("reject_"):
        action, withdrawal_id = data.split("_")
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            c = conn.cursor()
            w_user_id, amount = c.execute("SELECT user_id, amount FROM withdrawals WHERE withdrawal_id = ?", (withdrawal_id,)).fetchone()
            if action == "approve":
                c.execute("UPDATE withdrawals SET status = 'approved' WHERE withdrawal_id = ?", (withdrawal_id,)); conn.commit()
                await context.bot.send_message(chat_id=w_user_id, text=f"🎉 Approved ${amount:.2f}!"); await query.answer("Approved!")
            else:
                c.execute("UPDATE withdrawals SET status = 'rejected' WHERE withdrawal_id = ?", (withdrawal_id,)); c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, w_user_id)); conn.commit()
                await context.bot.send_message(chat_id=w_user_id, text=f"😔 Rejected. Funds returned."); await query.answer("Rejected!")
        await query.message.delete()
    elif data.startswith("delete_task_"):
        task_id = int(data.split("_")[2])
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn: c = conn.cursor(); c.execute("UPDATE tasks SET status = 'deleted' WHERE task_id = ?", (task_id,)); conn.commit()
        await delete_task_list(update, context)
    elif data.startswith("delete_tracked_"):
        ch_id = int(data.split("_")[2])
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn: c = conn.cursor(); c.execute("UPDATE forced_channels SET status = 'deleted' WHERE id = ?", (ch_id,)); conn.commit()
        await remove_tracked_channel_list(update, context)
    elif data.startswith("delete_coupon_tracked_"):
        ch_id = int(data.split("_")[3])
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn: c = conn.cursor(); c.execute("UPDATE coupon_forced_channels SET status = 'deleted' WHERE id = ?", (ch_id,)); conn.commit()
        await remove_coupon_tracked_channel_list(update, context)
    elif data == "admin_export_users": await export_users(update, context)
    elif data == "back_to_admin_tasks": await handle_admin_tasks(update, context)
    elif data == "back_to_admin_tracking": await handle_admin_tracking(update, context)
    elif data == "back_to_coupon_menu": await handle_coupon_management(update, context)
    elif data == "admin_coupon_history": await handle_coupon_history(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    keyboard = get_user_keyboard(user_id) if user_id != ADMIN_ID else get_admin_keyboard()
    await update.effective_message.reply_text("Action canceled.", reply_markup=keyboard)
    context.user_data.clear()
    return ConversationHandler.END

def main() -> None:
    setup_database()
    
    # Start Health Check Server in a separate thread
    threading.Thread(target=run_health_check, daemon=True).start()
    logger.info(f"Health check server started on port {PORT}")

    application = Application.builder().token(BOT_API_KEY).build()
    
    user_menu_buttons = ["💰 Balance", "👥 Referral", "🎁 Daily Bonus", "📋 Tasks", "💸 Withdraw", "🎟️ Coupon Code", "👑 Admin Panel"]
    admin_menu_buttons = ["📧 Mailing", "📋 Task Management", "🎟️ Coupon Management", "📊 Bot Stats", "🏧 Withdrawals", "🔗 Main Track Management", "⬅️ Back to User Menu"]
    any_menu_button_filter = filters.Regex(f"^({'|'.join(user_menu_buttons + admin_menu_buttons)})$")
    non_menu_text_filter = filters.TEXT & ~filters.COMMAND & ~any_menu_button_filter

    async def menu_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text("Previous action canceled.")
        text = update.message.text
        if text == "💰 Balance": await handle_balance(update, context)
        elif text == "👥 Referral": await handle_referral(update, context)
        elif text == "🎁 Daily Bonus": await handle_daily_bonus(update, context)
        elif text == "📋 Tasks": await handle_tasks(update, context)
        elif text == "💸 Withdraw": return await withdraw_start(update, context)
        elif text == "🎟️ Coupon Code": return await claim_coupon_start(update, context)
        elif text == "👑 Admin Panel": await admin_panel_start(update, context)
        elif text == "📧 Mailing": return await mailing_start(update, context)
        elif text == "📋 Task Management": await handle_admin_tasks(update, context)
        elif text == "🎟️ Coupon Management": await handle_coupon_management(update, context)
        elif text == "📊 Bot Stats": await handle_admin_stats(update, context)
        elif text == "🏧 Withdrawals": await handle_admin_withdrawals(update, context)
        elif text == "🔗 Main Track Management": await handle_admin_tracking(update, context)
        elif text == "⬅️ Back to User Menu": await admin_back_to_user_menu(update, context)
        return ConversationHandler.END

    conv_fallbacks = [CommandHandler("cancel", cancel), MessageHandler(any_menu_button_filter, menu_interrupt)]

    add_task_conv = ConversationHandler(entry_points=[CallbackQueryHandler(add_task_start, pattern="^admin_add_task_start$")], states={State.GET_TASK_NAME: [MessageHandler(non_menu_text_filter, get_task_name)], State.GET_TARGET_CHAT_ID: [MessageHandler(non_menu_text_filter, get_target_chat_id)], State.GET_TASK_URL: [MessageHandler(non_menu_text_filter, get_task_url)], State.GET_TASK_REWARD: [MessageHandler(non_menu_text_filter, get_task_reward_and_save)]}, fallbacks=conv_fallbacks, per_message=False)
    mailing_conv = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^📧 Mailing$"), mailing_start)], states={State.GET_MAIL_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND & ~any_menu_button_filter, get_mail_message)], State.AWAIT_BUTTON_OR_SEND: [CallbackQueryHandler(await_button_or_send, pattern="^mail_add_button$"), CallbackQueryHandler(broadcast_message, pattern="^mail_send_now$")], State.GET_BUTTON_DATA: [MessageHandler(non_menu_text_filter, get_button_data)]}, fallbacks=conv_fallbacks, per_message=False)
    add_tracked_conv = ConversationHandler(entry_points=[CallbackQueryHandler(add_tracked_channel_start, pattern="^admin_add_tracked_start$")], states={State.GET_TRACKED_NAME: [MessageHandler(non_menu_text_filter, get_tracked_name)], State.GET_TRACKED_ID: [MessageHandler(non_menu_text_filter, get_tracked_id)], State.GET_TRACKED_URL: [MessageHandler(non_menu_text_filter, get_tracked_url_and_save)]}, fallbacks=conv_fallbacks, per_message=False)
    create_coupon_conv = ConversationHandler(entry_points=[CallbackQueryHandler(create_coupon_start, pattern="^admin_create_coupon_start$")], states={State.GET_COUPON_BUDGET: [MessageHandler(non_menu_text_filter, get_coupon_budget)], State.GET_COUPON_MAX_CLAIMS: [MessageHandler(non_menu_text_filter, get_coupon_max_claims_and_save)]}, fallbacks=conv_fallbacks, per_message=False)
    add_coupon_tracked_conv = ConversationHandler(entry_points=[CallbackQueryHandler(add_coupon_tracked_channel_start, pattern="^admin_add_coupon_tracked_start$")], states={State.GET_COUPON_TRACKED_NAME: [MessageHandler(non_menu_text_filter, get_coupon_tracked_name)], State.GET_COUPON_TRACKED_ID: [MessageHandler(non_menu_text_filter, get_coupon_tracked_id)], State.GET_COUPON_TRACKED_URL: [MessageHandler(non_menu_text_filter, get_coupon_tracked_url_and_save)]}, fallbacks=conv_fallbacks, per_message=False)
    withdraw_conv = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_start)], states={State.CHOOSE_WITHDRAW_NETWORK: [CallbackQueryHandler(choose_withdraw_network, pattern="^w_net_")], State.GET_WALLET_ADDRESS: [MessageHandler(non_menu_text_filter, get_wallet_address)], State.GET_WITHDRAW_AMOUNT: [MessageHandler(non_menu_text_filter, get_withdraw_amount)]}, fallbacks=conv_fallbacks, per_message=False)
    claim_coupon_conv = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^🎟️ Coupon Code$"), claim_coupon_start)], states={State.AWAIT_COUPON_CODE: [MessageHandler(non_menu_text_filter, receive_coupon_code), CallbackQueryHandler(claim_coupon_start, pattern="^verify_coupon_membership$")]}, fallbacks=conv_fallbacks, per_message=False)
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~any_menu_button_filter, gatekeeper_handler), group=-1)
    application.add_handler(add_task_conv); application.add_handler(withdraw_conv); application.add_handler(mailing_conv); application.add_handler(add_tracked_conv)
    application.add_handler(create_coupon_conv); application.add_handler(add_coupon_tracked_conv); application.add_handler(claim_coupon_conv)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), handle_balance))
    application.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), handle_referral))
    application.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), handle_daily_bonus))
    application.add_handler(MessageHandler(filters.Regex("^📋 Tasks$"), handle_tasks))
    application.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), admin_panel_start))
    application.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), admin_back_to_user_menu))
    application.add_handler(MessageHandler(filters.Regex("^📋 Task Management$"), handle_admin_tasks))
    application.add_handler(MessageHandler(filters.Regex("^📊 Bot Stats$"), handle_admin_stats))
    application.add_handler(MessageHandler(filters.Regex("^🏧 Withdrawals$"), handle_admin_withdrawals))
    application.add_handler(MessageHandler(filters.Regex("^🔗 Main Track Management$"), handle_admin_tracking))
    application.add_handler(MessageHandler(filters.Regex("^🎟️ Coupon Management$"), handle_coupon_management))
    application.add_handler(CallbackQueryHandler(delete_task_list, pattern="^admin_delete_task_list$"))
    application.add_handler(CallbackQueryHandler(remove_tracked_channel_list, pattern="^admin_remove_tracked_list$"))
    application.add_handler(CallbackQueryHandler(remove_coupon_tracked_channel_list, pattern="^admin_remove_coupon_tracked_list$"))
    application.add_handler(CallbackQueryHandler(button_callback_handler))

    application.run_polling()

if __name__ == "__main__":
    main()

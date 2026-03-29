import logging
import sqlite3
import io
import os
import random
import threading
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, date, timedelta
from enum import Enum

from telegram import (
    ReplyKeyboardMarkup, Update, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.error import BadRequest, Forbidden, NetworkError

# --- Choreo Persistence & Configuration ---
# Choreo usually requires a persistent volume. If 'data' isn't available, we use '/tmp' to avoid crashes.
DATA_DIR = os.getenv("DATA_DIR", "data")
try:
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = "/tmp" 

DB_FILE = os.path.join(DATA_DIR, "user_data.db")

BOT_API_KEY = os.getenv("BOT_API_KEY")
ADMIN_ID_ENV = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV else 0
PORT = int(os.getenv("PORT", 8080)) 

REFERRAL_BONUS = 0.05
DAILY_BONUS = 0.05
MIN_WITHDRAWAL_LIMIT = 5.00

# --- Health Check Server for Choreo ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def log_message(self, format, *args):
        return 

def run_health_check():
    try:
        server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
        logger.info(f"✅ Health Check Server started on port {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"❌ Health Check Server failed: {e}")

# --- Setup Logging & States ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

class State(Enum):
    GET_TASK_NAME = 1; GET_TARGET_CHAT_ID = 2; GET_TASK_URL = 3; GET_TASK_REWARD = 4
    CHOOSE_WITHDRAW_NETWORK = 5; GET_WALLET_ADDRESS = 6; GET_WITHDRAW_AMOUNT = 7
    GET_MAIL_MESSAGE = 8; AWAIT_BUTTON_OR_SEND = 9; GET_BUTTON_DATA = 10
    GET_TRACKED_NAME = 11; GET_TRACKED_ID = 12; GET_TRACKED_URL = 13
    GET_COUPON_BUDGET = 14; GET_COUPON_MAX_CLAIMS = 15
    AWAIT_COUPON_CODE = 16
    GET_COUPON_TRACKED_NAME = 17; GET_COUPON_TRACKED_ID = 18; GET_COUPON_TRACKED_URL = 19
    SET_PROOF_CHANNEL = 20

# --- Database Setup ---
def setup_database():
    try:
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, balance REAL DEFAULT 0, last_bonus_claim DATE, referred_by INTEGER, referral_count INTEGER DEFAULT 0)")
            try:
                c.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
            except sqlite3.OperationalError:
                pass
            c.execute("CREATE TABLE IF NOT EXISTS tasks (task_id INTEGER PRIMARY KEY AUTOINCREMENT, task_name TEXT NOT NULL, reward REAL NOT NULL, target_chat_id TEXT NOT NULL, task_url TEXT NOT NULL, status TEXT DEFAULT 'active')")
            c.execute("CREATE TABLE IF NOT EXISTS completed_tasks (user_id INTEGER, task_id INTEGER, PRIMARY KEY (user_id, task_id))")
            c.execute("CREATE TABLE IF NOT EXISTS withdrawals (withdrawal_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount REAL NOT NULL, network TEXT NOT NULL, wallet_address TEXT NOT NULL, status TEXT DEFAULT 'pending', request_date DATETIME DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (user_id) REFERENCES users (user_id))")
            c.execute("CREATE TABLE IF NOT EXISTS forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')")
            c.execute("CREATE TABLE IF NOT EXISTS coupons (coupon_code TEXT PRIMARY KEY, budget REAL NOT NULL, max_claims INTEGER NOT NULL, claims_count INTEGER DEFAULT 0, status TEXT DEFAULT 'active', creation_date DATETIME DEFAULT CURRENT_TIMESTAMP)")
            c.execute("CREATE TABLE IF NOT EXISTS claimed_coupons (user_id INTEGER, coupon_code TEXT, PRIMARY KEY (user_id, coupon_code))")
            c.execute("CREATE TABLE IF NOT EXISTS coupon_forced_channels (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_name TEXT, channel_id TEXT UNIQUE, channel_url TEXT, status TEXT DEFAULT 'active')")
            c.execute("CREATE TABLE IF NOT EXISTS coupon_messages (coupon_code TEXT, chat_id INTEGER, message_id INTEGER, PRIMARY KEY (coupon_code, chat_id))")
            c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            conn.commit()
            logger.info("✅ Database Setup Complete")
    except Exception as e:
        logger.error(f"❌ Database Error: {e}")

# --- Keyboard Definitions ---
def get_user_keyboard(user_id):
    user_buttons = [[KeyboardButton("💰 Balance"), KeyboardButton("👥 Referral")], [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📋 Tasks")], [KeyboardButton("💸 Withdraw"), KeyboardButton("🎟️ Coupon Code")]]
    if user_id == ADMIN_ID: user_buttons.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(user_buttons, resize_keyboard=True)

def get_admin_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📧 Mailing"), KeyboardButton("📋 Task Management")],
        [KeyboardButton("🎟️ Coupon Management"), KeyboardButton("📊 Bot Stats")],
        [KeyboardButton("🏧 Withdrawals"), KeyboardButton("🔗 Main Track Management")],
        [KeyboardButton("📢 Proof Channel"), KeyboardButton("⬅️ Back to User Menu")],
    ], resize_keyboard=True)

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
        except (BadRequest, Forbidden): continue
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
        except (ValueError, IndexError): pass
    await check_membership_and_grant_access(update, context, 'verify_membership', 'forced_channels')

async def check_membership_and_grant_access(update: Update, context: ContextTypes.DEFAULT_TYPE, verify_callback: str, table_name: str):
    user = update.effective_user
    if not user and update.callback_query: user = update.callback_query.from_user
    unjoined = await get_unjoined_channels(user.id, context, table_name)
    if unjoined:
        message_text = "⚠️ **To proceed, you must join the following channel(s):**"
        keyboard = [[InlineKeyboardButton(f"➡️ Join {channel['name']}", url=channel['url'])] for channel in unjoined]
        keyboard.append([InlineKeyboardButton("✅ I Have Joined", callback_data=verify_callback)])
        target = update.callback_query.message if update.callback_query else update.effective_message
        if update.callback_query: await update.callback_query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        else: await target.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return 'CONTINUE'

    if update.callback_query: await update.callback_query.message.delete()
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        is_new = c.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,)).fetchone() is None
        if verify_callback != 'verify_coupon_membership':
            welcome_message = f"✅ Thank you for joining!\n\n👋 Welcome, {user.first_name}!";
            if "from_admin_back" in context.user_data: welcome_message = "⬅️ Switched back to User Mode."; del context.user_data["from_admin_back"]
            ref_id = context.user_data.get('referrer_id')
            if is_new and ref_id:
                if c.execute("SELECT user_id FROM users WHERE user_id = ?", (ref_id,)).fetchone():
                    c.execute("INSERT INTO users (user_id, username, first_name, balance, referred_by) VALUES (?, ?, ?, ?, ?)", (user.id, user.username, user.first_name, REFERRAL_BONUS, ref_id))
                    c.execute("UPDATE users SET balance = balance + ?, referral_count = referral_count + 1 WHERE user_id = ?", (REFERRAL_BONUS, ref_id))
                    conn.commit()
                    welcome_message = f"🎉 Welcome {user.first_name}!\nYou joined via referral bonus of **${REFERRAL_BONUS:.2f}**!"
                    try: await context.bot.send_message(chat_id=ref_id, text=f"✅ User *{user.first_name}* joined using your link.\nYou earned **${REFERRAL_BONUS:.2f}**!", parse_mode='Markdown')
                    except: pass
                else: c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)", (user.id, user.username, user.first_name))
            else:
                c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)", (user.id, user.username, user.first_name))
                c.execute("UPDATE users SET first_name = ? WHERE user_id = ?", (user.first_name, user.id))
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
        balance = conn.cursor().execute("SELECT balance FROM users WHERE user_id = ?", (update.effective_user.id,)).fetchone()[0]
    await update.message.reply_text(f"💰 Balance: **${balance:.2f}**.", parse_mode='Markdown')

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_member_or_send_join_message(update, context): return
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)", (uid, update.effective_user.username, update.effective_user.first_name))
        conn.commit()
        ref_count = c.execute("SELECT referral_count FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
    bot_un = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_un}?start={uid}"
    await update.message.reply_text(f"🚀 Invite friends and earn **${REFERRAL_BONUS:.2f}**!\n\n`{link}`\n\n👥 Referrals: **{ref_count}**", parse_mode='Markdown')

async def handle_daily_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_member_or_send_join_message(update, context): return
    uid = update.effective_user.id
    today = date.today()
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        last = c.execute("SELECT last_bonus_claim FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
        if last and date.fromisoformat(last) >= today:
            await update.message.reply_text("Claimed already today!")
        else:
            c.execute("UPDATE users SET balance = balance + ?, last_bonus_claim = ? WHERE user_id = ?", (DAILY_BONUS, today.isoformat(), uid)); conn.commit()
            await update.message.reply_text(f"🎉 Received ${DAILY_BONUS:.2f} bonus!", parse_mode='Markdown')

async def display_next_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        task = conn.cursor().execute("SELECT task_id, task_name, reward, task_url FROM tasks WHERE status = 'active' AND task_id NOT IN (SELECT task_id FROM completed_tasks WHERE user_id = ?) LIMIT 1", (uid,)).fetchone()
    if not task:
        await update.effective_message.reply_text("🎉 No more tasks!")
        return
    tid, name, rew, url = task
    kb = [[InlineKeyboardButton("➡️ Go to Channel", url=url), InlineKeyboardButton("✅ I Have Joined", callback_data=f"verify_join_{tid}")]]
    await update.effective_message.reply_text(f"**{name}**\nReward: **${rew:.2f}**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def handle_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_member_or_send_join_message(update, context): return
    await display_next_task(update, context)

async def admin_panel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("👑 Admin Mode.", reply_markup=get_admin_keyboard())

async def admin_back_to_user_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    context.user_data["from_admin_back"] = True
    await start(update, context)

# --- PROOF CHANNEL ---
async def handle_admin_proof_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        cur = conn.cursor().execute("SELECT value FROM settings WHERE key = 'proof_channel_id'").fetchone()
    msg = f"📢 *Proof Channel*\nCurrent: `{cur[0] if cur else 'Not Set'}`"
    kb = [[InlineKeyboardButton("🛠 Set Proof Channel", callback_data="admin_set_proof_start")]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def set_proof_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    await update.callback_query.message.delete()
    await update.callback_query.message.reply_text("Enter Proof Channel ID (e.g. `@MyProofs`):", reply_markup=ReplyKeyboardRemove())
    return State.SET_PROOF_CHANNEL

async def save_proof_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    cid = update.message.text.strip()
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ('proof_channel_id', cid))
        conn.commit()
    await update.message.reply_text(f"✅ Set to: `{cid}`", reply_markup=get_admin_keyboard())
    return ConversationHandler.END

async def handle_admin_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    kb = [[InlineKeyboardButton("➕ Add Task", callback_data="admin_add_task_start")], [InlineKeyboardButton("🗑️ Delete Task", callback_data="admin_delete_task_list")]]
    await update.message.reply_text("📋 *Task Management*", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def handle_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        total = conn.cursor().execute("SELECT COUNT(user_id) FROM users").fetchone()[0]
    kb = [[InlineKeyboardButton("📥 Export IDs (.xml)", callback_data="admin_export_users")]]
    await update.message.reply_text(f"📊 Stats\nUsers: **{total}**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def handle_admin_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        ws = conn.cursor().execute("SELECT w.withdrawal_id, u.username, w.amount, w.network, w.wallet_address FROM withdrawals w JOIN users u ON w.user_id = u.user_id WHERE w.status = 'pending'").fetchall()
    if not ws: await update.message.reply_text("🏧 No pending requests."); return
    for wid, un, amt, net, addr in ws:
        msg = f"ID: `{wid}` | @{un or 'N/A'}\nAmt: **${amt:.2f}** ({net})\nAddr: `{addr}`"
        kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wid}"), InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wid}")]]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def handle_admin_tracking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    kb = [[InlineKeyboardButton("➕ Add Channel", callback_data="admin_add_tracked_start")], [InlineKeyboardButton("🗑️ Remove Channel", callback_data="admin_remove_tracked_list")]]
    await update.message.reply_text("🔗 *Forced Join*", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

# --- MAILING ---
async def mailing_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    if update.effective_user.id != ADMIN_ID: return ConversationHandler.END
    await update.message.reply_text("Send message to broadcast:", reply_markup=ReplyKeyboardRemove()); return State.GET_MAIL_MESSAGE

async def get_mail_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['mail_message'] = update.message; context.user_data['buttons'] = []
    kb = [[InlineKeyboardButton("➕ Add Button", callback_data="mail_add_button"), InlineKeyboardButton("🚀 Send", callback_data="mail_send_now")]]
    await update.message.reply_text("Received.", reply_markup=InlineKeyboardMarkup(kb)); return State.AWAIT_BUTTON_OR_SEND

async def await_button_or_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    if len(context.user_data.get('buttons', [])) >= 3: await update.callback_query.answer("Max 3."); return State.AWAIT_BUTTON_OR_SEND
    await update.callback_query.edit_message_text("Format: `Text - https://link.com`."); return State.GET_BUTTON_DATA

async def get_button_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        txt, url = update.message.text.split(' - ', 1)
        context.user_data['buttons'].append(InlineKeyboardButton(txt.strip(), url=url.strip()))
        kb = [InlineKeyboardButton("🚀 Send Now", callback_data="mail_send_now")]
        if len(context.user_data['buttons']) < 3: kb.insert(0, InlineKeyboardButton("➕ Add Another", callback_data="mail_add_button"))
        await update.message.reply_text("Button added.", reply_markup=InlineKeyboardMarkup([kb])); return State.AWAIT_BUTTON_OR_SEND
    except: await update.message.reply_text("Invalid."); return State.GET_BUTTON_DATA

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query; await query.message.delete(); prog = await query.message.reply_text("Broadcasting...")
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        uids = conn.cursor().execute("SELECT user_id FROM users").fetchall()
    msg, btns = context.user_data['mail_message'], context.user_data.get('buttons', [])
    markup = InlineKeyboardMarkup([btns]) if btns else None; s, f = 0, 0
    for uid in uids:
        try: await msg.copy(chat_id=uid[0], reply_markup=markup); s += 1
        except: f += 1
    await prog.edit_text(f"📢 Done! ✅ {s} | ❌ {f}"); await query.message.reply_text("Admin Menu", reply_markup=get_admin_keyboard())
    context.user_data.clear(); return ConversationHandler.END

# --- TASK MANAGEMENT ---
async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    await update.callback_query.message.delete(); await update.callback_query.message.reply_text("Task name?", reply_markup=ReplyKeyboardRemove()); return State.GET_TASK_NAME
async def get_task_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['task_name'] = update.message.text; await update.message.reply_text("ID?"); return State.GET_TARGET_CHAT_ID
async def get_target_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['target_chat_id'] = update.message.text; await update.message.reply_text("Link?"); return State.GET_TASK_URL
async def get_task_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['task_url'] = update.message.text; await update.message.reply_text("Reward?"); return State.GET_TASK_REWARD
async def get_task_reward_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        rew = float(update.message.text); data = context.user_data
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            conn.cursor().execute("INSERT INTO tasks (task_name, reward, target_chat_id, task_url) VALUES (?, ?, ?, ?)", (data['task_name'], rew, data['target_chat_id'], data['task_url'])); conn.commit()
        await update.message.reply_text("✅ Added.", reply_markup=get_admin_keyboard()); context.user_data.clear(); return ConversationHandler.END
    except: await update.message.reply_text("Error."); return State.GET_TASK_REWARD

async def delete_task_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        tasks = conn.cursor().execute("SELECT task_id, task_name FROM tasks WHERE status = 'active'").fetchall()
    if not tasks: await query.edit_message_text("No tasks.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin_tasks")]])); return
    kb = [[InlineKeyboardButton(f"❌ {n}", callback_data=f"delete_task_{tid}")] for tid, n in tasks]
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin_tasks")])
    await query.edit_message_text("Select to delete:", reply_markup=InlineKeyboardMarkup(kb))

async def export_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer("Generating...")
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        uids = conn.cursor().execute("SELECT user_id FROM users").fetchall()
    content = "<users>\n" + "".join([f"  <id>{u[0]}</id>\n" for u in uids]) + "</users>"
    bio = io.BytesIO(content.encode()); bio.name = "users.xml"
    await context.bot.send_document(chat_id=update.effective_chat.id, document=bio)

# --- WITHDRAW ---
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    if not await is_member_or_send_join_message(update, context): return ConversationHandler.END
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        bal = conn.cursor().execute("SELECT balance FROM users WHERE user_id = ?", (update.effective_user.id,)).fetchone()[0]
    if bal < MIN_WITHDRAWAL_LIMIT: await update.message.reply_text(f"❌ Min ${MIN_WITHDRAWAL_LIMIT:.2f} needed."); return ConversationHandler.END
    kb = [[InlineKeyboardButton("🔶 BEP20", callback_data="w_net_BEP20"), InlineKeyboardButton("🔷 TRC20", callback_data="w_net_TRC20")]];
    await update.message.reply_text("Network:", reply_markup=InlineKeyboardMarkup(kb)); return State.CHOOSE_WITHDRAW_NETWORK

async def choose_withdraw_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['network'] = update.callback_query.data.split("_")[2]; await update.callback_query.answer()
    await update.callback_query.edit_message_text("Send Address:"); return State.GET_WALLET_ADDRESS

async def get_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['address'] = update.message.text; await update.message.reply_text("Amount:"); return State.GET_WITHDRAW_AMOUNT

async def get_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    uid = update.effective_user.id
    try:
        amt = float(update.message.text)
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            c = conn.cursor()
            bal = c.execute("SELECT balance FROM users WHERE user_id = ?", (uid,)).fetchone()[0]
            if amt <= 0 or amt > bal: await update.message.reply_text("Invalid."); return State.GET_WITHDRAW_AMOUNT
            c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amt, uid))
            c.execute("INSERT INTO withdrawals (user_id, amount, network, wallet_address) VALUES (?, ?, ?, ?)", (uid, amt, context.user_data['network'], context.user_data['address'])); wid = c.lastrowid; conn.commit()
        await update.message.reply_text("✅ Submitted.")
        msg = f"🔔 *New Withdrawal*\nID: `{wid}`\n@{(update.effective_user.username or 'N/A')}\nAmt: **${amt:.2f}**\nAddr: `{context.user_data['address']}`"
        kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"approve_{wid}"), InlineKeyboardButton("❌ Reject", callback_data=f"reject_{wid}")]]
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        return ConversationHandler.END
    except: await update.message.reply_text("Error."); return State.GET_WITHDRAW_AMOUNT

# --- TRACKING ---
async def add_tracked_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    await update.callback_query.message.delete(); await update.callback_query.message.reply_text("Name?", reply_markup=ReplyKeyboardRemove()); return State.GET_TRACKED_NAME
async def get_tracked_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['tracked_name'] = update.message.text; await update.message.reply_text("ID?"); return State.GET_TRACKED_ID
async def get_tracked_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    context.user_data['tracked_id'] = update.message.text; await update.message.reply_text("Link?"); return State.GET_TRACKED_URL
async def get_tracked_url_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        try: conn.cursor().execute("INSERT INTO forced_channels (channel_name, channel_id, channel_url) VALUES (?, ?, ?)", (context.user_data['tracked_name'], context.user_data['tracked_id'], update.message.text)); conn.commit(); await update.message.reply_text("✅ Added.", reply_markup=get_admin_keyboard())
        except: await update.message.reply_text("Already tracked.", reply_markup=get_admin_keyboard())
    context.user_data.clear(); return ConversationHandler.END

async def remove_tracked_channel_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        chans = conn.cursor().execute("SELECT id, channel_name FROM forced_channels WHERE status = 'active'").fetchall()
    if not chans: await query.edit_message_text("No channels.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin_tracking")]])); return
    kb = [[InlineKeyboardButton(f"❌ {n}", callback_data=f"delete_tracked_{cid}")] for cid, n in chans]
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back_to_admin_tracking")])
    await query.edit_message_text("Remove:", reply_markup=InlineKeyboardMarkup(kb))

# --- COUPONS ---
async def generate_coupon_message_text(context: ContextTypes.DEFAULT_TYPE, code: str, budget: float, max_c: int, current_c: int) -> str:
    bot_un = (await context.bot.get_me()).username
    st = "✅ Active" if current_c < max_c else "❌ Expired"
    return (f"🎁 **Today Coupon Code** 🎁\n\n**Code** : `{code}`\n**Budget** : ${budget:.2f}\n"
            f"**Claims** : {current_c} / {max_c}\n{st}\n\n➡️ Get reward at: @{bot_un}")

async def handle_coupon_management(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID: return
    kb = [[InlineKeyboardButton("➕ Create", callback_data="admin_create_coupon_start"), InlineKeyboardButton("📜 History", callback_data="admin_coupon_history")], [InlineKeyboardButton("➕ Add Channel", callback_data="admin_add_coupon_tracked_start"), InlineKeyboardButton("🗑️ Remove Channel", callback_data="admin_remove_coupon_tracked_list")]]
    await update.message.reply_text("🎟️ *Coupons*", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def create_coupon_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    await update.callback_query.message.delete(); await update.callback_query.message.reply_text("Budget?"); return State.GET_COUPON_BUDGET
async def get_coupon_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try: context.user_data['coupon_budget'] = float(update.message.text); await update.message.reply_text("Max claims?"); return State.GET_COUPON_MAX_CLAIMS
    except: return State.GET_COUPON_BUDGET
async def get_coupon_max_claims_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        mc, bud = int(update.message.text), context.user_data['coupon_budget']
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            c = conn.cursor()
            code = f"C-{random.randint(10000000, 99999999)}"
            c.execute("INSERT INTO coupons (coupon_code, budget, max_claims) VALUES (?, ?, ?)", (code, bud, mc)); conn.commit()
            txt = await generate_coupon_message_text(context, code, bud, mc, 0)
            chans = c.execute("SELECT channel_id FROM coupon_forced_channels WHERE status = 'active'").fetchall()
            for (cid,) in chans:
                try: 
                    sent = await context.bot.send_message(chat_id=cid, text=txt, parse_mode='Markdown')
                    c.execute("INSERT INTO coupon_messages (coupon_code, chat_id, message_id) VALUES (?, ?, ?)", (code, sent.chat_id, sent.message_id)); conn.commit()
                except: pass
        await update.message.reply_text("✅ Created.", reply_markup=get_admin_keyboard()); context.user_data.clear(); return ConversationHandler.END
    except: return State.GET_COUPON_MAX_CLAIMS

async def claim_coupon_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    if not await is_member_or_send_join_message(update, context): return ConversationHandler.END
    res = await check_membership_and_grant_access(update, context, 'verify_coupon_membership', 'coupon_forced_channels')
    if res in ['CONTINUE', 'PROCEED_TO_CODE']: return State.AWAIT_COUPON_CODE
    return ConversationHandler.END

async def receive_coupon_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    uid, code = update.effective_user.id, update.message.text.strip().upper()
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        data = c.execute("SELECT budget, max_claims, claims_count, status FROM coupons WHERE coupon_code = ?", (code,)).fetchone()
        if not data: await update.message.reply_text("❌ Invalid."); return State.AWAIT_COUPON_CODE
        if c.execute("SELECT 1 FROM claimed_coupons WHERE user_id = ? AND coupon_code = ?", (uid, code)).fetchone(): await update.message.reply_text("⚠️ Already claimed."); return ConversationHandler.END
        bud, mc, cc, st = data
        if st != 'active' or cc >= mc: await update.message.reply_text("😥 Expired."); return ConversationHandler.END
        rew = ((mc - cc) / (mc * (mc + 1) / 2)) * bud
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (rew, uid))
        c.execute("INSERT INTO claimed_coupons (user_id, coupon_code) VALUES (?, ?)", (uid, code))
        c.execute("UPDATE coupons SET claims_count = claims_count + 1 WHERE coupon_code = ?", (code,)); conn.commit()
        await update.message.reply_text(f"✅ Earned ${rew:.2f}!")
        cc += 1; new_txt = await generate_coupon_message_text(context, code, bud, mc, cc)
        msgs = c.execute("SELECT chat_id, message_id FROM coupon_messages WHERE coupon_code = ?", (code,)).fetchall()
        for cid, mid in msgs:
            try: await context.bot.edit_message_text(text=new_txt, chat_id=cid, message_id=mid, parse_mode='Markdown')
            except: pass
    return ConversationHandler.END

# --- CALLBACK HANDLER ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query; await q.answer(); data, uid = q.data, q.from_user.id
    if data == "verify_membership": await check_membership_and_grant_access(update, context, 'verify_membership', 'forced_channels')
    elif data == "clear_join_message": await q.message.delete()
    elif data.startswith("verify_join_"):
        tid = int(data.split("_")[2])
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            c = conn.cursor(); info = c.execute("SELECT reward, target_chat_id FROM tasks WHERE task_id = ?", (tid,)).fetchone()
            try:
                m = await context.bot.get_chat_member(chat_id=info[1], user_id=uid)
                if m.status in ['member', 'administrator', 'creator']:
                    c.execute("INSERT OR IGNORE INTO completed_tasks (user_id, task_id) VALUES (?, ?)", (uid, tid))
                    if c.rowcount > 0:
                        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (info[0], uid)); conn.commit()
                        await q.edit_message_text(f"✅ Earned ${info[0]:.2f}"); await display_next_task(update, context)
                else: await q.answer("⚠️ Not joined.", show_alert=True)
            except: await q.answer("Error.", show_alert=True)
    elif data.startswith("approve_") or data.startswith("reject_"):
        act, wid = data.split("_")
        with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
            c = conn.cursor()
            res = c.execute("SELECT w.user_id, w.amount, w.network, w.wallet_address, u.first_name FROM withdrawals w JOIN users u ON w.user_id = u.user_id WHERE w.withdrawal_id = ?", (wid,)).fetchone()
            if not res: return
            wuid, amt, net, addr, fname = res
            if act == "approve":
                c.execute("UPDATE withdrawals SET status = 'approved' WHERE withdrawal_id = ?", (wid,)); conn.commit()
                await context.bot.send_message(chat_id=wuid, text=f"🎉 Approved ${amt:.2f}!")
                proof_ch = c.execute("SELECT value FROM settings WHERE key = 'proof_channel_id'").fetchone()
                if proof_ch:
                    t_real = (datetime.now() + timedelta(hours=3)).strftime("%-m/%-d/%Y %-I:%M:%S %p")
                    p_msg = f"🔎 Withdrawal Details\n🆔 ID: {wid}\n👤 User: {fname} ({wuid})\n💰 Amount: {amt}\n⛓ Net: {net}\n📍 Addr: {addr}\n⏰ Time: {t_real}\n✅ Paid"
                    try: await context.bot.send_message(chat_id=proof_ch[0], text=p_msg)
                    except: pass
            else:
                c.execute("UPDATE withdrawals SET status = 'rejected' WHERE withdrawal_id = ?", (wid,)); c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amt, wuid)); conn.commit()
                await context.bot.send_message(chat_id=wuid, text="😔 Rejected. Funds returned.")
        await q.message.delete()
    elif data.startswith("delete_task_"):
        with sqlite3.connect(DB_FILE) as conn: conn.cursor().execute("UPDATE tasks SET status = 'deleted' WHERE task_id = ?", (data.split("_")[2],)); conn.commit(); await delete_task_list(update, context)
    elif data.startswith("delete_tracked_"):
        with sqlite3.connect(DB_FILE) as conn: conn.cursor().execute("UPDATE forced_channels SET status = 'deleted' WHERE id = ?", (data.split("_")[2],)); conn.commit(); await remove_tracked_channel_list(update, context)
    elif data.startswith("delete_coupon_tracked_"):
        with sqlite3.connect(DB_FILE) as conn: conn.cursor().execute("UPDATE coupon_forced_channels SET status = 'deleted' WHERE id = ?", (data.split("_")[3],)); conn.commit(); await remove_coupon_tracked_channel_list(update, context)
    elif data == "back_to_admin_tasks": await handle_admin_tasks(update, context)
    elif data == "back_to_admin_tracking": await handle_admin_tracking(update, context)
    elif data == "back_to_coupon_menu": await handle_coupon_management(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    kb = get_user_keyboard(uid) if uid != ADMIN_ID else get_admin_keyboard()
    await update.effective_message.reply_text("Action canceled.", reply_markup=kb); context.user_data.clear(); return ConversationHandler.END

def main() -> None:
    # 1. START HEALTH CHECK IMMEDIATELY
    threading.Thread(target=run_health_check, daemon=True).start()
    
    # 2. SETUP DATABASE
    setup_database()

    # 3. CONFIGURE BOT
    if not BOT_API_KEY:
        logger.error("❌ NO BOT_API_KEY FOUND. EXITING.")
        return

    application = Application.builder().token(BOT_API_KEY).build()
    
    u_btns = ["💰 Balance", "👥 Referral", "🎁 Daily Bonus", "📋 Tasks", "💸 Withdraw", "🎟️ Coupon Code", "👑 Admin Panel"]
    a_btns = ["📧 Mailing", "📋 Task Management", "🎟️ Coupon Management", "📊 Bot Stats", "🏧 Withdrawals", "🔗 Main Track Management", "📢 Proof Channel", "⬅️ Back to User Menu"]
    menu_filter = filters.Regex(f"^({'|'.join(u_btns + a_btns)})$")
    t_filter = filters.TEXT & ~filters.COMMAND & ~menu_filter

    async def menu_interrupt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text("Canceled."); text = update.message.text
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
        elif text == "📢 Proof Channel": await handle_admin_proof_channel(update, context)
        elif text == "⬅️ Back to User Menu": await admin_back_to_user_menu(update, context)
        return ConversationHandler.END

    f_backs = [CommandHandler("cancel", cancel), MessageHandler(menu_filter, menu_interrupt)]

    # REGISTER ALL CONVERSATION HANDLERS
    application.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(add_task_start, pattern="^admin_add_task_start$")], states={State.GET_TASK_NAME: [MessageHandler(t_filter, get_task_name)], State.GET_TARGET_CHAT_ID: [MessageHandler(t_filter, get_target_chat_id)], State.GET_TASK_URL: [MessageHandler(t_filter, get_task_url)], State.GET_TASK_REWARD: [MessageHandler(t_filter, get_task_reward_and_save)]}, fallbacks=f_backs))
    application.add_handler(ConversationHandler(entry_points=[MessageHandler(filters.Regex("^📧 Mailing$"), mailing_start)], states={State.GET_MAIL_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND & ~menu_filter, get_mail_message)], State.AWAIT_BUTTON_OR_SEND: [CallbackQueryHandler(await_button_or_send, pattern="^mail_add_button$"), CallbackQueryHandler(broadcast_message, pattern="^mail_send_now$")], State.GET_BUTTON_DATA: [MessageHandler(t_filter, get_button_data)]}, fallbacks=f_backs))
    application.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(add_tracked_channel_start, pattern="^admin_add_tracked_start$")], states={State.GET_TRACKED_NAME: [MessageHandler(t_filter, get_tracked_name)], State.GET_TRACKED_ID: [MessageHandler(t_filter, get_tracked_id)], State.GET_TRACKED_URL: [MessageHandler(t_filter, get_tracked_url_and_save)]}, fallbacks=f_backs))
    application.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(create_coupon_start, pattern="^admin_create_coupon_start$")], states={State.GET_COUPON_BUDGET: [MessageHandler(t_filter, get_coupon_budget)], State.GET_COUPON_MAX_CLAIMS: [MessageHandler(t_filter, get_coupon_max_claims_and_save)]}, fallbacks=f_backs))
    application.add_handler(ConversationHandler(entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_start)], states={State.CHOOSE_WITHDRAW_NETWORK: [CallbackQueryHandler(choose_withdraw_network, pattern="^w_net_")], State.GET_WALLET_ADDRESS: [MessageHandler(t_filter, get_wallet_address)], State.GET_WITHDRAW_AMOUNT: [MessageHandler(t_filter, get_withdraw_amount)]}, fallbacks=f_backs))
    application.add_handler(ConversationHandler(entry_points=[MessageHandler(filters.Regex("^🎟️ Coupon Code$"), claim_coupon_start)], states={State.AWAIT_COUPON_CODE: [MessageHandler(t_filter, receive_coupon_code), CallbackQueryHandler(claim_coupon_start, pattern="^verify_coupon_membership$")]}, fallbacks=f_backs))
    application.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(set_proof_channel_start, pattern="^admin_set_proof_start$")], states={State.SET_PROOF_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_proof_channel)]}, fallbacks=f_backs))

    # REGISTER REMAINING HANDLERS
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~menu_filter, gatekeeper_handler), group=-1)
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
    application.add_handler(MessageHandler(filters.Regex("^📢 Proof Channel$"), handle_admin_proof_channel))
    application.add_handler(CallbackQueryHandler(button_callback_handler))

    # START POLLING WITH ERROR PROTECTION
    logger.info("🚀 Bot is starting polling...")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except (NetworkError, Exception) as e:
        logger.error(f"💥 BOT CRASHED: {e}")
        # Choreo will restart the container automatically

if __name__ == "__main__":
    main()

import logging
import sqlite3
import io
import os
import random
from datetime import datetime, date
from enum import Enum

from telegram import (
    ReplyKeyboardMarkup, Update, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup, InputFile, MessageOriginUser
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from telegram.error import BadRequest, Forbidden

# --- Configuration ---
# IMPORTANT: Replace with your new, valid Bot API Key from BotFather
# Use environment variables
BOT_API_KEY = os.environ.get("BOT_API_KEY") 
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5815604554"))

REFERRAL_BONUS = 0.05
DAILY_BONUS = 0.05
MIN_WITHDRAWAL_LIMIT = 5.00
MIN_CPC = 0.05 

# Choreo uses /data for persistent storage if you attach a volume
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
    GET_COUPON_BUDGET = 14; GET_COUPON_MAX_CLAIMS = 15
    AWAIT_COUPON_CODE = 16
    GET_COUPON_TRACKED_NAME = 17; GET_COUPON_TRACKED_ID = 18; GET_COUPON_TRACKED_URL = 19
    # States for Ad Creation Flow
    GET_AD_LINK = 20; AWAIT_ADMIN_CONFIRMATION = 21; GET_AD_DESCRIPTION = 22
    GET_AD_CPC = 23; GET_AD_BUDGET = 24
    # States for BOT Ad Creation Flow
    GET_BOT_FORWARD = 25; GET_BOT_LINK = 26; GET_BOT_AD_DESCRIPTION = 27
    GET_BOT_AD_CPC = 28; GET_BOT_AD_BUDGET = 29
    # Add this new state
    AWAIT_BOT_TASK_VERIFICATION = 30

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
        c.execute("""
            CREATE TABLE IF NOT EXISTS advertisements (
                ad_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ad_type TEXT NOT NULL, -- 'channel' or 'bot'
                target_id TEXT NOT NULL, -- e.g., @username
                target_url TEXT NOT NULL,
                description TEXT,
                cpc REAL NOT NULL,
                daily_budget REAL NOT NULL,
                is_tracking_enabled INTEGER DEFAULT 0, -- 0 for False, 1 for True
                status TEXT DEFAULT 'paused', -- paused, active, completed, deleted
                creation_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS completed_ads (
                user_id INTEGER NOT NULL,
                ad_id INTEGER NOT NULL,
                status TEXT NOT NULL, -- 'completed' or 'skipped'
                completion_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, ad_id),
                FOREIGN KEY (user_id) REFERENCES users (user_id),
                FOREIGN KEY (ad_id) REFERENCES advertisements (ad_id)
            )
        """)
        conn.commit()

# --- Keyboard Definitions ---
def get_user_keyboard(user_id):
    user_buttons = [
        [KeyboardButton("💰 Balance"), KeyboardButton("👥 Referral")],
        [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📋 Tasks")],
        [KeyboardButton("💸 Withdraw"), KeyboardButton("🎟️ Coupon Code")]
        # Removed Advertise button from here
    ]
    if user_id == ADMIN_ID:
        user_buttons.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(user_buttons, resize_keyboard=True)

def get_main_advertise_keyboard():
    advertise_buttons = [
        [KeyboardButton("My Ads"), KeyboardButton("➕ Create New Ad ➕")],
        [KeyboardButton("⬅️ Back to Main Menu")]
    ]
    return ReplyKeyboardMarkup(advertise_buttons, resize_keyboard=True)

def get_create_ad_type_keyboard():
    advertise_buttons = [
        [KeyboardButton("📢 Channel/Group"), KeyboardButton("🤖 Bot")],
        [KeyboardButton("⬅️ Back to Advertise")]
    ]
    return ReplyKeyboardMarkup(advertise_buttons, resize_keyboard=True)

def get_tasks_keyboard():
    tasks_buttons = [
        [KeyboardButton("🔗 Join Channel/Group"), KeyboardButton("🤖 Start Bot")],
        [KeyboardButton("⬅️ Back to Main Menu")]
    ]
    return ReplyKeyboardMarkup(tasks_buttons, resize_keyboard=True)

def get_admin_keyboard():
    admin_buttons = [
        [KeyboardButton("📧 Mailing"), KeyboardButton("📋 Task Management")],
        [KeyboardButton("🎟️ Coupon Management"), KeyboardButton("📊 Bot Stats")],
        [KeyboardButton("🏧 Withdrawals"), KeyboardButton("🔗 Main Track Management")],
        [KeyboardButton("⬅️ Back to User Menu")],
    ]
    return ReplyKeyboardMarkup(admin_buttons, resize_keyboard=True)

async def handle_advertise_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the main advertising menu."""
    if not await is_member_or_send_join_message(update, context):
        return
    await update.message.reply_text(
        "Welcome to the advertising panel. Here you can create and manage your ad campaigns.",
        reply_markup=get_main_advertise_keyboard()
    )

# --- ADVERTISE CHANNEL FLOW ---
async def advertise_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    if not await is_member_or_send_join_message(update, context):
        return ConversationHandler.END
    context.user_data['new_ad'] = {'ad_type': 'channel'}
    message_text = (
        "🔗 *Send the PUBLIC LINK of your channel/group*\n\n"
        "ℹ️ Please make sure the link starts with `https://t.me/`.\n"
        "ℹ️ Alternatively, you can share the `@username` (including @)\n\n"
        "Members will join your channels or groups immediately after you activate this ad!\n\n"
        "👇🏻 Send the link to your channel or group now."
    )
    await update.message.reply_text(
        message_text,
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup([["⬅️ Back to Advertise"]], resize_keyboard=True)
    )
    return State.GET_AD_LINK

async def get_ad_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    link = update.message.text.strip()
    if link.startswith("https://t.me/") or link.startswith("@"):
        context.user_data['new_ad']['link'] = link
        target_id = link if link.startswith("@") else "@" + link.split('/')[-1]
        context.user_data['new_ad']['target_id'] = target_id
        message_text = (
            "⚠️ Make the bot admin of your channel and press « ✅ Done » button.\n\n"
            "ℹ️ This step is necessary to track the exact number of leaving members.\n\n"
            "☑️ *Continue without tracking*: This kind of ad won't provide precise members count tracking."
        )
        keyboard = [
            [InlineKeyboardButton("✅ Done (I made the bot admin)", callback_data="ad_admin_done")],
            [InlineKeyboardButton("☑️ Continue without tracking", callback_data="ad_admin_skip")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="ad_cancel")]
        ]
        await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return State.AWAIT_ADMIN_CONFIRMATION
    else:
        await update.message.reply_text("❌ Invalid format. Please send a valid channel link starting with `https://t.me/` or a `@username`.")
        return State.GET_AD_LINK

async def handle_admin_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query
    await query.answer()
    if query.data == 'ad_cancel':
        await query.edit_message_text("Action canceled.")
        user_id = update.effective_user.id
        await query.message.reply_text("⬅️ Returning to the main menu.", reply_markup=get_user_keyboard(user_id))
        context.user_data.pop('new_ad', None)
        return ConversationHandler.END
    context.user_data['new_ad']['tracking'] = (query.data == 'ad_admin_done')
    message_text = (
        "✏️ *Create an engaging description for your AD:*\n\n"
        "• This will be the first thing users see and it should grab their attention and make them want to click on your link or check out your product/service.\n\n"
        "ℹ️ You can use formatting options like *bold*, _italic_, and more to make your description stand out."
    )
    await query.edit_message_text(message_text, parse_mode='Markdown')
    return State.GET_AD_DESCRIPTION

async def get_ad_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    description = update.message.text
    context.user_data['new_ad']['description'] = description
    preview_text = f"*Preview of your AD:*\n\n{description}"
    await update.message.reply_text(preview_text, parse_mode='Markdown')
    cpc_message = (
        f"💸 *How much do you want to pay for each click?*\n\n"
        "ℹ️ This is the amount you'll pay for each person who joins. Paying more will get your ad displayed in front of others.\n\n"
        f"🔻 Min: `${MIN_CPC:.2f}`\n\n"
        "👇🏻 Enter your desired CPC in $"
    )
    await update.message.reply_text(cpc_message, parse_mode='Markdown')
    return State.GET_AD_CPC

async def get_ad_cpc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    try:
        cpc = float(update.message.text)
        if cpc < MIN_CPC:
            await update.message.reply_text(f"The minimum CPC is ${MIN_CPC:.2f}. Please enter a higher amount.")
            return State.GET_AD_CPC
        context.user_data['new_ad']['cpc'] = cpc
        with sqlite3.connect(DB_FILE) as conn:
            balance = conn.cursor().execute("SELECT balance FROM users WHERE user_id = ?", (update.effective_user.id,)).fetchone()[0]
        budget_message = (
            "💰 *What is your daily budget for this ad campaign?*\n\n"
            "ℹ️ This will determine the maximum amount you are willing to spend per day. Your ad will be paused for the day if the daily budget is exceeded.\n\n"
            f"💸 Available Balance: `${balance:.2f}`\n\n"
            "👇🏻 Enter your desired daily budget in $"
        )
        await update.message.reply_text(budget_message, parse_mode='Markdown')
        return State.GET_AD_BUDGET
    except (ValueError, TypeError):
        await update.message.reply_text("Invalid number. Please enter your desired CPC (e.g., `0.10`).")
        return State.GET_AD_CPC

async def get_ad_budget_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Gets budget, validates, sets initial status, saves campaign, and ends conversation."""
    try:
        budget = float(update.message.text)
        if budget <= 0:
            await update.message.reply_text("The daily budget must be a positive number. Please try again.")
            return State.GET_AD_BUDGET

        user_id = update.effective_user.id
        initial_status = "paused" # Default status

        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            balance = c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
            if budget > balance:
                await update.message.reply_text(
                    f"❌ Insufficient balance for daily budget.\n\n"
                    f"Your balance: `${balance:.2f}`\n"
                    f"Required budget: `${budget:.2f}`\n\n"
                    "Please enter a smaller budget or top up your balance."
                )
                return State.GET_AD_BUDGET
            
            # Set status to active if balance is sufficient
            if balance >= budget:
                initial_status = "active"

        ad_data = context.user_data['new_ad']
        ad_data['budget'] = budget
        
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute(
                """INSERT INTO advertisements
                   (user_id, ad_type, target_id, target_url, description, cpc, daily_budget, is_tracking_enabled, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, ad_data['ad_type'], ad_data['target_id'], ad_data['link'], ad_data['description'],
                 ad_data['cpc'], ad_data['budget'], 1 if ad_data['tracking'] else 0, initial_status)
            )
            ad_id = c.lastrowid
            conn.commit()
        
        if initial_status == "active":
             await update.message.reply_text("✅ Your ad campaign has been created and is now **active**!")
        else:
             await update.message.reply_text("✅ Your ad campaign has been created but is **paused**. You can activate it from the 'My Ads' menu once your balance is sufficient.")

        message_content = await generate_ad_management_message(ad_id)
        await update.message.reply_text(
            message_content["text"],
            parse_mode='Markdown',
            reply_markup=message_content["reply_markup"]
        )
        await update.message.reply_text("Returning to menu.", reply_markup=get_user_keyboard(user_id))
        context.user_data.pop('new_ad', None)
        return ConversationHandler.END
    except (ValueError, TypeError):
        await update.message.reply_text("Invalid number. Please enter your desired daily budget (e.g., `10`).")
        return State.GET_AD_BUDGET

# --- ADVERTISE BOT FLOW ---
async def advertise_bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Starts the bot advertising creation flow."""
    if not await is_member_or_send_join_message(update, context):
        return ConversationHandler.END
    context.user_data['new_ad'] = {'ad_type': 'bot'}
    message_text = (
        "🔎 *FORWARD a message from the bot you want to promote*\n\n"
        "1️⃣ Go to the bot you want to promote\n"
        "2️⃣ Select any message\n"
        "3️⃣ Forward it to this bot\n\n"
        "👇🏻 Do it now"
    )
    await update.message.reply_text(
        message_text,
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup([["⬅️ Back to Advertise"]], resize_keyboard=True)
    )
    return State.GET_BOT_FORWARD

async def get_bot_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Receives a forwarded message and validates it's from a bot."""
    origin = update.message.forward_origin

    # The new, correct check for modern python-telegram-bot versions
    if not isinstance(origin, MessageOriginUser) or not origin.sender_user.is_bot:
        await update.message.reply_text("❌ This is not a forwarded message from a bot. Please try again.")
        return State.GET_BOT_FORWARD

    bot_user = origin.sender_user
    context.user_data['new_ad']['target_id'] = f"@{bot_user.username}"
    message_text = (
        "🔗 *Send the bot LINK*\n\n"
        "ℹ️ It can be your referral link or any simple link to the bot.\n\n"
        f"⚠️ It should start with `https://t.me/{bot_user.username}`\n\n"
        "👇🏻 Send it now"
    )
    await update.message.reply_text(message_text, parse_mode='Markdown')
    return State.GET_BOT_LINK

async def get_bot_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Receives the bot link and validates it."""
    link = update.message.text.strip()
    bot_username = context.user_data['new_ad']['target_id'][1:]  # remove @
    if not link.startswith(f"https://t.me/{bot_username}"):
        await update.message.reply_text(
            f"❌ Invalid link. It must start with `https://t.me/{bot_username}`. Please try again.",
            parse_mode='Markdown'
        )
        return State.GET_BOT_LINK

    context.user_data['new_ad']['link'] = link
    message_text = (
        "✏️ *Create an engaging description for your AD:*\n\n"
        "• This will be the first thing users see and it should grab their attention and make them want to click on your link or check out your product/service.\n\n"
        "ℹ️ You can use formatting options like *bold*, _italic_, and more to make your description stand out."
    )
    await update.message.reply_text(message_text, parse_mode='Markdown')
    return State.GET_BOT_AD_DESCRIPTION

async def get_bot_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Receives the ad description and asks for CPC."""
    description = update.message.text
    context.user_data['new_ad']['description'] = description
    preview_text = f"*Preview of your AD:*\n\n{description}"
    await update.message.reply_text(preview_text, parse_mode='Markdown')
    cpc_message = (
        f"💸 *How much do you want to pay for each click?*\n\n"
        "ℹ️ This is the amount you'll pay for each person who clicks on your ad. Paying more will get your ad displayed in front of others.\n\n"
        "To target only Telegram Premium users, use /premium_users_only\n\n"
        f"🔻 Min: `${MIN_CPC:.2f}`\n\n"
        "👇🏻 Enter your desired CPC in $"
    )
    await update.message.reply_text(cpc_message, parse_mode='Markdown')
    return State.GET_BOT_AD_CPC

async def get_bot_cpc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Receives the CPC and asks for the daily budget."""
    try:
        cpc = float(update.message.text)
        if cpc < MIN_CPC:
            await update.message.reply_text(f"The minimum CPC is ${MIN_CPC:.2f}. Please enter a higher amount.")
            return State.GET_BOT_AD_CPC
        context.user_data['new_ad']['cpc'] = cpc
        with sqlite3.connect(DB_FILE) as conn:
            balance = conn.cursor().execute("SELECT balance FROM users WHERE user_id = ?", (update.effective_user.id,)).fetchone()[0]
        budget_message = (
            "💰 *What is your daily budget for this ad campaign?*\n\n"
            "ℹ️ This will determine the maximum amount you are willing to spend per day. Your ad will be paused for the day if the daily budget is exceeded.\n\n"
            f"💸 Available Balance: `${balance:.2f}`\n\n"
            "👇🏻 Enter your desired daily budget in $"
        )
        await update.message.reply_text(budget_message, parse_mode='Markdown')
        return State.GET_BOT_AD_BUDGET
    except (ValueError, TypeError):
        await update.message.reply_text("Invalid number. Please enter your desired CPC (e.g., `0.10`).")
        return State.GET_BOT_AD_CPC

async def get_bot_budget_and_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Gets budget, validates, saves the bot campaign, and ends conversation."""
    try:
        budget = float(update.message.text)
        if budget <= 0:
            await update.message.reply_text("The daily budget must be a positive number. Please try again.")
            return State.GET_BOT_AD_BUDGET

        user_id = update.effective_user.id
        initial_status = "paused"

        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            balance = c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
            if budget > balance:
                await update.message.reply_text(
                    f"❌ Insufficient balance for daily budget.\n\n"
                    f"Your balance: `${balance:.2f}`\nRequired budget: `${budget:.2f}`\n\n"
                    "Please enter a smaller budget or top up your balance."
                )
                return State.GET_BOT_AD_BUDGET
            if balance >= budget:
                initial_status = "active"

        ad_data = context.user_data['new_ad']
        ad_data['budget'] = budget
        
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute(
                """INSERT INTO advertisements
                   (user_id, ad_type, target_id, target_url, description, cpc, daily_budget, is_tracking_enabled, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, ad_data['ad_type'], ad_data['target_id'], ad_data['link'], ad_data['description'],
                 ad_data['cpc'], ad_data['budget'], 0, initial_status) # Tracking is N/A for bots
            )
            ad_id = c.lastrowid
            conn.commit()
        
        await update.message.reply_text("✅ Promotion created successfully", reply_markup=get_main_advertise_keyboard())

        message_content = await generate_ad_management_message(ad_id)
        await update.message.reply_text(
            message_content["text"],
            parse_mode='Markdown',
            reply_markup=message_content["reply_markup"]
        )
        context.user_data.pop('new_ad', None)
        return ConversationHandler.END
    except (ValueError, TypeError):
        await update.message.reply_text("Invalid number. Please enter your desired daily budget (e.g., `10`).")
        return State.GET_BOT_AD_BUDGET


async def generate_ad_management_message(ad_id: int) -> dict:
    """Generates the text and keyboard for managing an ad, including live stats."""
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        ad = c.execute("SELECT target_id, description, cpc, daily_budget, status, ad_type, target_url FROM advertisements WHERE ad_id = ?", (ad_id,)).fetchone()

        if not ad:
            return {"text": "This advertisement could not be found. It may have been deleted.", "reply_markup": None}

        target_id, description, cpc, daily_budget, status, ad_type, target_url = ad

        total_clicks = c.execute(
            "SELECT COUNT(*) FROM completed_ads WHERE ad_id = ? AND status = 'completed'", (ad_id,)
        ).fetchone()[0]
        
        total_skips = c.execute(
            "SELECT COUNT(*) FROM completed_ads WHERE ad_id = ? AND status = 'skipped'", (ad_id,)
        ).fetchone()[0]
        
        today_str = date.today().isoformat()
        todays_clicks = c.execute(
            "SELECT COUNT(*) FROM completed_ads WHERE ad_id = ? AND status = 'completed' AND DATE(completion_date) = ?",
            (ad_id, today_str)
        ).fetchone()[0]
        
        spent_today = todays_clicks * cpc

    if status == 'paused':
        status_line = "ℹ️ Status: 🕐 Paused"
        toggle_button = InlineKeyboardButton("▶️ Activate", callback_data=f"ad_activate_{ad_id}")
    elif status == 'active':
        status_line = "ℹ️ Status: ✅ Active"
        toggle_button = InlineKeyboardButton("⏸️ Pause", callback_data=f"ad_pause_{ad_id}")
    else:
        status_line = f"ℹ️ Status: {status.capitalize()}"
        toggle_button = None

    text = ""
    if ad_type == 'bot':
        text = (
            f"⚙️ Campaign #{ad_id}/{target_id.replace('@', '')} - 🤖 Bot\n\n"
            "👇🏻 *Your Advert (User can see this)*\n"
            f"{description}\n\n"
            f"🔗 Users will be asked to start {target_id} using this link: {target_url}\n\n"
            "🔎 Telegram Premium Users ONLY: disabled\n"
            f"💸 CPC: `${cpc:.2f}`\n"
            f"💰 Daily Budget: `${daily_budget:.2f}`\n\n"
            f"{status_line}\n"
            f"👉🏻 Total Clicks: {total_clicks}\n"
            f"⏩ Total Skips: {total_skips}\n"
            f"💰 Spent Today: ${spent_today:.2f}"
        )
    else:  # Default to existing channel ad format
        text = (
            f"✅ *Promotion Details*\n"
            f"⚙️ Campaign #{ad_id}/{target_id} - 📢 Channel Members\n\n"
            "👇🏻 *Your Advert (User can see this)*\n"
            f"{description}\n\n"
            f"🔗 Users will be asked to join {target_id}.\n\n"
            f"💸 CPC: `${cpc:.2f}`\n"
            f"💰 Daily Budget: `${daily_budget:.2f}`\n\n"
            f"{status_line}\n"
            f"👉🏻 Total Clicks: {total_clicks}\n"
            f"💰 Spent Today: ${spent_today:.2f}"
        )

    keyboard_rows = []
    if toggle_button:
        keyboard_rows.append([toggle_button, InlineKeyboardButton("❌ Delete", callback_data=f"ad_delete_{ad_id}")])

    if status in ['active', 'paused']:
         keyboard_rows.extend([
            [InlineKeyboardButton("🔺 Increase CPC", callback_data=f"ad_edit_cpc_{ad_id}"),
             InlineKeyboardButton("💵 Edit Daily Budget", callback_data=f"ad_edit_budget_{ad_id}")],
            [InlineKeyboardButton("📍 Edit Description", callback_data=f"ad_edit_desc_{ad_id}"),
             InlineKeyboardButton("🌍 Edit Geolocation", callback_data=f"ad_edit_geo_{ad_id}")]
        ])

    reply_markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

    return {"text": text, "reply_markup": reply_markup}


# --- TASK (ADVERTISEMENT) SYSTEM FOR USERS ---
async def start_join_tasks_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_member_or_send_join_message(update, context):
        return
    await show_next_join_task(update, context)

async def show_next_join_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    query = update.callback_query
    target_message = query.message if query else update.message

    all_tasks = []
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        admin_tasks = c.execute("""
            SELECT task_id, task_name, reward, task_url, 'admin' as type
            FROM tasks
            WHERE status = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM completed_tasks ct
                  WHERE ct.task_id = tasks.task_id AND ct.user_id = ?
              )
        """, (user_id,)).fetchall()
        for task_id, name, reward, url, task_type in admin_tasks:
            all_tasks.append({'id': task_id, 'desc': name, 'reward': reward, 'url': url, 'type': task_type})

        user_ads = c.execute("""
            SELECT ad.ad_id, ad.description, ad.cpc, ad.target_url, 'user' as type
            FROM advertisements ad
            JOIN users u ON ad.user_id = u.user_id
            WHERE ad.status = 'active'
              AND ad.ad_type = 'channel'
              AND ad.user_id != ?
              AND u.balance >= ad.cpc
              AND NOT EXISTS (
                  SELECT 1 FROM completed_ads ca
                  WHERE ca.ad_id = ad.ad_id AND ca.user_id = ?
              )
        """, (user_id, user_id)).fetchall()
        for ad_id, desc, cpc, url, task_type in user_ads:
            all_tasks.append({'id': ad_id, 'desc': desc, 'reward': cpc, 'url': url, 'type': task_type})

    if not all_tasks:
        message_text = "🎉 You have completed all available tasks! Please check back later for new ones."
        if query:
            await query.edit_message_text(message_text)
        else:
            await target_message.reply_text(message_text)
        return

    selected_task = random.choice(all_tasks)
    
    task_id = selected_task['id']
    description = selected_task['desc']
    reward = selected_task['reward']
    target_url = selected_task['url']
    task_type = selected_task['type']

    message_text = (
        f"**Join Channel/Group Task**\n\n"
        f"{description}\n\n"
        f"💰 Reward: **${reward:.2f}**"
    )

    keyboard = [
        [InlineKeyboardButton("➡️ Join", url=target_url)],
        [
            InlineKeyboardButton("✅ Verify Join", callback_data=f"task_verify_{task_type}_{task_id}"),
            InlineKeyboardButton("⏭️ Skip", callback_data=f"task_skip_{task_type}_{task_id}")
        ]
    ]

    if query:
        await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await target_message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- CORE HANDLERS & HELPERS ---
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
async def start_bot_tasks_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for the 'Start Bot' task flow."""
    if not await is_member_or_send_join_message(update, context):
        return
    await show_next_bot_task(update, context)

async def show_next_bot_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetches and displays the next available bot task."""
    user_id = update.effective_user.id
    query = update.callback_query
    target_message = query.message if query else update.message

    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        # Query specifically for active bot ads
        bot_ad = c.execute("""
            SELECT ad.ad_id, ad.description, ad.cpc, ad.target_url
            FROM advertisements ad
            JOIN users u ON ad.user_id = u.user_id
            WHERE ad.status = 'active'
              AND ad.ad_type = 'bot'
              AND ad.user_id != ?
              AND u.balance >= ad.cpc
              AND NOT EXISTS (
                  SELECT 1 FROM completed_ads ca
                  WHERE ca.ad_id = ad.ad_id AND ca.user_id = ?
              )
            ORDER BY RANDOM() LIMIT 1
        """, (user_id, user_id)).fetchone()

    if not bot_ad:
        message_text = "🎉 You have completed all available bot tasks! Please check back later."
        if query:
            await query.edit_message_text(message_text)
        else:
            await target_message.reply_text(message_text)
        return

    ad_id, description, cpc, target_url = bot_ad

    message_text = (
        f"**Start Bot Task**\n\n"
        f"{description}\n\n"
        f"💰 Reward: **${cpc:.2f}**"
    )

    keyboard = [
        [InlineKeyboardButton("➡️ Start Bot", url=target_url)],
        [
            InlineKeyboardButton("⏭️ Skip", callback_data=f"task_skip_user_{ad_id}"),
            InlineKeyboardButton("✅ Started", callback_data=f"task_start_verify_{ad_id}")
        ]
    ]
    
    # We must remove the old keyboard if this is a new message
    if query:
        await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        # Replying to a button click needs a new message
        await target_message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# ---FIXED: This function is now correctly un-indented---
async def start_bot_task_verification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Starts the conversation to verify a bot task by asking for a forwarded message."""
    query = update.callback_query
    await query.answer()
    
    ad_id = int(query.data.split("_")[-1])
    
    with sqlite3.connect(DB_FILE) as conn:
        ad_info = conn.cursor().execute("SELECT target_url, target_id FROM advertisements WHERE ad_id = ?", (ad_id,)).fetchone()

    if not ad_info:
        await query.edit_message_text("This task is no longer available.")
        return ConversationHandler.END

    target_url, target_id = ad_info
    context.user_data['verifying_ad_id'] = ad_id
    context.user_data['verifying_bot_username'] = target_id.replace('@', '')

    message_text = (
        f"🔎 *FORWARD a message from the bot at the link below*\n\n"
        f"➡️ {target_url}\n\n"
        "1️⃣ Go to the bot using the link.\n"
        "2️⃣ Start it and get any message from it.\n"
        "3️⃣ Forward that message here to verify.\n\n"
        "👇🏻 Do it now"
    )
    
    await query.edit_message_text(message_text, parse_mode='Markdown')
    
    return State.AWAIT_BOT_TASK_VERIFICATION


async def handle_bot_task_verification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the forwarded message to verify and reward the user."""
    ad_id = context.user_data.get('verifying_ad_id')
    expected_username = context.user_data.get('verifying_bot_username')
    user_id = update.effective_user.id
    
    origin = update.message.forward_origin
    
    # Check if the forwarded message is from the correct bot
    if isinstance(origin, MessageOriginUser) and origin.sender_user.is_bot and origin.sender_user.username == expected_username:
        
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            # Double check the ad still exists and is valid
            ad_info = c.execute("SELECT user_id, cpc FROM advertisements WHERE ad_id = ?", (ad_id,)).fetchone()
            if not ad_info:
                await update.message.reply_text("This ad is no longer available.")
                return ConversationHandler.END
            
            advertiser_id, cpc = ad_info
            
            # Perform transactions
            c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (cpc, advertiser_id))
            c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (cpc, user_id))
            c.execute("INSERT INTO completed_ads (user_id, ad_id, status) VALUES (?, ?, 'completed')", (user_id, ad_id))
            conn.commit()
        
        await update.message.reply_text(f"✅ Success! You earned **${cpc:.2f}** for completing the task.", parse_mode='Markdown')
        # Clean up user_data
        context.user_data.pop('verifying_ad_id', None)
        context.user_data.pop('verifying_bot_username', None)
        # Show the next task
        await show_next_bot_task(update, context)
        return ConversationHandler.END
    else:
        await update.message.reply_text("❌ Verification failed. That message was not forwarded from the correct bot. Please try the task again later.")
        context.user_data.pop('verifying_ad_id', None)
        context.user_data.pop('verifying_bot_username', None)
        return ConversationHandler.END

async def incorrect_verification_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles cases where user sends text instead of forwarding."""
    await update.message.reply_text("That's not a forwarded message. Please forward a message from the bot to complete the task.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if context.args and len(context.args) > 0:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id:
                context.user_data['referrer_id'] = referrer_id
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
        c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username))
        conn.commit()

        is_new_user = c.execute("SELECT referred_by FROM users WHERE user_id = ?", (user.id,)).fetchone() is None
        
        if verify_callback == 'verify_coupon_membership': pass
        else:
            welcome_message = f"✅ Thank you for joining!\n\n👋 Welcome, {user.first_name}!";
            if "from_admin_back" in context.user_data:
                welcome_message = "⬅️ Switched back to User Mode."; del context.user_data["from_admin_back"]
            
            referrer_id = context.user_data.get('referrer_id')
            if is_new_user and referrer_id:
                if c.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,)).fetchone():
                    c.execute("UPDATE users SET balance = balance + ?, referred_by = ? WHERE user_id = ?", (REFERRAL_BONUS, referrer_id, user.id))
                    c.execute("UPDATE users SET balance = balance + ?, referral_count = referral_count + 1 WHERE user_id = ?", (REFERRAL_BONUS, referrer_id))
                    conn.commit()
                    welcome_message = f"🎉 Welcome aboard, {user.first_name}!\nYou joined via a referral link and have received a welcome bonus of **${REFERRAL_BONUS:.2f}**!"
                    try:
                        await context.bot.send_message(chat_id=referrer_id, text=f"✅ Success! User *{user.first_name}* joined using your link.\nYou have been awarded **${REFERRAL_BONUS:.2f}**!", parse_mode='Markdown')
                    except (Forbidden, BadRequest) as e: logger.warning(f"Could not send referral notification to {referrer_id}: {e}")
                
                if 'referrer_id' in context.user_data: del context.user_data['referrer_id']

            await update.effective_message.reply_text(welcome_message, reply_markup=get_user_keyboard(user.id), parse_mode='Markdown')

    if verify_callback == 'verify_coupon_membership':
        await prompt_for_code(update, context)
        return 'PROCEED_TO_CODE'

    return ConversationHandler.END

# --- Simple Handlers ---
async def handle_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with sqlite3.connect(DB_FILE) as conn:
        balance = conn.cursor().execute("SELECT balance FROM users WHERE user_id = ?", (update.effective_user.id,)).fetchone()[0]
    await update.message.reply_text(f"💰 Your current balance is: **${balance:.2f}**.", parse_mode='Markdown')

async def handle_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        result = conn.cursor().execute("SELECT referral_count FROM users WHERE user_id = ?", (user_id,)).fetchone()
        referral_count = result[0] if result else 0
        
    bot_username = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start={user_id}"
    await update.message.reply_text(f"🚀 Invite friends and earn **${REFERRAL_BONUS:.2f}** for each friend who joins!\n\nYour referral link is:\n`{referral_link}`\n\n👥 You have successfully referred **{referral_count}** friends.", parse_mode='Markdown')

async def handle_daily_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    today = date.today()
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        last_claim_str = c.execute("SELECT last_bonus_claim FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
        if last_claim_str and date.fromisoformat(last_claim_str) >= today:
            await update.message.reply_text("You have already claimed your daily bonus today. Try again tomorrow!")
        else:
            c.execute("UPDATE users SET balance = balance + ?, last_bonus_claim = ? WHERE user_id = ?", (DAILY_BONUS, today.isoformat(), user_id))
            conn.commit()
            await update.message.reply_text(f"🎉 You have received ${DAILY_BONUS:.2f} as a daily bonus!", parse_mode='Markdown')

async def handle_tasks_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Please select a task category:", reply_markup=get_tasks_keyboard())

async def handle_start_bot_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🤖 'Start Bot' tasks from other users will appear here soon. Please check back later!")

# --- Advertise Menu Handlers ---

async def handle_create_new_ad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("What would you like to advertise?", reply_markup=get_create_ad_type_keyboard())

async def handle_my_ads(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        ads = c.execute(
            "SELECT ad_id, target_id, status, ad_type FROM advertisements WHERE user_id = ? AND status != 'deleted' ORDER BY creation_date DESC",
            (user_id,)
        ).fetchall()

    if not ads:
        await update.message.reply_text(
            "You don't have any ad campaigns yet. Click '➕ Create New Ad ➕' to start one!",
            reply_markup=get_main_advertise_keyboard()
        )
        return

    await update.message.reply_text("Here are your ad campaigns. Click on any campaign to manage it.")
    for ad_id, target_id, status, ad_type in ads:
        status_emoji = {'active': '✅', 'paused': '⏸️', 'completed': '🏁'}.get(status, '❓')
        message_text = (
            f"{status_emoji} Campaign #{ad_id}\n"
            f"Target: `{target_id}`\n"
            f"Type: {ad_type.capitalize()}\n"
            f"Status: {status.capitalize()}"
        )
        keyboard = [[InlineKeyboardButton("⚙️ Manage Campaign", callback_data=f"ad_manage_{ad_id}")]]
        await update.message.reply_text(
            message_text,
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    await update.message.reply_text("⬅️ Returning to the main menu.", reply_markup=get_user_keyboard(user_id))

# --- Admin Panel Functions ---
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
    message_text = "📋 *Task Management*\n\n(This section is for admin-created tasks only)"
    
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

# (Omitting the rest of the unchanged functions like mailing, tasks, withdrawals, coupons for brevity)
# ... all those functions remain the same as the previous version ...
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
    except sqlite3.Error as e:
        logger.error(f"Database error during coupon creation: {e}")
        await update.message.reply_text(f"❌ A database error occurred. Coupon was not created.", reply_markup=get_admin_keyboard())
    except Exception as e:
        logger.error(f"An unexpected error occurred during coupon creation: {e}")
        await update.message.reply_text(f"❌ An unexpected error occurred. Coupon was not created.", reply_markup=get_admin_keyboard())
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
    if result == 'CONTINUE': return State.AWAIT_COUPON_CODE
    if result == 'PROCEED_TO_CODE': return State.AWAIT_COUPON_CODE
    return ConversationHandler.END
async def prompt_for_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    message = update.message or update.callback_query.message
    await message.reply_text("✅ Membership verified! Please send me the coupon code to claim your reward.")
    return State.AWAIT_COUPON_CODE
async def receive_coupon_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    user_id = update.effective_user.id
    code = update.message.text.strip().upper()
    logger.info(f"User {user_id} attempting to claim coupon code: '{code}'")
    
    with sqlite3.connect(DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        coupon_data = c.execute("SELECT budget, max_claims, claims_count, status FROM coupons WHERE coupon_code = ?", (code,)).fetchone()
        if not coupon_data:
            await update.message.reply_text("❌ Invalid coupon code. Please check and try again.")
            return State.AWAIT_COUPON_CODE

        budget, max_claims, claims_count, status = coupon_data
        
        if c.execute("SELECT 1 FROM claimed_coupons WHERE user_id = ? AND coupon_code = ?", (user_id, code)).fetchone():
            await update.message.reply_text("⚠️ You have already claimed this coupon."); return ConversationHandler.END

        if status != 'active' or claims_count >= max_claims:
            await update.message.reply_text("😥 Sorry, this coupon is expired or at its claim limit.")
            if status == 'active': c.execute("UPDATE coupons SET status = 'expired' WHERE coupon_code = ?", (code,)); conn.commit()
            return ConversationHandler.END
        
        total_weight = max_claims * (max_claims + 1) / 2
        user_weight = max_claims - claims_count
        reward = (user_weight / total_weight) * budget if total_weight > 0 else 0
        
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, user_id))
        c.execute("INSERT INTO claimed_coupons (user_id, coupon_code) VALUES (?, ?)", (user_id, code))
        c.execute("UPDATE coupons SET claims_count = claims_count + 1 WHERE coupon_code = ?", (code,)); conn.commit()
        
        claims_count += 1
        messages_to_update = c.execute("SELECT chat_id, message_id FROM coupon_messages WHERE coupon_code = ?", (code,)).fetchall()
        
        await update.message.reply_text(f"✅**Congratulations!**\nYou claimed the coupon and received **${reward:.2f}**.", parse_mode='Markdown')

    if messages_to_update:
        new_message_text = await generate_coupon_message_text(context, code, budget, max_claims, claims_count)
        for chat_id, message_id in messages_to_update:
            try:
                await context.bot.edit_message_text(text=new_message_text, chat_id=chat_id, message_id=message_id, parse_mode='Markdown')
            except (BadRequest, Forbidden) as e: logger.warning(f"Could not update coupon msg {message_id} in chat {chat_id}: {e}")
    return ConversationHandler.END

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    
    if data.startswith("task_"):
        await query.answer()
        parts = data.split("_")
        # Handle cases like task_start_verify_12345
        if parts[1] == 'start' and parts[2] == 'verify':
            # This is handled by the bot_verification_conv, so we can ignore it here.
            return

        action, task_type, task_id = parts[1], parts[2], int(parts[3])

        paused_ads_info_for_notification = []
        advertiser_id_for_notification = None

        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()

            # --- LOGIC FOR USER-CREATED ADS (Channels & Bots) ---
            if task_type == 'user':
                ad_id = task_id
                
                if action == "skip":
                    c.execute("INSERT OR REPLACE INTO completed_ads (user_id, ad_id, status) VALUES (?, ?, 'skipped')", (user_id, ad_id))
                    conn.commit()
                    # Check the ad type to show the correct next task
                    ad_type_result = c.execute("SELECT ad_type FROM advertisements WHERE ad_id = ?", (ad_id,)).fetchone()
                    if ad_type_result:
                        ad_type = ad_type_result[0]
                        if ad_type == 'bot':
                            await show_next_bot_task(update, context)
                        else: # 'channel'
                            await show_next_join_task(update, context)
                    else: # Fallback if ad was deleted
                        await show_next_join_task(update, context)

                elif action == "verify": # This is for CHANNEL verification only
                    ad_info = c.execute("SELECT user_id, cpc, target_id, is_tracking_enabled FROM advertisements WHERE ad_id = ?", (ad_id,)).fetchone()
                    if not ad_info:
                        await query.message.reply_text("This ad is no longer available."); await query.message.delete(); return
                    advertiser_id, cpc, target_id, is_tracking_enabled = ad_info
                    if c.execute("SELECT 1 FROM completed_ads WHERE user_id = ? AND ad_id = ?", (user_id, ad_id)).fetchone():
                        await query.message.reply_text("You have already interacted with this ad."); return
                    advertiser_balance = c.execute("SELECT balance FROM users WHERE user_id = ?", (advertiser_id,)).fetchone()[0]
                    if advertiser_balance < cpc:
                        await query.message.reply_text("Advertiser has insufficient funds. Please try another task."); await show_next_join_task(update, context); return
                    
                    verification_passed = False
                    if is_tracking_enabled:
                        try:
                            member = await context.bot.get_chat_member(chat_id=target_id, user_id=user_id)
                            if member.status in ['member', 'administrator', 'creator']: verification_passed = True
                            else: await query.message.reply_text("Verification failed. Please ensure you have joined the channel.")
                        except (BadRequest, Forbidden) as e:
                            await query.message.reply_text("Bot error: Cannot verify membership."); logger.error(f"Error verifying ad membership for {ad_id}: {e}")
                    else: verification_passed = True
                    
                    if verification_passed:
                        c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (cpc, advertiser_id))
                        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (cpc, user_id))
                        c.execute("INSERT INTO completed_ads (user_id, ad_id, status) VALUES (?, ?, 'completed')", (user_id, ad_id))
                        new_balance = advertiser_balance - cpc
                        ads_to_check = c.execute("SELECT ad_id, cpc, target_id FROM advertisements WHERE user_id = ? AND status = 'active'", (advertiser_id,)).fetchall()
                        paused_ads_to_update = []
                        for ad_id_to_pause, ad_cpc, ad_target_id in ads_to_check:
                            if new_balance < ad_cpc:
                                paused_ads_to_update.append(ad_id_to_pause)
                                paused_ads_info_for_notification.append({'id': ad_id_to_pause, 'target': ad_target_id, 'cpc': ad_cpc})
                        if paused_ads_to_update:
                            placeholders = ','.join('?' for _ in paused_ads_to_update)
                            c.execute(f"UPDATE advertisements SET status = 'paused' WHERE ad_id IN ({placeholders})", paused_ads_to_update)
                            advertiser_id_for_notification = advertiser_id
                        conn.commit()
                        await query.message.reply_text(f"✅ Success! You earned **${cpc:.2f}** for completing the task.", parse_mode='Markdown')
                        await show_next_join_task(update, context)

            # --- LOGIC FOR ADMIN-CREATED TASKS ---
            elif task_type == 'admin':
                if c.execute("SELECT 1 FROM completed_tasks WHERE user_id = ? AND task_id = ?", (user_id, task_id)).fetchone():
                    await query.message.reply_text("You have already completed this task."); return
                
                if action == "skip":
                    c.execute("INSERT OR IGNORE INTO completed_tasks (user_id, task_id) VALUES (?, ?)", (user_id, task_id))
                    conn.commit()
                    await show_next_join_task(update, context)

                elif action == "verify":
                    task_info = c.execute("SELECT reward, target_chat_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                    if not task_info:
                        await query.message.reply_text("This task is no longer available."); await query.message.delete(); return
                    reward, target_chat_id = task_info
                    try:
                        member = await context.bot.get_chat_member(chat_id=target_chat_id, user_id=user_id)
                        if member.status in ['member', 'administrator', 'creator']:
                            c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (reward, user_id))
                            c.execute("INSERT INTO completed_tasks (user_id, task_id) VALUES (?, ?)", (user_id, task_id))
                            conn.commit()
                            await query.message.reply_text(f"✅ Success! You earned **${reward:.2f}** for completing the task.", parse_mode='Markdown')
                            await show_next_join_task(update, context)
                        else:
                            await query.message.reply_text("Verification failed. Please ensure you have joined the channel.")
                    except (BadRequest, Forbidden) as e:
                        await query.message.reply_text("Bot error: Cannot verify membership."); logger.error(f"Error verifying admin task membership for {task_id}: {e}")
        
        if advertiser_id_for_notification and paused_ads_info_for_notification:
            new_balance_final = sqlite3.connect(DB_FILE).cursor().execute("SELECT balance FROM users WHERE user_id = ?", (advertiser_id_for_notification,)).fetchone()[0]
            for ad_info in paused_ads_info_for_notification:
                try:
                    await context.bot.send_message(chat_id=advertiser_id_for_notification, text=f"⚠️ Your ad campaign for `{ad_info['target']}` (ID: {ad_info['id']}) has been automatically paused because your balance (${new_balance_final:.2f}) is too low to cover the next click (${ad_info['cpc']:.2f}).\n\nPlease top up your balance to reactivate it.", parse_mode='Markdown')
                except (Forbidden, BadRequest) as e:
                    logger.warning(f"Could not send low balance notification to advertiser {advertiser_id_for_notification}: {e}")

    elif data.startswith("ad_"):
        await query.answer()
        parts = data.split("_")
        action = "_".join(parts[1:-1])
        if len(parts) < 3 or not parts[-1].isdigit():
             logger.warning(f"Could not parse callback_data: {data}")
             return
        ad_id = int(parts[-1])
        
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            ad_info = c.execute("SELECT user_id, daily_budget FROM advertisements WHERE ad_id = ?", (ad_id,)).fetchone()
            if not ad_info:
                try: await query.message.delete()
                except BadRequest: pass
                return
            ad_owner_id, daily_budget = ad_info
            if user_id != ad_owner_id: return
            if action == 'manage':
                message_content = await generate_ad_management_message(ad_id)
                await query.edit_message_text(text=message_content["text"], reply_markup=message_content["reply_markup"], parse_mode='Markdown')
            elif action in ["activate", "pause"]:
                new_status = 'active' if action == 'activate' else 'paused'
                if new_status == 'active':
                    balance = c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
                    if balance < daily_budget:
                        await context.bot.answer_callback_query(query.id, text=f"Insufficient balance. You need at least ${daily_budget:.2f} to activate.", show_alert=True)
                        return
                c.execute("UPDATE advertisements SET status = ? WHERE ad_id = ?", (new_status, ad_id))
                conn.commit()
                message_content = await generate_ad_management_message(ad_id)
                await query.edit_message_text(text=message_content["text"], reply_markup=message_content["reply_markup"], parse_mode='Markdown')
            elif action == "delete":
                c.execute("UPDATE advertisements SET status = 'deleted' WHERE ad_id = ?", (ad_id,))
                conn.commit()
                await query.edit_message_text(f"✅ Campaign #{ad_id} has been deleted.", reply_markup=None)
            else:
                await context.bot.answer_callback_query(query.id, text=f"Functionality for '{action}' is coming soon!", show_alert=True)
        return
    
    else:
        await query.answer()
        if data == "verify_membership": await check_membership_and_grant_access(update, context, 'verify_membership', 'forced_channels')
        elif data == 'verify_coupon_membership': pass
        elif data == "clear_join_message": await query.message.delete()
        elif data.startswith("approve_") or data.startswith("reject_"):
            action, withdrawal_id = data.split("_")
            with sqlite3.connect(DB_FILE) as conn:
                c = conn.cursor()
                res = c.execute("SELECT user_id, amount FROM withdrawals WHERE withdrawal_id = ? AND status = 'pending'", (withdrawal_id,)).fetchone()
                if not res: return
                w_user_id, amount = res
                if action == "approve":
                    c.execute("UPDATE withdrawals SET status = 'approved' WHERE withdrawal_id = ?", (withdrawal_id,)); conn.commit()
                    await context.bot.send_message(chat_id=w_user_id, text=f"🎉 Your withdrawal request for ${amount:.2f} has been approved!");
                else:
                    c.execute("UPDATE withdrawals SET status = 'rejected' WHERE withdrawal_id = ?", (withdrawal_id,)); c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, w_user_id)); conn.commit()
                    await context.bot.send_message(chat_id=w_user_id, text=f"😔 Your withdrawal for ${amount:.2f} was rejected. Funds returned to your balance.");
            await query.message.delete()
        elif data.startswith("delete_task_"):
            task_id = int(data.split("_")[2])
            with sqlite3.connect(DB_FILE) as conn: conn.cursor().execute("UPDATE tasks SET status = 'deleted' WHERE task_id = ?", (task_id,)); conn.commit()
            await delete_task_list(update, context)
        elif data.startswith("delete_tracked_"):
            ch_id = int(data.split("_")[2])
            with sqlite3.connect(DB_FILE) as conn: conn.cursor().execute("UPDATE forced_channels SET status = 'deleted' WHERE id = ?", (ch_id,)); conn.commit()
            await remove_tracked_channel_list(update, context)
        elif data.startswith("delete_coupon_tracked_"):
            ch_id = int(data.split("_")[3])
            with sqlite3.connect(DB_FILE) as conn: conn.cursor().execute("UPDATE coupon_forced_channels SET status = 'deleted' WHERE id = ?", (ch_id,)); conn.commit()
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

async def cancel_and_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the current conversation and prompts the user to try again."""
    await update.message.reply_text("Previous action canceled. Please click the button you want now.")
    return ConversationHandler.END

async def handle_advertise_menu_and_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the conversation and shows the advertise menu."""
    await handle_advertise_menu(update, context)
    context.user_data.clear()
    return ConversationHandler.END

async def remind_to_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reminds the user to forward a message instead of typing."""
    await update.message.reply_text(
        "Please *forward* a message from the bot, don't send a regular text message. Let's try again.",
        parse_mode='Markdown'
    )

def main() -> None:
    setup_database()
    application = Application.builder().token(BOT_API_KEY).build()
    
    user_menu_buttons = ["💰 Balance", "👥 Referral", "🎁 Daily Bonus", "📋 Tasks", "💸 Withdraw", "🎟️ Coupon Code", "👑 Admin Panel"]
    sub_menu_buttons = ["My Ads", "➕ Create New Ad ➕", "📢 Channel/Group", "🤖 Bot", "🔗 Join Channel/Group", "🤖 Start Bot", "⬅️ Back to Main Menu", "⬅️ Back to Advertise"]
    admin_menu_buttons = ["📧 Mailing", "📋 Task Management", "🎟️ Coupon Management", "📊 Bot Stats", "🏧 Withdrawals", "🔗 Main Track Management", "⬅️ Back to User Menu"]
    all_buttons = user_menu_buttons + sub_menu_buttons + admin_menu_buttons
    any_menu_button_filter = filters.Regex(f"^({'|'.join(all_buttons)})$")
    non_menu_text_filter = filters.TEXT & ~filters.COMMAND & ~any_menu_button_filter

    conv_fallbacks = [CommandHandler("cancel", cancel), MessageHandler(any_menu_button_filter, cancel_and_prompt)]
    
    ad_creation_fallbacks = [
        CommandHandler("cancel", cancel),
        MessageHandler(filters.Regex("^⬅️ Back to Advertise$"), handle_advertise_menu_and_cancel)
    ]

    add_task_conv = ConversationHandler(entry_points=[CallbackQueryHandler(add_task_start, pattern="^admin_add_task_start$")], states={State.GET_TASK_NAME: [MessageHandler(non_menu_text_filter, get_task_name)], State.GET_TARGET_CHAT_ID: [MessageHandler(non_menu_text_filter, get_target_chat_id)], State.GET_TASK_URL: [MessageHandler(non_menu_text_filter, get_task_url)], State.GET_TASK_REWARD: [MessageHandler(non_menu_text_filter, get_task_reward_and_save)]}, fallbacks=conv_fallbacks)
    mailing_conv = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^📧 Mailing$"), mailing_start)], states={State.GET_MAIL_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND & ~any_menu_button_filter, get_mail_message)], State.AWAIT_BUTTON_OR_SEND: [CallbackQueryHandler(await_button_or_send, pattern="^mail_add_button$"), CallbackQueryHandler(broadcast_message, pattern="^mail_send_now$")], State.GET_BUTTON_DATA: [MessageHandler(non_menu_text_filter, get_button_data)]}, fallbacks=conv_fallbacks)
    add_tracked_conv = ConversationHandler(entry_points=[CallbackQueryHandler(add_tracked_channel_start, pattern="^admin_add_tracked_start$")], states={State.GET_TRACKED_NAME: [MessageHandler(non_menu_text_filter, get_tracked_name)], State.GET_TRACKED_ID: [MessageHandler(non_menu_text_filter, get_tracked_id)], State.GET_TRACKED_URL: [MessageHandler(non_menu_text_filter, get_tracked_url_and_save)]}, fallbacks=conv_fallbacks)
    create_coupon_conv = ConversationHandler(entry_points=[CallbackQueryHandler(create_coupon_start, pattern="^admin_create_coupon_start$")], states={State.GET_COUPON_BUDGET: [MessageHandler(non_menu_text_filter, get_coupon_budget)], State.GET_COUPON_MAX_CLAIMS: [MessageHandler(non_menu_text_filter, get_coupon_max_claims_and_save)]}, fallbacks=conv_fallbacks)
    add_coupon_tracked_conv = ConversationHandler(entry_points=[CallbackQueryHandler(add_coupon_tracked_channel_start, pattern="^admin_add_coupon_tracked_start$")], states={State.GET_COUPON_TRACKED_NAME: [MessageHandler(non_menu_text_filter, get_coupon_tracked_name)], State.GET_COUPON_TRACKED_ID: [MessageHandler(non_menu_text_filter, get_coupon_tracked_id)], State.GET_COUPON_TRACKED_URL: [MessageHandler(non_menu_text_filter, get_coupon_tracked_url_and_save)]}, fallbacks=conv_fallbacks)
    withdraw_conv = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_start)], states={State.CHOOSE_WITHDRAW_NETWORK: [CallbackQueryHandler(choose_withdraw_network, pattern="^w_net_")], State.GET_WALLET_ADDRESS: [MessageHandler(non_menu_text_filter, get_wallet_address)], State.GET_WITHDRAW_AMOUNT: [MessageHandler(non_menu_text_filter, get_withdraw_amount)]}, fallbacks=conv_fallbacks)
    claim_coupon_conv = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^🎟️ Coupon Code$"), claim_coupon_start)], states={State.AWAIT_COUPON_CODE: [MessageHandler(non_menu_text_filter, receive_coupon_code), CallbackQueryHandler(claim_coupon_start, pattern="^verify_coupon_membership$")]}, fallbacks=conv_fallbacks)
    
    advertise_channel_conv = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^📢 Channel/Group$"), advertise_channel_start)], states={State.GET_AD_LINK: [MessageHandler(non_menu_text_filter, get_ad_link)], State.AWAIT_ADMIN_CONFIRMATION: [CallbackQueryHandler(handle_admin_confirmation, pattern="^ad_admin_")], State.GET_AD_DESCRIPTION: [MessageHandler(non_menu_text_filter, get_ad_description)], State.GET_AD_CPC: [MessageHandler(non_menu_text_filter, get_ad_cpc)], State.GET_AD_BUDGET: [MessageHandler(non_menu_text_filter, get_ad_budget_and_save)]}, fallbacks=ad_creation_fallbacks)
    
    advertise_bot_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🤖 Bot$"), advertise_bot_start)],
        states={
            State.GET_BOT_FORWARD: [
                MessageHandler(filters.FORWARDED & ~filters.COMMAND & ~any_menu_button_filter, get_bot_forward),
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~any_menu_button_filter, remind_to_forward)
            ],
            State.GET_BOT_LINK: [MessageHandler(non_menu_text_filter, get_bot_link)],
            State.GET_BOT_AD_DESCRIPTION: [MessageHandler(non_menu_text_filter, get_bot_description)],
            State.GET_BOT_AD_CPC: [MessageHandler(non_menu_text_filter, get_bot_cpc)],
            State.GET_BOT_AD_BUDGET: [MessageHandler(non_menu_text_filter, get_bot_budget_and_save)],
        },
        fallbacks=ad_creation_fallbacks
    )
    # Define the new conversation handler for bot verification
    bot_verification_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_bot_task_verification, pattern="^task_start_verify_")],
        states={
            State.AWAIT_BOT_TASK_VERIFICATION: [
                MessageHandler(filters.FORWARDED, handle_bot_task_verification),
                MessageHandler(filters.TEXT & ~filters.COMMAND, incorrect_verification_message)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), handle_balance))
    application.add_handler(MessageHandler(filters.Regex("^👥 Referral$"), handle_referral))
    application.add_handler(MessageHandler(filters.Regex("^🎁 Daily Bonus$"), handle_daily_bonus))
    application.add_handler(MessageHandler(filters.Regex("^📋 Tasks$"), handle_tasks_menu))
    application.add_handler(MessageHandler(filters.Regex("^📢 Advertise$"), handle_advertise_menu))
    application.add_handler(MessageHandler(filters.Regex("^⬅️ Back to Main Menu$"), handle_back_to_main_menu))
    application.add_handler(MessageHandler(filters.Regex("^⬅️ Back to Advertise$"), handle_advertise_menu))
    application.add_handler(MessageHandler(filters.Regex("^🔗 Join Channel/Group$"), start_join_tasks_flow))
    application.add_handler(MessageHandler(filters.Regex("^🤖 Start Bot$"), start_bot_tasks_flow)) 
    application.add_handler(MessageHandler(filters.Regex("^My Ads$"), handle_my_ads))
    application.add_handler(MessageHandler(filters.Regex("^➕ Create New Ad ➕$"), handle_create_new_ad))
    application.add_handler(MessageHandler(filters.Regex("^👑 Admin Panel$"), admin_panel_start))
    application.add_handler(MessageHandler(filters.Regex("^⬅️ Back to User Menu$"), admin_back_to_user_menu))
    application.add_handler(MessageHandler(filters.Regex("^📋 Task Management$"), handle_admin_tasks))
    application.add_handler(MessageHandler(filters.Regex("^📊 Bot Stats$"), handle_admin_stats))
    application.add_handler(MessageHandler(filters.Regex("^🏧 Withdrawals$"), handle_admin_withdrawals))
    application.add_handler(MessageHandler(filters.Regex("^🔗 Main Track Management$"), handle_admin_tracking))
    application.add_handler(MessageHandler(filters.Regex("^🎟️ Coupon Management$"), handle_coupon_management))
    
    application.add_handler(add_task_conv)
    application.add_handler(withdraw_conv)
    application.add_handler(mailing_conv)
    application.add_handler(add_tracked_conv)
    application.add_handler(create_coupon_conv)
    application.add_handler(add_coupon_tracked_conv)
    application.add_handler(claim_coupon_conv)
    application.add_handler(advertise_channel_conv)
    application.add_handler(advertise_bot_conv)
    application.add_handler(bot_verification_conv)
    
    application.add_handler(CallbackQueryHandler(delete_task_list, pattern="^admin_delete_task_list$"))
    application.add_handler(CallbackQueryHandler(remove_tracked_channel_list, pattern="^admin_remove_tracked_list$"))
    application.add_handler(CallbackQueryHandler(remove_coupon_tracked_channel_list, pattern="^admin_remove_coupon_tracked_list$"))
    application.add_handler(CallbackQueryHandler(button_callback_handler))

    logger.info("Bot is starting up...")
    application.run_polling()
    logger.info("Bot has been shut down.")

if __name__ == "__main__":
    main()

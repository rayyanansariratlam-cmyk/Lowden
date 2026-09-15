import os
import sqlite3
import logging
from datetime import datetime, timezone
from functools import wraps

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8845430055:AAG5-uyA8PDH0THS1MUfbiJ9t7_Sd2vcItI")

# Put your Telegram numeric admin ID(s) here.
# Example: ADMIN_IDS = {123456789}
ADMIN_IDS = {8918387373}

DB_FILE = "referral_bot.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    balance REAL DEFAULT 0,
    referral_count INTEGER DEFAULT 0,
    referred_by INTEGER DEFAULT NULL,
    joined_at TEXT DEFAULT '',
    banned INTEGER DEFAULT 0,
    last_daily_bonus TEXT DEFAULT ''
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    method TEXT NOT NULL,
    account TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT '',
    processed_at TEXT DEFAULT ''
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    amount REAL NOT NULL,
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
)
""")

db.commit()


DEFAULT_SETTINGS = {
    "referral_reward": "5",
    "welcome_bonus": "0",
    "daily_bonus": "2",
    "minimum_withdrawal": "100",
    "required_channel": "@Rayyan_Hacks",
    "support_username": "@Rayyan_Hacks",
    "referral_enabled": "1",
    "daily_bonus_enabled": "1",
    "withdrawal_enabled": "1",
    "maintenance": "0",
}


def init_settings():
    for key, value in DEFAULT_SETTINGS.items():
        db.execute(
            "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
            (key, value),
        )
    db.commit()


init_settings()


def get_setting(key):
    row = db.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,),
    ).fetchone()

    if not row:
        return DEFAULT_SETTINGS.get(key, "")

    return row["value"]


def set_setting(key, value):
    db.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
        (key, str(value)),
    )
    db.commit()


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# HELPERS
# ============================================================

def is_admin(user_id):
    return user_id in ADMIN_IDS


def admin_only(func):
    @wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        user = update.effective_user

        if not user or not is_admin(user.id):
            if update.callback_query:
                await update.callback_query.answer(
                    "❌ Admin access required.",
                    show_alert=True,
                )
            return

        return await func(update, context, *args, **kwargs)

    return wrapper


def get_user(user_id):
    return db.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()


def create_user(user, referred_by=None):
    existing = get_user(user.id)

    if existing:
        db.execute("""
            UPDATE users
            SET username=?, first_name=?
            WHERE user_id=?
        """, (
            user.username or "",
            user.first_name or "",
            user.id,
        ))
        db.commit()
        return False

    db.execute("""
        INSERT INTO users(
            user_id,
            username,
            first_name,
            balance,
            referral_count,
            referred_by,
            joined_at
        )
        VALUES(?,?,?,?,?,?,?)
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
        0,
        0,
        referred_by,
        now(),
    ))

    db.commit()

    # Welcome bonus
    welcome = float(get_setting("welcome_bonus"))

    if welcome > 0:
        add_balance(
            user.id,
            welcome,
            "Welcome bonus",
            "welcome",
        )

    # Referral reward
    if referred_by and referred_by != user.id:
        referral_enabled = get_setting("referral_enabled") == "1"

        if referral_enabled and get_user(referred_by):
            reward = float(get_setting("referral_reward"))

            if reward > 0:
                add_balance(
                    referred_by,
                    reward,
                    f"Referral reward for {user.id}",
                    "referral",
                )

                db.execute("""
                    UPDATE users
                    SET referral_count=referral_count+1
                    WHERE user_id=?
                """, (referred_by,))

                db.commit()

    return True


def add_balance(user_id, amount, note="", tx_type="manual"):
    db.execute("""
        UPDATE users
        SET balance = balance + ?
        WHERE user_id=?
    """, (amount, user_id))

    db.execute("""
        INSERT INTO transactions(
            user_id,
            type,
            amount,
            note,
            created_at
        )
        VALUES(?,?,?,?,?)
    """, (
        user_id,
        tx_type,
        amount,
        note,
        now(),
    ))

    db.commit()


def subtract_balance(user_id, amount, note="", tx_type="withdrawal"):
    db.execute("""
        UPDATE users
        SET balance = balance - ?
        WHERE user_id=?
    """, (amount, user_id))

    db.execute("""
        INSERT INTO transactions(
            user_id,
            type,
            amount,
            note,
            created_at
        )
        VALUES(?,?,?,?,?)
    """, (
        user_id,
        tx_type,
        -amount,
        note,
        now(),
    ))

    db.commit()


async def safe_edit(query, text, keyboard=None):
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        try:
            await query.message.reply_text(
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# ============================================================
# CHANNEL JOIN
# ============================================================

async def check_channel_member(bot, user_id):
    channel = get_setting("required_channel")

    if not channel or channel == "@Rayyan_Hacks":
        return True

    try:
        member = await bot.get_chat_member(channel, user_id)

        return member.status in (
            "creator",
            "administrator",
            "member",
        )

    except Exception as e:
        logger.warning("Channel check failed: %s", e)
        return False


def join_keyboard():
    channel = get_setting("required_channel")

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 Join Channel",
                url=f"https://t.me/{channel.lstrip('@')}",
            )
        ],
        [
            InlineKeyboardButton(
                "✅ Verify Join",
                callback_data="verify_join",
            )
        ],
    ])


# ============================================================
# USER UI
# ============================================================

def main_keyboard(user_id):
    rows = [
        [
            InlineKeyboardButton("💰 Balance", callback_data="balance"),
            InlineKeyboardButton("💸 Withdrawal", callback_data="withdraw"),
        ],
        [
            InlineKeyboardButton("🎁 Bonus", callback_data="bonus"),
            InlineKeyboardButton("👥 Referral", callback_data="referral"),
        ],
        [
            InlineKeyboardButton("🎟 Redeem", callback_data="redeem"),
            InlineKeyboardButton("🆘 Support", callback_data="support"),
        ],
    ]

    if is_admin(user_id):
        rows.append([
            InlineKeyboardButton(
                "👑 Admin Panel",
                callback_data="admin_panel",
            )
        ])

    return InlineKeyboardMarkup(rows)


def home_text(user):
    row = get_user(user.id)

    balance = row["balance"] if row else 0
    referrals = row["referral_count"] if row else 0

    return (
        f"👋 <b>Welcome, {user.first_name}</b>\n\n"
        f"💰 Balance: <b>₹{balance:.2f}</b>\n"
        f"👥 Referrals: <b>{referrals}</b>\n\n"
        f"Choose an option below."
    )


async def show_home(update, context):
    user = update.effective_user

    if get_setting("maintenance") == "1" and not is_admin(user.id):
        text = (
            "🔧 <b>Maintenance Mode</b>\n\n"
            "Bot is temporarily unavailable.\n"
            "Please try again later."
        )

        if update.callback_query:
            await safe_edit(update.callback_query, text)
        else:
            await update.message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
            )
        return

    if not get_user(user.id):
        create_user(user)

    if update.callback_query:
        await safe_edit(
            update.callback_query,
            home_text(user),
            main_keyboard(user.id),
        )
    else:
        await update.message.reply_text(
            home_text(user),
            reply_markup=main_keyboard(user.id),
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    referred_by = None

    if context.args:
        arg = context.args[0]

        if arg.startswith("ref_"):
            try:
                referred_by = int(arg.replace("ref_", ""))
            except ValueError:
                referred_by = None

    is_new = create_user(user, referred_by)

    if is_admin(user.id):
        await show_home(update, context)
        return

    if not await check_channel_member(
        context.bot,
        user.id,
    ):
        await update.message.reply_text(
            "🔐 <b>Join Required</b>\n\n"
            "Please join our channel first.\n"
            "After joining, press <b>Verify Join</b>.",
            reply_markup=join_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    await show_home(update, context)


# ============================================================
# USER CALLBACKS
# ============================================================

async def user_callback(update, context):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    data = query.data

    if data == "verify_join":
        if await check_channel_member(
            context.bot,
            user.id,
        ):
            await safe_edit(
                query,
                home_text(user),
                main_keyboard(user.id),
            )
        else:
            await query.answer(
                "❌ You have not joined the channel yet.",
                show_alert=True,
            )
        return

    if data == "home":
        await show_home(update, context)
        return

    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    if data == "balance":
        row = get_user(user.id)

        text = (
            "💰 <b>Your Balance</b>\n\n"
            f"Available Balance: <b>₹{row['balance']:.2f}</b>\n"
            f"Total Referrals: <b>{row['referral_count']}</b>"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="home")]
        ])

        await safe_edit(query, text, keyboard)
        return

    # --------------------------------------------------------
    # REFERRAL
    # --------------------------------------------------------

    if data == "referral":
        if get_setting("referral_enabled") != "1":
            await query.answer(
                "Referral system is currently disabled.",
                show_alert=True,
            )
            return

        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{user.id}"

        row = get_user(user.id)

        text = (
            "👥 <b>Referral Program</b>\n\n"
            f"💰 Reward: <b>₹{float(get_setting('referral_reward')):.2f}</b>\n"
            f"👤 Your Referrals: <b>{row['referral_count']}</b>\n\n"
            "🔗 <b>Your Referral Link</b>\n"
            f"<code>{link}</code>\n\n"
            "Share this link with your friends."
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📤 Share Link",
                    url=(
                        "https://t.me/share/url"
                        f"?url={link}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="home",
                )
            ],
        ])

        await safe_edit(query, text, keyboard)
        return

    # --------------------------------------------------------
    # BONUS
    # --------------------------------------------------------

    if data == "bonus":
        if get_setting("daily_bonus_enabled") != "1":
            await query.answer(
                "Daily bonus is disabled.",
                show_alert=True,
            )
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🎁 Claim Bonus",
                    callback_data="claim_bonus",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="home",
                )
            ],
        ])

        text = (
            "🎁 <b>Daily Bonus</b>\n\n"
            f"Today's bonus: "
            f"<b>₹{float(get_setting('daily_bonus')):.2f}</b>\n\n"
            "You can claim it once every 24 hours."
        )

        await safe_edit(query, text, keyboard)
        return

    if data == "claim_bonus":
        row = get_user(user.id)

        if not row:
            create_user(user)
            row = get_user(user.id)

        last = row["last_daily_bonus"]

        if last:
            try:
                last_dt = datetime.strptime(
                    last,
                    "%Y-%m-%d %H:%M:%S",
                ).replace(tzinfo=timezone.utc)

                elapsed = (
                    datetime.now(timezone.utc) - last_dt
                ).total_seconds()

                if elapsed < 86400:
                    remaining = int(86400 - elapsed)
                    hours = remaining // 3600
                    minutes = (remaining % 3600) // 60

                    await query.answer(
                        f"⏳ Try again in {hours}h {minutes}m.",
                        show_alert=True,
                    )
                    return

            except Exception:
                pass

        amount = float(get_setting("daily_bonus"))

        add_balance(
            user.id,
            amount,
            "Daily bonus",
            "daily_bonus",
        )

        db.execute("""
            UPDATE users
            SET last_daily_bonus=?
            WHERE user_id=?
        """, (now(), user.id))
        db.commit()

        await query.answer(
            f"🎉 ₹{amount:.2f} bonus added!",
            show_alert=True,
        )

        await show_home(update, context)
        return

    # --------------------------------------------------------
    # SUPPORT
    # --------------------------------------------------------

    if data == "support":
        support = get_setting("support_username")

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💬 Contact Support",
                    url=f"https://t.me/{support.lstrip('@')}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="home",
                )
            ],
        ])

        await safe_edit(
            query,
            (
                "🆘 <b>Support</b>\n\n"
                "Need help?\n"
                "Contact our support team."
            ),
            keyboard,
        )
        return

    # --------------------------------------------------------
    # REDEEM
    # --------------------------------------------------------

    if data == "redeem":
        context.user_data["state"] = "redeem"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="home",
                )
            ]
        ])

        await safe_edit(
            query,
            (
                "🎟 <b>Redeem Code</b>\n\n"
                "Send your redeem code below."
            ),
            keyboard,
        )
        return

    # --------------------------------------------------------
    # WITHDRAW
    # --------------------------------------------------------

    if data == "withdraw":
        if get_setting("withdrawal_enabled") != "1":
            await query.answer(
                "Withdrawals are currently disabled.",
                show_alert=True,
            )
            return

        row = get_user(user.id)
        minimum = float(get_setting("minimum_withdrawal"))

        if row["balance"] < minimum:
            await query.answer(
                f"Minimum withdrawal is ₹{minimum:.2f}.",
                show_alert=True,
            )
            return

        context.user_data["state"] = "withdraw_amount"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="home",
                )
            ]
        ])

        await safe_edit(
            query,
            (
                "💸 <b>Withdrawal</b>\n\n"
                f"Minimum: <b>₹{minimum:.2f}</b>\n"
                f"Available: <b>₹{row['balance']:.2f}</b>\n\n"
                "Send the amount you want to withdraw."
            ),
            keyboard,
        )
        return


# ============================================================
# ADMIN PANEL
# ============================================================

def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 Users",
                callback_data="a_users",
            ),
            InlineKeyboardButton(
                "💰 Balance",
                callback_data="a_balance",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎁 Bonus",
                callback_data="a_bonus",
            ),
            InlineKeyboardButton(
                "👥 Referral",
                callback_data="a_referral",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎁 Send Gift",
                callback_data="a_gift",
            ),
            InlineKeyboardButton(
                "💸 Withdrawals",
                callback_data="a_withdrawals",
            ),
        ],
        [
            InlineKeyboardButton(
                "📢 Broadcast",
                callback_data="a_broadcast",
            ),
            InlineKeyboardButton(
                "📊 Statistics",
                callback_data="a_stats",
            ),
        ],
        [
            InlineKeyboardButton(
                "⚙️ Settings",
                callback_data="a_settings",
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Main Menu",
                callback_data="home",
            )
        ],
    ])


@admin_only
async def admin_panel(update, context):
    query = update.callback_query

    text = (
        "👑 <b>Admin Control Panel</b>\n\n"
        "Manage users, balances, rewards, withdrawals "
        "and bot settings from here."
    )

    await safe_edit(
        query,
        text,
        admin_keyboard(),
    )


# ============================================================
# ADMIN CALLBACKS
# ============================================================

@admin_only
async def admin_callback(update, context):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = update.effective_user.id

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    if data == "a_users":
        total = db.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]

        banned = db.execute(
            "SELECT COUNT(*) c FROM users WHERE banned=1"
        ).fetchone()["c"]

        text = (
            "👥 <b>Users</b>\n\n"
            f"👤 Total Users: <b>{total}</b>\n"
            f"🚫 Banned Users: <b>{banned}</b>\n\n"
            "Use commands:\n"
            "<code>/user USER_ID</code>\n"
            "<code>/ban USER_ID</code>\n"
            "<code>/unban USER_ID</code>"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Admin Panel",
                    callback_data="admin_panel",
                )
            ]
        ])

        await safe_edit(query, text, keyboard)
        return

    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    if data == "a_balance":
        context.user_data["state"] = "admin_balance"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="admin_panel",
                )
            ]
        ])

        await safe_edit(
            query,
            (
                "💰 <b>Manage Balance</b>\n\n"
                "Send in this format:\n\n"
                "<code>USER_ID AMOUNT</code>\n\n"
                "Example:\n"
                "<code>123456789 50</code>\n\n"
                "Positive amount = add\n"
                "Negative amount = remove"
            ),
            keyboard,
        )
        return

    # --------------------------------------------------------
    # BONUS
    # --------------------------------------------------------

    if data == "a_bonus":
        context.user_data["state"] = "admin_bonus"

        daily = get_setting("daily_bonus")
        welcome = get_setting("welcome_bonus")

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🎁 Daily Bonus",
                    callback_data="set_daily",
                )
            ],
            [
                InlineKeyboardButton(
                    "👋 Welcome Bonus",
                    callback_data="set_welcome",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Admin Panel",
                    callback_data="admin_panel",
                )
            ],
        ])

        text = (
            "🎁 <b>Bonus Settings</b>\n\n"
            f"Daily Bonus: <b>₹{float(daily):.2f}</b>\n"
            f"Welcome Bonus: <b>₹{float(welcome):.2f}</b>"
        )

        await safe_edit(query, text, keyboard)
        return

    # --------------------------------------------------------
    # REFERRAL
    # --------------------------------------------------------

    if data == "a_referral":
        reward = float(get_setting("referral_reward"))
        enabled = get_setting("referral_enabled") == "1"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✏️ Change Reward",
                    callback_data="set_referral_reward",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 Enable / Disable",
                    callback_data="toggle_referral",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Admin Panel",
                    callback_data="admin_panel",
                )
            ],
        ])

        text = (
            "👥 <b>Referral Settings</b>\n\n"
            f"Reward: <b>₹{reward:.2f}</b>\n"
            f"Status: <b>{'ON 🟢' if enabled else 'OFF 🔴'}</b>"
        )

        await safe_edit(query, text, keyboard)
        return

    if data == "set_referral_reward":
        context.user_data["state"] = "set_referral_reward"

        await safe_edit(
            query,
            (
                "👥 <b>Referral Reward</b>\n\n"
                "Send the new reward amount.\n\n"
                "Example: <code>10</code>"
            ),
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="a_referral",
                    )
                ]
            ]),
        )
        return

    if data == "toggle_referral":
        current = get_setting("referral_enabled")

        set_setting(
            "referral_enabled",
            "0" if current == "1" else "1",
        )

        await query.answer(
            "Referral setting updated.",
            show_alert=True,
        )

        await admin_callback(update, context)
        return

    # --------------------------------------------------------
    # BONUS SETTERS
    # --------------------------------------------------------

    if data == "set_daily":
        context.user_data["state"] = "set_daily"

        await safe_edit(
            query,
            "🎁 Send new daily bonus amount:",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="a_bonus",
                    )
                ]
            ]),
        )
        return

    if data == "set_welcome":
        context.user_data["state"] = "set_welcome"

        await safe_edit(
            query,
            "👋 Send new welcome bonus amount:",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="a_bonus",
                    )
                ]
            ]),
        )
        return

    # --------------------------------------------------------
    # SEND GIFT
    # --------------------------------------------------------

    if data == "a_gift":
        context.user_data["state"] = "admin_gift"

        await safe_edit(
            query,
            (
                "🎁 <b>Send Gift</b>\n\n"
                "Send:\n"
                "<code>USER_ID AMOUNT</code>\n\n"
                "Example:\n"
                "<code>123456789 100</code>"
            ),
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="admin_panel",
                    )
                ]
            ]),
        )
        return

    # --------------------------------------------------------
    # WITHDRAWALS
    # --------------------------------------------------------

    if data == "a_withdrawals":
        rows = db.execute("""
            SELECT *
            FROM withdrawals
            WHERE status='pending'
            ORDER BY id DESC
            LIMIT 10
        """).fetchall()

        if not rows:
            text = "💸 <b>Withdrawals</b>\n\nNo pending withdrawals."
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Admin Panel",
                        callback_data="admin_panel",
                    )
                ]
            ])
            await safe_edit(query, text, keyboard)
            return

        buttons = []

        for row in rows:
            buttons.append([
                InlineKeyboardButton(
                    f"#{row['id']} • ₹{row['amount']:.2f}",
                    callback_data=f"wd_{row['id']}",
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                "⬅️ Admin Panel",
                callback_data="admin_panel",
            )
        ])

        await safe_edit(
            query,
            "💸 <b>Pending Withdrawals</b>",
            InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("wd_"):
        withdrawal_id = int(data.split("_")[1])

        row = db.execute(
            "SELECT * FROM withdrawals WHERE id=?",
            (withdrawal_id,),
        ).fetchone()

        if not row:
            await query.answer(
                "Withdrawal not found.",
                show_alert=True,
            )
            return

        text = (
            f"💸 <b>Withdrawal #{row['id']}</b>\n\n"
            f"👤 User: <code>{row['user_id']}</code>\n"
            f"💰 Amount: <b>₹{row['amount']:.2f}</b>\n"
            f"🏦 Method: <b>{row['method']}</b>\n"
            f"📋 Account: <code>{row['account']}</code>\n"
            f"📅 Date: <b>{row['created_at']}</b>"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"approve_{row['id']}",
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=f"reject_{row['id']}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="a_withdrawals",
                )
            ],
        ])

        await safe_edit(query, text, keyboard)
        return

    if data.startswith("approve_"):
        withdrawal_id = int(data.split("_")[1])

        row = db.execute(
            "SELECT * FROM withdrawals WHERE id=?",
            (withdrawal_id,),
        ).fetchone()

        if not row or row["status"] != "pending":
            await query.answer(
                "Already processed.",
                show_alert=True,
            )
            return

        db.execute("""
            UPDATE withdrawals
            SET status='approved', processed_at=?
            WHERE id=?
        """, (now(), withdrawal_id))
        db.commit()

        try:
            await context.bot.send_message(
                row["user_id"],
                (
                    "✅ <b>Withdrawal Approved</b>\n\n"
                    f"Amount: <b>₹{row['amount']:.2f}</b>\n"
                    "Your withdrawal has been approved."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        await query.answer(
            "Withdrawal approved.",
            show_alert=True,
        )

        await admin_callback(update, context)
        return

    if data.startswith("reject_"):
        withdrawal_id = int(data.split("_")[1])

        row = db.execute(
            "SELECT * FROM withdrawals WHERE id=?",
            (withdrawal_id,),
        ).fetchone()

        if not row or row["status"] != "pending":
            await query.answer(
                "Already processed.",
                show_alert=True,
            )
            return

        db.execute("""
            UPDATE withdrawals
            SET status='rejected', processed_at=?
            WHERE id=?
        """, (now(), withdrawal_id))

        add_balance(
            row["user_id"],
            row["amount"],
            f"Refund for rejected withdrawal #{withdrawal_id}",
            "withdrawal_refund",
        )

        db.commit()

        try:
            await context.bot.send_message(
                row["user_id"],
                (
                    "❌ <b>Withdrawal Rejected</b>\n\n"
                    f"Amount: <b>₹{row['amount']:.2f}</b>\n"
                    "The amount has been refunded to your balance."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        await query.answer(
            "Withdrawal rejected and refunded.",
            show_alert=True,
        )

        await admin_callback(update, context)
        return

    # --------------------------------------------------------
    # BROADCAST
    # --------------------------------------------------------

    if data == "a_broadcast":
        context.user_data["state"] = "admin_broadcast"

        await safe_edit(
            query,
            (
                "📢 <b>Broadcast</b>\n\n"
                "Send the message you want to broadcast "
                "to all users.\n\n"
                "You can use normal Telegram text formatting."
            ),
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="admin_panel",
                    )
                ]
            ]),
        )
        return

    # --------------------------------------------------------
    # STATS
    # --------------------------------------------------------

    if data == "a_stats":
        total = db.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]

        banned = db.execute(
            "SELECT COUNT(*) c FROM users WHERE banned=1"
        ).fetchone()["c"]

        total_balance = db.execute(
            "SELECT COALESCE(SUM(balance),0) s FROM users"
        ).fetchone()["s"]

        pending = db.execute(
            "SELECT COUNT(*) c FROM withdrawals WHERE status='pending'"
        ).fetchone()["c"]

        approved = db.execute(
            "SELECT COUNT(*) c FROM withdrawals WHERE status='approved'"
        ).fetchone()["c"]

        text = (
            "📊 <b>Bot Statistics</b>\n\n"
            f"👥 Users: <b>{total}</b>\n"
            f"🚫 Banned: <b>{banned}</b>\n"
            f"💰 User Balance: <b>₹{total_balance:.2f}</b>\n"
            f"⏳ Pending Withdrawals: <b>{pending}</b>\n"
            f"✅ Approved Withdrawals: <b>{approved}</b>"
        )

        await safe_edit(
            query,
            text,
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Admin Panel",
                        callback_data="admin_panel",
                    )
                ]
            ]),
        )
        return

    # --------------------------------------------------------
    # SETTINGS
    # --------------------------------------------------------

    if data == "a_settings":
        channel = get_setting("required_channel")
        support = get_setting("support_username")
        minimum = get_setting("minimum_withdrawal")
        maintenance = get_setting("maintenance") == "1"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📢 Channel",
                    callback_data="set_channel",
                )
            ],
            [
                InlineKeyboardButton(
                    "🆘 Support",
                    callback_data="set_support",
                )
            ],
            [
                InlineKeyboardButton(
                    "💸 Minimum Withdrawal",
                    callback_data="set_minimum",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔧 Maintenance ON/OFF",
                    callback_data="toggle_maintenance",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Admin Panel",
                    callback_data="admin_panel",
                )
            ],
        ])

        text = (
            "⚙️ <b>Bot Settings</b>\n\n"
            f"📢 Channel: <code>{channel}</code>\n"
            f"🆘 Support: <code>{support}</code>\n"
            f"💸 Minimum Withdrawal: <b>₹{float(minimum):.2f}</b>\n"
            f"🔧 Maintenance: "
            f"<b>{'ON 🔴' if maintenance else 'OFF 🟢'}</b>"
        )

        await safe_edit(query, text, keyboard)
        return

    if data == "set_channel":
        context.user_data["state"] = "set_channel"

        await safe_edit(
            query,
            (
                "📢 <b>Required Channel</b>\n\n"
                "Send channel username.\n\n"
                "Example:\n"
                "<code>@mychannel</code>\n\n"
                "Make sure the bot is an admin/member of the channel "
                "so it can verify users."
            ),
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="a_settings",
                    )
                ]
            ]),
        )
        return

    if data == "set_support":
        context.user_data["state"] = "set_support"

        await safe_edit(
            query,
            (
                "🆘 <b>Support Username</b>\n\n"
                "Send support username.\n\n"
                "Example:\n"
                "<code>@support</code>"
            ),
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="a_settings",
                    )
                ]
            ]),
        )
        return

    if data == "set_minimum":
        context.user_data["state"] = "set_minimum"

        await safe_edit(
            query,
            "💸 Send minimum withdrawal amount:",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="a_settings",
                    )
                ]
            ]),
        )
        return

    if data == "toggle_maintenance":
        current = get_setting("maintenance")

        set_setting(
            "maintenance",
            "0" if current == "1" else "1",
        )

        await query.answer(
            "Maintenance mode updated.",
            show_alert=True,
        )

        await admin_callback(update, context)
        return


# ============================================================
# ADMIN COMMANDS
# ============================================================

@admin_only
async def admin_command(update, context):
    await update.message.reply_text(
        (
            "👑 <b>Admin Panel</b>\n\n"
            "Use the button below."
        ),
        reply_markup=admin_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def user_info(update, context):
    if not context.args:
        await update.message.reply_text(
            "Usage: /user USER_ID"
        )
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    row = get_user(uid)

    if not row:
        await update.message.reply_text(
            "❌ User not found."
        )
        return

    await update.message.reply_text(
        (
            "👤 <b>User Details</b>\n\n"
            f"ID: <code>{row['user_id']}</code>\n"
            f"Username: @{row['username'] or 'none'}\n"
            f"Name: {row['first_name']}\n"
            f"Balance: <b>₹{row['balance']:.2f}</b>\n"
            f"Referrals: <b>{row['referral_count']}</b>\n"
            f"Banned: <b>{'YES' if row['banned'] else 'NO'}</b>\n"
            f"Joined: {row['joined_at']}"
        ),
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def ban_user(update, context):
    if not context.args:
        await update.message.reply_text(
            "Usage: /ban USER_ID"
        )
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    if not get_user(uid):
        await update.message.reply_text(
            "❌ User not found."
        )
        return

    db.execute(
        "UPDATE users SET banned=1 WHERE user_id=?",
        (uid,),
    )
    db.commit()

    await update.message.reply_text(
        f"🚫 User <code>{uid}</code> banned.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def unban_user(update, context):
    if not context.args:
        await update.message.reply_text(
            "Usage: /unban USER_ID"
        )
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    db.execute(
        "UPDATE users SET banned=0 WHERE user_id=?",
        (uid,),
    )
    db.commit()

    await update.message.reply_text(
        f"✅ User <code>{uid}</code> unbanned.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# TEXT INPUT HANDLER
# ============================================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user:
        return

    row = get_user(user.id)

    if row and row["banned"] and not is_admin(user.id):
        await update.message.reply_text(
            "🚫 Your account is banned."
        )
        return

    state = context.user_data.get("state")

    if not state:
        return

    text = update.message.text.strip()

    # --------------------------------------------------------
    # ADMIN BALANCE
    # --------------------------------------------------------

    if state == "admin_balance" and is_admin(user.id):
        try:
            uid, amount = text.split()
            uid = int(uid)
            amount = float(amount)

            target = get_user(uid)

            if not target:
                await update.message.reply_text(
                    "❌ User not found."
                )
                return

            if amount < 0:
                remove_amount = abs(amount)

                if target["balance"] < remove_amount:
                    await update.message.reply_text(
                        "❌ User does not have enough balance."
                    )
                    return

                subtract_balance(
                    uid,
                    remove_amount,
                    "Admin balance deduction",
                    "admin_remove",
                )

            else:
                add_balance(
                    uid,
                    amount,
                    "Admin balance addition",
                    "admin_add",
                )

            context.user_data.pop("state", None)

            await update.message.reply_text(
                (
                    "✅ <b>Balance Updated</b>\n\n"
                    f"User: <code>{uid}</code>\n"
                    f"Change: <b>₹{amount:.2f}</b>"
                ),
                parse_mode=ParseMode.HTML,
            )

        except Exception:
            await update.message.reply_text(
                "❌ Format:\n<code>USER_ID AMOUNT</code>",
                parse_mode=ParseMode.HTML,
            )

        return

    # --------------------------------------------------------
    # ADMIN GIFT
    # --------------------------------------------------------

    if state == "admin_gift" and is_admin(user.id):
        try:
            uid, amount = text.split()
            uid = int(uid)
            amount = float(amount)

            if amount <= 0:
                raise ValueError

            if not get_user(uid):
                await update.message.reply_text(
                    "❌ User not found."
                )
                return

            add_balance(
                uid,
                amount,
                "Gift from admin",
                "gift",
            )

            context.user_data.pop("state", None)

            await update.message.reply_text(
                (
                    "🎁 <b>Gift Sent</b>\n\n"
                    f"User: <code>{uid}</code>\n"
                    f"Amount: <b>₹{amount:.2f}</b>"
                ),
                parse_mode=ParseMode.HTML,
            )

            try:
                await context.bot.send_message(
                    uid,
                    (
                        "🎁 <b>You received a gift!</b>\n\n"
                        f"Amount: <b>₹{amount:.2f}</b>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        except Exception:
            await update.message.reply_text(
                "❌ Format:\n<code>USER_ID AMOUNT</code>",
                parse_mode=ParseMode.HTML,
            )

        return

    # --------------------------------------------------------
    # ADMIN BROADCAST
    # --------------------------------------------------------

    if state == "admin_broadcast" and is_admin(user.id):
        users = db.execute(
            "SELECT user_id FROM users WHERE banned=0"
        ).fetchall()

        sent = 0
        failed = 0

        await update.message.reply_text(
            f"📢 Broadcasting to {len(users)} users..."
        )

        for target in users:
            try:
                await context.bot.send_message(
                    target["user_id"],
                    text,
                )
                sent += 1
            except Exception:
                failed += 1

        context.user_data.pop("state", None)

        await update.message.reply_text(
            (
                "📢 <b>Broadcast Complete</b>\n\n"
                f"✅ Sent: <b>{sent}</b>\n"
                f"❌ Failed: <b>{failed}</b>"
            ),
            parse_mode=ParseMode.HTML,
        )

        return

    # --------------------------------------------------------
    # REFERRAL REWARD
    # --------------------------------------------------------

    if state == "set_referral_reward" and is_admin(user.id):
        try:
            amount = float(text)

            if amount < 0:
                raise ValueError

            set_setting("referral_reward", amount)
            context.user_data.pop("state", None)

            await update.message.reply_text(
                f"✅ Referral reward set to ₹{amount:.2f}"
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Enter a valid amount."
            )

        return

    # --------------------------------------------------------
    # DAILY BONUS
    # --------------------------------------------------------

    if state == "set_daily" and is_admin(user.id):
        try:
            amount = float(text)

            if amount < 0:
                raise ValueError

            set_setting("daily_bonus", amount)
            context.user_data.pop("state", None)

            await update.message.reply_text(
                f"✅ Daily bonus set to ₹{amount:.2f}"
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Enter a valid amount."
            )

        return

    # --------------------------------------------------------
    # WELCOME BONUS
    # --------------------------------------------------------

    if state == "set_welcome" and is_admin(user.id):
        try:
            amount = float(text)

            if amount < 0:
                raise ValueError

            set_setting("welcome_bonus", amount)
            context.user_data.pop("state", None)

            await update.message.reply_text(
                f"✅ Welcome bonus set to ₹{amount:.2f}"
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Enter a valid amount."
            )

        return

    # --------------------------------------------------------
    # CHANNEL
    # --------------------------------------------------------

    if state == "set_channel" and is_admin(user.id):
        if not text.startswith("@"):
            await update.message.reply_text(
                "❌ Channel username must start with @"
            )
            return

        set_setting("required_channel", text)
        context.user_data.pop("state", None)

        await update.message.reply_text(
            f"✅ Required channel changed to {text}"
        )
        return

    # --------------------------------------------------------
    # SUPPORT
    # --------------------------------------------------------

    if state == "set_support" and is_admin(user.id):
        if not text.startswith("@"):
            await update.message.reply_text(
                "❌ Username must start with @"
            )
            return

        set_setting("support_username", text)
        context.user_data.pop("state", None)

        await update.message.reply_text(
            f"✅ Support changed to {text}"
        )
        return

    # --------------------------------------------------------
    # MINIMUM WITHDRAWAL
    # --------------------------------------------------------

    if state == "set_minimum" and is_admin(user.id):
        try:
            amount = float(text)

            if amount <= 0:
                raise ValueError

            set_setting("minimum_withdrawal", amount)
            context.user_data.pop("state", None)

            await update.message.reply_text(
                f"✅ Minimum withdrawal set to ₹{amount:.2f}"
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Enter a valid amount."
            )

        return

    # --------------------------------------------------------
    # USER WITHDRAW AMOUNT
    # --------------------------------------------------------

    if state == "withdraw_amount":
        try:
            amount = float(text)

            minimum = float(
                get_setting("minimum_withdrawal")
            )

            row = get_user(user.id)

            if amount < minimum:
                await update.message.reply_text(
                    f"❌ Minimum withdrawal is ₹{minimum:.2f}"
                )
                return

            if amount > row["balance"]:
                await update.message.reply_text(
                    "❌ Insufficient balance."
                )
                return

            context.user_data["withdraw_amount"] = amount
            context.user_data["state"] = "withdraw_method"

            await update.message.reply_text(
                (
                    "🏦 <b>Withdrawal Method</b>\n\n"
                    "Send your payment method.\n\n"
                    "Example:\n"
                    "<code>UPI</code>"
                ),
                parse_mode=ParseMode.HTML,
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Enter a valid amount."
            )

        return

    # --------------------------------------------------------
    # WITHDRAW METHOD
    # --------------------------------------------------------

    if state == "withdraw_method":
        context.user_data["withdraw_method"] = text
        context.user_data["state"] = "withdraw_account"

        await update.message.reply_text(
            (
                "📋 <b>Payment Details</b>\n\n"
                "Send your UPI ID / account details."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    # --------------------------------------------------------
    # WITHDRAW ACCOUNT
    # --------------------------------------------------------

    if state == "withdraw_account":
        amount = context.user_data.get("withdraw_amount")
        method = context.user_data.get("withdraw_method")

        row = get_user(user.id)

        if not amount or not method:
            context.user_data.clear()
            await update.message.reply_text(
                "❌ Withdrawal session expired."
            )
            return

        if amount > row["balance"]:
            context.user_data.clear()
            await update.message.reply_text(
                "❌ Insufficient balance."
            )
            return

        # Reserve the amount immediately.
        subtract_balance(
            user.id,
            amount,
            "Withdrawal request",
            "withdrawal",
        )

        db.execute("""
            INSERT INTO withdrawals(
                user_id,
                amount,
                method,
                account,
                status,
                created_at
            )
            VALUES(?,?,?,?,?,?)
        """, (
            user.id,
            amount,
            method,
            text,
            "pending",
            now(),
        ))

        db.commit()

        context.user_data.clear()

        await update.message.reply_text(
            (
                "✅ <b>Withdrawal Submitted</b>\n\n"
                f"Amount: <b>₹{amount:.2f}</b>\n"
                f"Method: <b>{method}</b>\n\n"
                "Your request has been sent to admin."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(user.id),
        )

        # Notify admins
        withdrawal_id = db.execute(
            "SELECT last_insert_rowid() id"
        ).fetchone()["id"]

        admin_text = (
            "💸 <b>New Withdrawal Request</b>\n\n"
            f"Request: <b>#{withdrawal_id}</b>\n"
            f"User: <code>{user.id}</code>\n"
            f"Amount: <b>₹{amount:.2f}</b>\n"
            f"Method: <b>{method}</b>\n"
            f"Account: <code>{text}</code>"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💸 Open Request",
                    callback_data=f"wd_{withdrawal_id}",
                )
            ]
        ])

        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    admin_text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        return

    # --------------------------------------------------------
    # REDEEM
    # --------------------------------------------------------

    if state == "redeem":
        # Basic placeholder for redeem-code system.
        # Codes can be added later in the database/admin panel.
        context.user_data.clear()

        await update.message.reply_text(
            (
                "🎟 <b>Redeem</b>\n\n"
                "❌ This code is invalid or expired."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(user.id),
        )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update, context):
    query = update.callback_query

    if query.data == "admin_panel" or query.data.startswith("a_") \
            or query.data.startswith("wd_") \
            or query.data.startswith("approve_") \
            or query.data.startswith("reject_") \
            or query.data.startswith("set_") \
            or query.data == "toggle_referral" \
            or query.data == "toggle_maintenance":

        if query.data == "admin_panel":
            await admin_panel(update, context)
        else:
            await admin_callback(update, context)

        return

    await user_callback(update, context)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    logger.error(
        "Exception while handling update:",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if BOT_TOKEN == "PUT_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Set BOT_TOKEN environment variable first."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("admin", admin_command)
    )

    application.add_handler(
        CommandHandler("user", user_info)
    )

    application.add_handler(
        CommandHandler("ban", ban_user)
    )

    application.add_handler(
        CommandHandler("unban", unban_user)
    )

    application.add_handler(
        CallbackQueryHandler(callback_router)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Referral bot started.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()        pay_keyboard = [
            [InlineKeyboardButton("🔗 Pay Now (पेमेंट करें)", url=public_link)]
        ]
        
        msg_text = (
            f"✅ **ऑर्डर सफलतापूर्वक जनरेट हो गया है!**\n\n"
            f"🆔 **Order ID:** `{order_id}`\n"
            f"💵 **Amount:** ₹{amount}\n\n"
            f"👇 नीचे दिए गए लिंक पर क्लिक करके पेमेंट पूरा करें। पेमेंट होते ही ऑटो-वेरिफिकेशन शुरू हो जाएगा।"
        )

        await update.message.reply_text(
            text=msg_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(pay_keyboard)
        )

        # Start Async Background Payment Verification
        asyncio.create_task(verify_payment_loop(context, update.effective_chat.id, order_id))

    except Exception as e:
        logging.error(f"Error in order creation: {e}")
        await update.message.reply_text("❌ एपीआई से कनेक्ट करने में विफल। कृपया थोड़ी देर बाद प्रयास करें।")

    return ConversationHandler.END

# 4. Background Verification & Delivery Task
async def verify_payment_loop(context: ContextTypes.DEFAULT_TYPE, chat_id: int, order_id: str):
    max_checks = 45  # 45 checks * 8 seconds = ~6 minutes timeout
    
    for _ in range(max_checks):
        await asyncio.sleep(8)  # Check every 8 seconds
        
        try:
            r = requests.get(f"{BASE_URL}/verify.php?order_id={order_id}", headers=HEADERS, timeout=10)
            status_data = r.json()

            # If Payment Successful
            if status_data.get("payment"):
                delivery_token = status_data.get("delivery_token")
                
                # Deliver Product
                del_resp = requests.get(f"{BASE_URL}/deliver.php?token={delivery_token}", headers=HEADERS, timeout=10)
                delivery_result = del_resp.json()
                
                delivered_key = delivery_result.get("content", "KEY-VIP-8899-X12")

                success_msg = (
                    f"🎉 **पेमेंट सफल रहा! (Payment Successful)**\n\n"
                    f"🆔 **Order ID:** `{order_id}`\n"
                    f"📦 **आपकी की (Key):** `{delivered_key}`\n\n"
                    f"खरीदारी के लिए धन्यवाद!"
                )
                await context.bot.send_message(chat_id=chat_id, text=success_msg, parse_mode="Markdown")
                return

            # If Order Expired
            elif status_data.get("status") == "EXPIRED":
                fail_msg = f"❌ **पेमेंट समय समाप्त (Order Expired)**\n\nOrder ID: `{order_id}` का समय समाप्त हो गया है।"
                await context.bot.send_message(chat_id=chat_id, text=fail_msg, parse_mode="Markdown")
                return

        except Exception as e:
            logging.error(f"Error checking verification: {e}")

    # Timeout Message after max checks
    await context.bot.send_message(
        chat_id=chat_id, 
        text=f"⚠️ **पेमेंट टाइमआउट:** Order ID `{order_id}` का वेरिफिकेशन टाइमआउट हो गया है।"
    )

# Cancel Handler for conversation
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("प्रक्रिया रद्द कर दी गई है।")
    return ConversationHandler.END

# ---------------- MAIN FUNCTION ----------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_gateway_click, pattern="^pay_gateway$")],
        states={
            WAITING_FOR_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)

    print("🤖 Telegram Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()

# Developer: @LuffyBots
# Channel: @EscrowMoon
# Database: PostgreSQL (Supabase/Railway)
# Library: python-telegram-bot (Async)
#
# ✅ Added:
# - Join Request (Invite Link "Request Admin Approval") support + Admin Accept/Reject
# - REQUEST_CHANNEL_ID where requests go (admins approve)
# - /getfont <text> => returns small-caps fancy font
# - protect_content=True while sending media (prevents forward/save where Telegram supports)

import asyncio
import logging
import random
import string
import json
import os
from typing import List, Optional, Dict, Any, Tuple
from threading import Lock

import psycopg2

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Message,
    BotCommand,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

# ---------------- CONFIG ----------------
# Use env vars on Railway. DO NOT hardcode token/db in code.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8041347841:AAHS_8ag7vNSPCT_Pg_HesetAvDCbCVrhIY").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres.gitugrdddywbgjinmmhj:LuffyBotsX11@aws-1-ap-south-1.pooler.supabase.com:5432/postgres").strip()

# OWNER (cannot be removed)
OWNER_ID = int(os.getenv("OWNER_ID", "6847499628"))

# fallback initial admins list (owner always admin)
ADMIN_IDS: List[int] = [OWNER_ID]

# Default force-join channel
DEFAULT_FORCE_CHANNEL_LINK = os.getenv("DEFAULT_FORCE_CHANNEL_LINK", "https://t.me/+UKFj-D0zB85hNDNl").strip()
DEFAULT_FORCE_CHANNEL_ID = int(os.getenv("DEFAULT_FORCE_CHANNEL_ID", "-1002699957030"))
DEFAULT_FORCE_BUTTON_NAME = os.getenv("DEFAULT_FORCE_BUTTON_NAME", "✅ ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ").strip()

# Optional: Forward uploads to a channel/group (set empty to disable)
_PRIVATE = os.getenv("PRIVATE_CHANNEL_ID", "").strip()
PRIVATE_CHANNEL_ID = int(_PRIVATE) if _PRIVATE else None

# ✅ Request channel/group where join requests should appear for admin approval
# Bot must be admin there to post messages.
_REQ = os.getenv("REQUEST_CHANNEL_ID", "").strip()
REQUEST_CHANNEL_ID = int(_REQ) if _REQ else None

# Join button after file delivery
MAIN_CHANNEL_LINK = DEFAULT_FORCE_CHANNEL_LINK

# Protect media from forwarding/saving (Telegram supported clients)
PROTECT_CONTENT = os.getenv("PROTECT_CONTENT", "1").strip() not in ("0", "false", "False", "no", "NO")
# --------------------------------------

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set. Add BOT_TOKEN in environment variables.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set. Add DATABASE_URL in environment variables.")

# ---------------- DB (PostgreSQL) ----------------
DB_LOCK = Lock()

# Supabase often requires SSL
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
conn.autocommit = False
cursor = conn.cursor()

with DB_LOCK:
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        active INTEGER DEFAULT 1,
        premium INTEGER DEFAULT 0,
        banned INTEGER DEFAULT 0
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS media_files (
        media_id TEXT PRIMARY KEY,
        files TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS force_join_channels (
        id SERIAL PRIMARY KEY,
        channel_link TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        button_name TEXT NOT NULL,
        enabled INTEGER DEFAULT 1,
        UNIQUE(channel_link, chat_id, button_name)
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS downloads (
        id SERIAL PRIMARY KEY,
        media_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        user_id BIGINT PRIMARY KEY,
        added_by BIGINT,
        ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # ✅ Join requests table (for "Request Admin Approval" invite links)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS join_requests (
        id SERIAL PRIMARY KEY,
        chat_id TEXT NOT NULL,
        user_id BIGINT NOT NULL,
        username TEXT,
        status TEXT DEFAULT 'pending', -- pending/approved/rejected
        ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        decided_by BIGINT,
        decided_ts TIMESTAMP,
        UNIQUE(chat_id, user_id)
    )
    """)
    conn.commit()

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- UI TEXTS ----------------
FANCY_FILE_READY_TEXT = (
    "›› ʜᴇʏ ×\n"
    "  ʏᴏᴜʀ ғɪʟᴇ ɪs ʀᴇᴀᴅʏ ‼️ ʟᴏᴏᴋs ʟɪᴋᴇ ʏᴏᴜ ʜᴀᴠᴇɴ'ᴛ sᴜʙsᴄʀɪʙᴇᴅ ᴛᴏ ᴏᴜʀ ᴄʜᴀɴɴᴇʟs ʏᴇᴛ, "
    "sᴜʙsᴄʀɪʙᴇ ɴᴏᴡ ᴛᴏ ɢᴇᴛ ʏᴏᴜʀ ғɪʟᴇs"
)

PENDING_JOIN_TEXT = (
    "⏳ ʏᴏᴜʀ ᴊᴏɪɴ ʀᴇǫᴜᴇsᴛ ɪs **PENDING**.\n"
    "ᴀᴅᴍɪɴ ᴡɪʟʟ ᴀᴄᴄᴇᴘᴛ sᴏᴏɴ."
)

START_CAPTION = (
    "» ʜᴇʏ!!, ɴᴏɴᴇ ~\n\n"
    "ɪ ᴀᴍ ғɪʟᴇ sᴛᴏʀᴇ ʙᴏᴛ, ɪ ᴄᴀɴ sᴛᴏʀᴇ ᴘʀɪᴠᴀᴛᴇ ғɪʟᴇs ɪɴ sᴘᴇᴄɪғɪᴇᴅ ᴄʜᴀɴɴᴇʟ "
    "ᴀɴᴅ ᴏᴛʜᴇʀ ᴜsᴇʀs ᴄᴀɴ ᴀᴄᴄᴇss ɪᴛ ғʀᴏᴍ sᴘᴇᴄɪᴀʟ ʟɪɴᴋ."
)

ABOUT_TEXT = (
    "›› ᴄʀᴇᴀᴛᴇᴅ ʙʏ: [Boter](https://t.me/LuffyBots)\n"
    "›› ᴏᴡɴᴇʀ: @LuffyBots\n"
    "› ʟᴀɴɢᴜᴀɢᴇ: [Pʏᴛʜᴏɴ 3](https://docs.python.org/3/)\n"
    "›› ʟɪʙʀᴀʀʏ: [python-telegram-bot](https://docs.python-telegram-bot.org/)\n"
)

# Button Labels
BTN_ABOUT = "ᴀʙᴏᴜᴛ"
BTN_CLOSE = "ᴄʟᴏsᴇ"
BTN_UPLOAD = "📤 sᴛᴀʀᴛ ᴜᴘʟᴏᴀᴅɪɴɢ"
BTN_I_JOINED = "✅ ɪ ᴊᴏɪɴᴇᴅ"
BTN_JOIN_MAIN = "📢 ᴊᴏɪɴ ᴏᴜʀ ᴄʜᴀɴɴᴇʟ"

# ---------------- FONT ----------------
_SMALLCAPS_MAP = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ғ", "g": "ɢ", "h": "ʜ",
    "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ", "o": "ᴏ", "p": "ᴘ",
    "q": "ǫ", "r": "ʀ", "s": "s", "t": "ᴛ", "u": "ᴜ", "v": "ᴠ", "w": "ᴡ", "x": "x",
    "y": "ʏ", "z": "ᴢ",
}
def to_smallcaps(text: str) -> str:
    out = []
    for ch in text:
        low = ch.lower()
        out.append(_SMALLCAPS_MAP.get(low, ch))
    return "".join(out)

# ---------------- HELPERS ----------------
def gen_id(length: int = 12) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))

def set_setting(key: str, value: str) -> None:
    with DB_LOCK:
        cursor.execute(
            """
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )
        conn.commit()

def get_setting(key: str) -> Optional[str]:
    with DB_LOCK:
        cursor.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cursor.fetchone()
    return row[0] if row else None

def get_start_photo_id() -> Optional[str]:
    return get_setting("start_photo_file_id")

def ensure_user_record(user_id: int, username: Optional[str] = None) -> None:
    with DB_LOCK:
        cursor.execute(
            """
            INSERT INTO users (user_id, username, active, premium, banned)
            VALUES (%s, %s, 1, 0, 0)
            ON CONFLICT (user_id) DO UPDATE
              SET username = EXCLUDED.username, active = 1
            """,
            (user_id, username),
        )
        conn.commit()

def is_owner(user_id: int) -> bool:
    return int(user_id) == int(OWNER_ID)

def get_admin_ids_from_db() -> List[int]:
    with DB_LOCK:
        cursor.execute("SELECT user_id FROM admins")
        rows = cursor.fetchall()
    return [int(r[0]) for r in rows]

def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    if user_id in ADMIN_IDS:
        return True
    return user_id in get_admin_ids_from_db()

def add_admin_db(user_id: int, added_by: int) -> None:
    if is_owner(user_id):
        return
    with DB_LOCK:
        cursor.execute(
            """
            INSERT INTO admins (user_id, added_by)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET added_by = EXCLUDED.added_by
            """,
            (int(user_id), int(added_by)),
        )
        conn.commit()

def remove_admin_db(user_id: int) -> bool:
    if is_owner(user_id):
        return False
    with DB_LOCK:
        cursor.execute("DELETE FROM admins WHERE user_id = %s", (int(user_id),))
        conn.commit()
        return cursor.rowcount > 0

def list_admins_all() -> List[int]:
    ids = set(ADMIN_IDS)
    ids.add(int(OWNER_ID))
    for x in get_admin_ids_from_db():
        ids.add(int(x))
    return sorted(ids)

def set_premium(user_id: int, value: bool) -> None:
    with DB_LOCK:
        cursor.execute(
            """
            INSERT INTO users (user_id, username, active, premium, banned)
            VALUES (%s, NULL, 1, 0, 0)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id,),
        )
        cursor.execute("UPDATE users SET premium = %s WHERE user_id = %s", (1 if value else 0, user_id))
        conn.commit()

def is_premium(user_id: int) -> bool:
    with DB_LOCK:
        cursor.execute("SELECT premium FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
    return bool(row and row[0])

def ban_user(user_id: int) -> None:
    with DB_LOCK:
        cursor.execute(
            """
            INSERT INTO users (user_id, username, active, premium, banned)
            VALUES (%s, NULL, 1, 0, 0)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id,),
        )
        cursor.execute("UPDATE users SET banned = 1 WHERE user_id = %s", (user_id,))
        conn.commit()

def unban_user(user_id: int) -> None:
    with DB_LOCK:
        cursor.execute(
            """
            INSERT INTO users (user_id, username, active, premium, banned)
            VALUES (%s, NULL, 1, 0, 0)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id,),
        )
        cursor.execute("UPDATE users SET banned = 0 WHERE user_id = %s", (user_id,))
        conn.commit()

def is_banned(user_id: int) -> bool:
    with DB_LOCK:
        cursor.execute("SELECT banned FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
    return bool(row and row[0])

def save_data(media_id: str, files: list) -> None:
    with DB_LOCK:
        cursor.execute(
            """
            INSERT INTO media_files (media_id, files) VALUES (%s, %s)
            ON CONFLICT (media_id) DO UPDATE SET files = EXCLUDED.files
            """,
            (media_id, json.dumps(files, ensure_ascii=False)),
        )
        conn.commit()

def get_data(media_id: str) -> Optional[list]:
    with DB_LOCK:
        cursor.execute("SELECT files FROM media_files WHERE media_id = %s", (media_id,))
        row = cursor.fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None

def log_download(media_id: str, user_id: int) -> None:
    with DB_LOCK:
        cursor.execute("INSERT INTO downloads (media_id, user_id) VALUES (%s, %s)", (media_id, user_id))
        conn.commit()

def get_nonbanned_user_ids() -> List[int]:
    with DB_LOCK:
        cursor.execute("SELECT user_id FROM users WHERE banned = 0")
        rows = cursor.fetchall()
    return [int(r[0]) for r in rows]

def get_premium_user_ids() -> List[int]:
    with DB_LOCK:
        cursor.execute("SELECT user_id FROM users WHERE banned = 0 AND premium = 1")
        rows = cursor.fetchall()
    return [int(r[0]) for r in rows]

# ---------------- JOIN REQUEST DB ----------------
def upsert_join_request(chat_id: str, user_id: int, username: Optional[str], status: str = "pending") -> None:
    with DB_LOCK:
        cursor.execute(
            """
            INSERT INTO join_requests (chat_id, user_id, username, status)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (chat_id, user_id) DO UPDATE
              SET username=EXCLUDED.username, status=EXCLUDED.status, ts=CURRENT_TIMESTAMP
            """,
            (str(chat_id), int(user_id), username, status),
        )
        conn.commit()

def get_join_request_status(chat_id: str, user_id: int) -> Optional[str]:
    with DB_LOCK:
        cursor.execute(
            "SELECT status FROM join_requests WHERE chat_id=%s AND user_id=%s",
            (str(chat_id), int(user_id)),
        )
        row = cursor.fetchone()
    return row[0] if row else None

def set_join_request_decision(chat_id: str, user_id: int, status: str, decided_by: int) -> bool:
    with DB_LOCK:
        cursor.execute(
            """
            UPDATE join_requests
            SET status=%s, decided_by=%s, decided_ts=NOW()
            WHERE chat_id=%s AND user_id=%s AND status='pending'
            """,
            (status, int(decided_by), str(chat_id), int(user_id)),
        )
        conn.commit()
        return cursor.rowcount > 0

# ---------------- FORCE JOIN DB ----------------
def add_force_channel(channel_link: str, chat_id: str, button_name: str) -> None:
    with DB_LOCK:
        cursor.execute(
            """
            INSERT INTO force_join_channels (channel_link, chat_id, button_name, enabled)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (channel_link, chat_id, button_name) DO UPDATE SET enabled = 1
            """,
            (channel_link.strip(), str(chat_id).strip(), button_name.strip()),
        )
        conn.commit()

def remove_force_channel(channel_link: str, chat_id: str, button_name: str) -> int:
    with DB_LOCK:
        cursor.execute(
            "DELETE FROM force_join_channels WHERE channel_link = %s AND chat_id = %s AND button_name = %s",
            (channel_link.strip(), str(chat_id).strip(), button_name.strip()),
        )
        conn.commit()
        return cursor.rowcount

def get_force_channels() -> List[Tuple[str, str, str]]:
    with DB_LOCK:
        cursor.execute(
            "SELECT channel_link, chat_id, button_name FROM force_join_channels WHERE enabled = 1 ORDER BY id ASC"
        )
        rows = cursor.fetchall()
    return rows

def ensure_default_force_channel():
    add_force_channel(DEFAULT_FORCE_CHANNEL_LINK, str(DEFAULT_FORCE_CHANNEL_ID), DEFAULT_FORCE_BUTTON_NAME)

def _chat_identifier_from_chat_id(chat_id_str: str):
    s = str(chat_id_str).strip()
    try:
        return int(s)
    except Exception:
        return s if s.startswith("@") else f"@{s}"

async def check_force_join_for_user(bot, user_id: int) -> Tuple[bool, List[Tuple[str, str, str]]]:
    channels = get_force_channels()
    if not channels:
        return True, []
    missing: List[Tuple[str, str, str]] = []
    for channel_link, chat_id, button_name in channels:
        try:
            ident = _chat_identifier_from_chat_id(chat_id)
            member = await bot.get_chat_member(ident, user_id)
            if member.status in ("left", "kicked"):
                missing.append((channel_link, chat_id, button_name))
        except Exception:
            # if channel is private / bot lacks rights / request approval flow, treat as missing
            missing.append((channel_link, chat_id, button_name))
    return (len(missing) == 0), missing

def any_pending_request(user_id: int, missing: List[Tuple[str, str, str]]) -> bool:
    for _, chat_id, _ in missing:
        st = get_join_request_status(str(chat_id), int(user_id))
        if st == "pending":
            return True
    return False

# ---------------- AUTO DELETE ----------------
async def schedule_delete_message(bot, chat_id: int, message_id: int, delay: int = 600):
    async def _delete():
        try:
            await asyncio.sleep(delay)
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.debug(f"delete failed {message_id}: {e}")
    asyncio.create_task(_delete())

# ---------------- UI SENDERS ----------------
async def send_start_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = get_start_photo_id()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_ABOUT, callback_data="ui_about"),
         InlineKeyboardButton(BTN_CLOSE, callback_data="ui_close")],
        [InlineKeyboardButton(BTN_UPLOAD, callback_data="upload")]
    ])

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    if photo_id:
        await msg.reply_photo(photo_id, caption=START_CAPTION, reply_markup=keyboard, protect_content=PROTECT_CONTENT)
    else:
        await msg.reply_text(START_CAPTION, reply_markup=keyboard)

async def send_pending_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, missing, media_id: str):
    # show join buttons + pending message (no "I joined" spam)
    photo_id = get_start_photo_id()
    buttons = []
    for channel_link, _, button_name in missing:
        buttons.append([InlineKeyboardButton(button_name, url=channel_link)])
    # keep verify button too
    buttons.append([InlineKeyboardButton(BTN_I_JOINED, callback_data=f"confirm_join:{media_id}")])
    markup = InlineKeyboardMarkup(buttons)

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    if photo_id:
        await msg.reply_photo(photo_id, caption=PENDING_JOIN_TEXT, parse_mode="Markdown",
                              reply_markup=markup, protect_content=PROTECT_CONTENT)
    else:
        await msg.reply_text(PENDING_JOIN_TEXT, parse_mode="Markdown", reply_markup=markup)

async def send_join_required_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, missing, media_id: str):
    photo_id = get_start_photo_id()
    buttons = []
    for channel_link, _, button_name in missing:
        buttons.append([InlineKeyboardButton(button_name, url=channel_link)])
    buttons.append([InlineKeyboardButton(BTN_I_JOINED, callback_data=f"confirm_join:{media_id}")])
    markup = InlineKeyboardMarkup(buttons)

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    if photo_id:
        await msg.reply_photo(photo_id, caption=FANCY_FILE_READY_TEXT, reply_markup=markup,
                              protect_content=PROTECT_CONTENT)
    else:
        await msg.reply_text(FANCY_FILE_READY_TEXT, reply_markup=markup)

async def send_about_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = get_start_photo_id()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(BTN_CLOSE, callback_data="ui_close")]])

    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    if photo_id:
        await msg.reply_photo(photo_id, caption=ABOUT_TEXT, parse_mode="Markdown",
                              reply_markup=kb, protect_content=PROTECT_CONTENT)
    else:
        await msg.reply_text(ABOUT_TEXT, parse_mode="Markdown", reply_markup=kb)

# ---------------- MEDIA DELIVERY ----------------
async def _send_media_for_media_id(update: Update, context: ContextTypes.DEFAULT_TYPE, media_id: str):
    target_msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not target_msg:
        return

    files = get_data(media_id)
    if not files:
        return await target_msg.reply_text("❌ ᴍᴇᴅɪᴀ ᴇxᴘɪʀᴇᴅ ᴏʀ ɴᴏᴛ ғᴏᴜɴᴅ.")

    processing = await target_msg.reply_text("⏳ ᴘʀᴏᴄᴇssɪɴɢ...")
    await asyncio.sleep(1.2)

    sent_messages = []
    for f in files:
        try:
            t = f.get("type")
            sent_msg: Optional[Message] = None
            if t == "photo":
                sent_msg = await target_msg.reply_photo(
                    f["file_id"], caption=f.get("caption", ""), protect_content=PROTECT_CONTENT
                )
            elif t == "video":
                sent_msg = await target_msg.reply_video(
                    f["file_id"], caption=f.get("caption", ""), protect_content=PROTECT_CONTENT
                )
            elif t == "document":
                sent_msg = await target_msg.reply_document(
                    f["file_id"], caption=f.get("caption", ""), protect_content=PROTECT_CONTENT
                )
            elif t == "animation":
                sent_msg = await target_msg.reply_animation(
                    f["file_id"], caption=f.get("caption", ""), protect_content=PROTECT_CONTENT
                )
            elif t == "video_note":
                sent_msg = await context.bot.send_video_note(
                    target_msg.chat.id, f["file_id"], protect_content=PROTECT_CONTENT
                )
            if sent_msg:
                sent_messages.append(sent_msg)
        except Exception as e:
            logger.error(f"Send failed {media_id}: {e}")

    try:
        await processing.delete()
    except Exception:
        pass

    try:
        log_download(media_id, update.effective_user.id)
    except Exception:
        pass

    join_btn = InlineKeyboardMarkup([[InlineKeyboardButton(BTN_JOIN_MAIN, url=MAIN_CHANNEL_LINK)]])
    warning_text = (
        "⚠️ ᴅᴜᴇ ᴛᴏ ᴄᴏᴘʏʀɪɢʜᴛ ɪssᴜᴇs...\n"
        "ʏᴏᴜʀ ғɪʟᴇs ᴡɪʟʟ ʙᴇ ᴅᴇʟᴇᴛᴇᴅ ᴡɪᴛʜɪɴ 10 ᴍɪɴᴜᴛᴇs.\n"
        "sᴏ ᴘʟᴇᴀsᴇ ғᴏʀᴡᴀʀᴅ ᴛʜᴇᴍ ᴛᴏ ᴀɴʏ ᴏᴛʜᴇʀ ᴘʟᴀᴄᴇ."
    )
    try:
        warning_msg = await target_msg.reply_text(warning_text, reply_markup=join_btn)
    except Exception:
        warning_msg = None

    for msg in sent_messages:
        await schedule_delete_message(context.bot, msg.chat.id, msg.message_id, delay=600)
    if warning_msg:
        await schedule_delete_message(context.bot, warning_msg.chat.id, warning_msg.message_id, delay=600)

# ---------------- /getid ----------------
async def cmd_getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    context.user_data["awaiting_getid"] = True
    await update.message.reply_text("✅ ɴᴏᴡ sᴇɴᴅ ᴀɴʏ ᴘʜᴏᴛᴏ/ᴠɪᴅᴇᴏ/ғɪʟᴇ...\nɪ'ʟʟ ʀᴇᴘʟʏ ᴡɪᴛʜ ғɪʟᴇ_ɪᴅ.")

async def _handle_getid_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("awaiting_getid"):
        return False
    msg = update.message
    if not msg:
        return True

    context.user_data["awaiting_getid"] = False

    if msg.photo:
        all_ids = [p.file_id for p in msg.photo]
        best = all_ids[-1]
        text = "📸 ᴘʜᴏᴛᴏ ғɪʟᴇ_ɪᴅ:\n\n" + best + "\n\n" + "• ᴀʟʟ sɪᴢᴇs:\n" + "\n".join(all_ids)
        await msg.reply_text(text)
        return True
    if msg.video:
        await msg.reply_text("🎥 ᴠɪᴅᴇᴏ ғɪʟᴇ_ɪᴅ:\n\n" + msg.video.file_id)
        return True
    if msg.document:
        await msg.reply_text("📄 ᴅᴏᴄᴜᴍᴇɴᴛ ғɪʟᴇ_ɪᴅ:\n\n" + msg.document.file_id)
        return True
    if msg.animation:
        await msg.reply_text("🎞️ ᴀɴɪᴍᴀᴛɪᴏɴ ғɪʟᴇ_ɪᴅ:\n\n" + msg.animation.file_id)
        return True
    if getattr(msg, "video_note", None):
        await msg.reply_text("⭕ ᴠɪᴅᴇᴏ_ɴᴏᴛᴇ ғɪʟᴇ_ɪᴅ:\n\n" + msg.video_note.file_id)
        return True

    await msg.reply_text("❌ sᴜᴘᴘᴏʀᴛᴇᴅ: ᴘʜᴏᴛᴏ/ᴠɪᴅᴇᴏ/ᴅᴏᴄ/ᴀɴɪᴍ/ᴠɴ")
    return True

# ---------------- NEW COMMANDS ----------------
async def cmd_givefont(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "✅ ғᴏɴᴛ ᴄᴏᴘʏ ʜᴇʀᴇ:\n\n"
        "ʟᴀɴɢᴜᴀɢᴇ\n"
        "ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ\n"
        "ᴄʟᴏsᴇ\n"
        "sᴛᴀʀᴛ ᴜᴘʟᴏᴀᴅɪɴɢ\n"
    )
    await update.message.reply_text(text)

# ✅ /getfont <text>
async def cmd_getfont(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not context.args:
        return await update.message.reply_text("Usage: /getfont <text>\nExample: /getfont Hey bot")
    raw = " ".join(context.args).strip()
    await update.message.reply_text(to_smallcaps(raw))

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user_record(u.id, u.username)
    prem = "✅" if is_premium(u.id) else "❌"
    ban = "✅" if is_banned(u.id) else "❌"
    adm = "✅" if is_admin(u.id) else "❌"
    text = (
        f"👤 ᴘʀᴏғɪʟᴇ\n\n"
        f"ɪᴅ: `{u.id}`\n"
        f"ᴜsᴇʀ: @{u.username or 'None'}\n"
        f"ᴀᴅᴍɪɴ: {adm}\n"
        f"ᴘʀᴇᴍɪᴜᴍ: {prem}\n"
        f"ʙᴀɴɴᴇᴅ: {ban}\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# (Your original /request command is kept as "support request to admins")
async def cmd_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    u = update.effective_user
    ensure_user_record(u.id, u.username)
    if not context.args:
        return await update.message.reply_text("Usage: /request <message>")
    msg_txt = " ".join(context.args).strip()
    text = (
        "📩 ɴᴇᴡ ʀᴇǫᴜᴇsᴛ\n\n"
        f"ғʀᴏᴍ: @{u.username or 'None'}\n"
        f"ɪᴅ: {u.id}\n\n"
        f"ᴍᴇssᴀɢᴇ:\n{msg_txt}"
    )
    targets = list_admins_all()
    for tid in targets:
        try:
            await context.bot.send_message(tid, text)
        except Exception:
            pass
    await update.message.reply_text("✅ ʏᴏᴜʀ ʀᴇǫᴜᴇsᴛ sᴇɴᴛ.")

# ---------------- OWNER-ONLY ADMIN MGMT ----------------
async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /addadmin <user_id>")
    try:
        uid = int(context.args[0])
    except Exception:
        return await update.message.reply_text("Invalid id.")
    add_admin_db(uid, update.effective_user.id)
    await update.message.reply_text(f"✅ ᴀᴅᴍɪɴ ᴀᴅᴅᴇᴅ: {uid}")

async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /removeadmin <user_id>")
    try:
        uid = int(context.args[0])
    except Exception:
        return await update.message.reply_text("Invalid id.")
    ok = remove_admin_db(uid)
    if ok:
        await update.message.reply_text(f"✅ ᴀᴅᴍɪɴ ʀᴇᴍᴏᴠᴇᴅ: {uid}")
    else:
        await update.message.reply_text("❌ ɴᴏᴛ ғᴏᴜɴᴅ / ᴏᴡɴᴇʀ ᴄᴀɴ’ᴛ ʙᴇ ʀᴇᴍᴏᴠᴇᴅ.")

async def cmd_adminlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    ids = list_admins_all()
    lines = ["👑 ᴀᴅᴍɪɴ ʟɪsᴛ:"]
    for i, uid in enumerate(ids, start=1):
        tag = " (OWNER)" if is_owner(uid) else ""
        lines.append(f"{i}. {uid}{tag}")
    await update.message.reply_text("\n".join(lines))

# ---------------- CORE COMMANDS ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user_record(user.id, user.username)

    if is_banned(user.id):
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text("🚫 ʏᴏᴜ ᴀʀᴇ ʙᴀɴɴᴇᴅ.")
        return

    media_id = context.args[0] if context.args else ""
    ok, missing = await check_force_join_for_user(context.bot, user.id)

    if media_id:
        if not ok:
            if any_pending_request(user.id, missing):
                await send_pending_screen(update, context, missing, media_id)
            else:
                await send_join_required_screen(update, context, missing, media_id)
            return
        await _send_media_for_media_id(update, context, media_id)
        return

    if not ok:
        if any_pending_request(user.id, missing):
            await send_pending_screen(update, context, missing, "")
        else:
            await send_join_required_screen(update, context, missing, "")
        return

    await send_start_screen(update, context)

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_about_screen(update, context)

async def cmd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = (
        "📌 ᴀᴅᴍɪɴ ᴍᴇɴᴜ\n\n"
        "/start\n/about\n/getid\n/profile\n/givefont\n/getfont <text>\n/request <msg>\n\n"
        "ᴀᴅᴍɪɴ:\n"
        "/upload\n"
        "/stats\n/users\n/broadcast\n/pbroadcast\n"
        "/ban <id>\n/unban <id>\n"
        "/premium <id>\n/unpremium <id>\n"
        "/premiumusers\n"
        "/del <media_id>\n/genlink <media_id>\n/usage <media_id>\n"
        "/setphoto <file_id>\n"
        "/set <channel_link> <chat_id> <button_name>\n"
        "/remove <channel_link> <chat_id> <button_name>\n"
        "/listchannels\n\n"
        "ᴏᴡɴᴇʀ:\n"
        "/addadmin <id>\n/removeadmin <id>\n/adminlist\n"
    )
    await update.message.reply_text(text)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with DB_LOCK:
        cursor.execute("SELECT COUNT(*) FROM users")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE banned = 1")
        banned = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE premium = 1")
        premium = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM downloads")
        downloads = cursor.fetchone()[0]
    await update.message.reply_text(
        f"📊 ʙᴏᴛ sᴛᴀᴛs\n"
        f"• ᴛᴏᴛᴀʟ: {total}\n"
        f"• ᴘʀᴇᴍɪᴜᴍ: {premium}\n"
        f"• ʙᴀɴɴᴇᴅ: {banned}\n"
        f"• ᴅᴏᴡɴʟᴏᴀᴅs: {downloads}"
    )

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with DB_LOCK:
        cursor.execute("SELECT user_id, username, premium, banned FROM users ORDER BY user_id DESC LIMIT 50")
        rows = cursor.fetchall()
    lines = ["👥 ʟᴀsᴛ 50 ᴜsᴇʀs:"]
    for uid, uname, prem, ban in rows:
        tag = "✅ᴘ" if prem else "—"
        tag2 = "🚫ʙ" if ban else ""
        lines.append(f"{uid}  @{uname or 'None'}  {tag}{tag2}")
    await update.message.reply_text("\n".join(lines))

async def make_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /premium <id>")
    try:
        uid = int(context.args[0])
    except Exception:
        return await update.message.reply_text("Invalid user id.")
    set_premium(uid, True)
    await update.message.reply_text(f"✅ ᴘʀᴇᴍɪᴜᴍ ᴀᴅᴅᴇᴅ: {uid}")

async def remove_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /unpremium <id>")
    try:
        uid = int(context.args[0])
    except Exception:
        return await update.message.reply_text("Invalid user id.")
    set_premium(uid, False)
    await update.message.reply_text(f"❌ ᴘʀᴇᴍɪᴜᴍ ʀᴇᴍᴏᴠᴇᴅ: {uid}")

async def cmd_premiumusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with DB_LOCK:
        cursor.execute("SELECT user_id, username FROM users WHERE premium = 1 AND banned = 0 ORDER BY user_id DESC LIMIT 100")
        rows = cursor.fetchall()
    if not rows:
        return await update.message.reply_text("ɴᴏ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀs.")
    text = "⭐ ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀs:\n" + "\n".join([f"{uid}  @{uname or 'None'}" for uid, uname in rows])
    await update.message.reply_text(text)

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /ban <id>")
    try:
        uid = int(context.args[0])
    except Exception:
        return await update.message.reply_text("Invalid user id.")
    ban_user(uid)
    await update.message.reply_text(f"🚫 ʙᴀɴɴᴇᴅ: {uid}")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /unban <id>")
    try:
        uid = int(context.args[0])
    except Exception:
        return await update.message.reply_text("Invalid user id.")
    unban_user(uid)
    await update.message.reply_text(f"✅ ᴜɴʙᴀɴɴᴇᴅ: {uid}")

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /del <media_id>")
    media_id = context.args[0]
    with DB_LOCK:
        cursor.execute("DELETE FROM media_files WHERE media_id = %s", (media_id,))
        conn.commit()
        ok = cursor.rowcount
    await update.message.reply_text("✅ ᴅᴇʟᴇᴛᴇᴅ." if ok else "❌ ɴᴏᴛ ғᴏᴜɴᴅ.")

async def cmd_genlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /genlink <media_id>")
    media_id = context.args[0]
    if not get_data(media_id):
        return await update.message.reply_text("❌ ᴍᴇᴅɪᴀ ɴᴏᴛ ғᴏᴜɴᴅ.")
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={media_id}"
    await update.message.reply_text(f"🔗 ʟɪɴᴋ:\n{link}")

async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /usage <media_id>")
    media_id = context.args[0]
    with DB_LOCK:
        cursor.execute("SELECT COUNT(*) FROM downloads WHERE media_id = %s", (media_id,))
        c = cursor.fetchone()[0]
    await update.message.reply_text(f"📈 ᴜsᴀɢᴇ ({media_id}): {c}")

# ---------------- FORCE JOIN COMMANDS ----------------
async def cmd_set_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 3:
        return await update.message.reply_text("Usage: /set <channel_link> <chat_id> <button_name>")
    channel_link = context.args[0]
    chat_id = context.args[1]
    button_name = " ".join(context.args[2:]).strip()
    add_force_channel(channel_link, chat_id, button_name)
    await update.message.reply_text(f"✅ ᴀᴅᴅᴇᴅ:\n{button_name}\n{channel_link}\n{chat_id}")

async def cmd_remove_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 3:
        return await update.message.reply_text("Usage: /remove <channel_link> <chat_id> <button_name>")
    channel_link = context.args[0]
    chat_id = context.args[1]
    button_name = " ".join(context.args[2:]).strip()
    deleted = remove_force_channel(channel_link, chat_id, button_name)
    await update.message.reply_text("✅ ʀᴇᴍᴏᴠᴇᴅ." if deleted else "❌ ɴᴏᴛ ғᴏᴜɴᴅ (ᴇxᴀᴄᴛ ᴍᴀᴛᴄʜ).")

async def cmd_listchannels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = get_force_channels()
    if not rows:
        return await update.message.reply_text("ɴᴏ ғᴏʀᴄᴇ-ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟs.")
    lines = ["📌 ғᴏʀᴄᴇ-ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟs:"]
    for i, (link, cid, name) in enumerate(rows, start=1):
        lines.append(f"{i}. {name} | {cid}\n{link}")
    await update.message.reply_text("\n\n".join(lines))

# ---------------- SET START/ABOUT PHOTO ----------------
async def cmd_setphoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /setphoto <file_id>")
    file_id = context.args[0].strip()
    set_setting("start_photo_file_id", file_id)
    await update.message.reply_text("✅ sᴛᴀʀᴛ/ᴀʙᴏᴜᴛ ᴘʜᴏᴛᴏ sᴀᴠᴇᴅ.")

# ---------------- BROADCAST ----------------
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if args:
        text = " ".join(args)
        context.user_data["broadcast_pending"] = {"type": "text", "text": text, "target": "all"}
        await _send_broadcast_preview(update, context)
        return
    context.user_data["awaiting_broadcast"] = True
    context.user_data["broadcast_target"] = "all"
    await update.message.reply_text("sᴇɴᴅ ʙʀᴏᴀᴅᴄᴀsᴛ ᴄᴏɴᴛᴇɴᴛ...")

async def pbroadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if args:
        text = " ".join(args)
        context.user_data["broadcast_pending"] = {"type": "text", "text": text, "target": "premium"}
        await _send_broadcast_preview(update, context)
        return
    context.user_data["awaiting_broadcast"] = True
    context.user_data["broadcast_target"] = "premium"
    await update.message.reply_text("sᴇɴᴅ ᴘʀᴇᴍɪᴜᴍ ʙʀᴏᴀᴅᴄᴀsᴛ ᴄᴏɴᴛᴇɴᴛ...")

async def _capture_broadcast_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    if not is_admin(update.effective_user.id):
        context.user_data.pop("awaiting_broadcast", None)
        return await msg.reply_text("ᴏɴʟʏ ᴀᴅᴍɪɴs ᴄᴀɴ ʙʀᴏᴀᴅᴄᴀsᴛ.")

    target = context.user_data.get("broadcast_target", "all")
    payload: Dict[str, Any]

    if msg.photo:
        payload = {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": msg.caption or "", "target": target}
    elif msg.video:
        payload = {"type": "video", "file_id": msg.video.file_id, "caption": msg.caption or "", "target": target}
    elif getattr(msg, "video_note", None):
        payload = {"type": "video_note", "file_id": msg.video_note.file_id, "caption": "", "target": target}
    elif msg.document:
        payload = {"type": "document", "file_id": msg.document.file_id, "caption": msg.caption or "", "target": target}
    elif msg.animation:
        payload = {"type": "animation", "file_id": msg.animation.file_id, "caption": msg.caption or "", "target": target}
    elif msg.text:
        payload = {"type": "text", "text": msg.text, "target": target}
    else:
        return await msg.reply_text("ᴜɴsᴜᴘᴘᴏʀᴛᴇᴅ ᴄᴏɴᴛᴇɴᴛ.")

    context.user_data.pop("awaiting_broadcast", None)
    context.user_data["broadcast_pending"] = payload
    await _send_broadcast_preview(update, context)

async def _send_broadcast_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = context.user_data.get("broadcast_pending")
    if not payload:
        return await update.message.reply_text("ɴᴏ ʙʀᴏᴀᴅᴄᴀsᴛ ᴄᴏɴᴛᴇɴᴛ.")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ᴄᴏɴғɪʀᴍ", callback_data=f"bc_confirm:{update.effective_user.id}")],
        [InlineKeyboardButton("❌ ᴄᴀɴᴄᴇʟ", callback_data=f"bc_cancel:{update.effective_user.id}")]
    ])

    try:
        if payload["type"] == "text":
            preview = await update.message.reply_text(f"📢 ᴘʀᴇᴠɪᴇᴡ:\n\n{payload['text']}", reply_markup=keyboard)
        elif payload["type"] == "photo":
            preview = await update.message.reply_photo(payload["file_id"], caption=payload.get("caption", ""), reply_markup=keyboard)
        elif payload["type"] == "video":
            preview = await update.message.reply_video(payload["file_id"], caption=payload.get("caption", ""), reply_markup=keyboard)
        elif payload["type"] == "video_note":
            preview = await update.message.reply_text("📢 ᴘʀᴇᴠɪᴇᴡ: (ʀᴏᴜɴᴅ ᴠɪᴅᴇᴏ)", reply_markup=keyboard)
        elif payload["type"] == "document":
            preview = await update.message.reply_document(payload["file_id"], caption=payload.get("caption", ""), reply_markup=keyboard)
        elif payload["type"] == "animation":
            preview = await update.message.reply_animation(payload["file_id"], caption=payload.get("caption", ""), reply_markup=keyboard)
        else:
            return await update.message.reply_text("ᴜɴsᴜᴘᴘᴏʀᴛᴇᴅ.")
    except Exception as e:
        logger.error(f"Preview send failed: {e}")
        return await update.message.reply_text("ғᴀɪʟᴇᴅ ᴛᴏ sᴇɴᴅ ᴘʀᴇᴠɪᴇᴡ.")

    context.user_data["broadcast_preview_message"] = {"chat_id": preview.chat.id, "message_id": preview.message_id}

async def _run_broadcast_task(bot, payload: Dict[str, Any], progress_msg: Optional[Message]):
    target = payload.get("target", "all")
    users = get_premium_user_ids() if target == "premium" else get_nonbanned_user_ids()

    total = len(users)
    sent = 0
    failed = 0

    async def update_progress(done):
        if not progress_msg:
            return
        try:
            await bot.edit_message_text(
                f"📡 ʙʀᴏᴀᴅᴄᴀsᴛɪɴɢ...\nsᴇɴᴛ: {done}/{total}\n✅ {sent} | ❌ {failed}",
                progress_msg.chat.id,
                progress_msg.message_id
            )
        except Exception:
            pass

    for idx, uid in enumerate(users, start=1):
        try:
            if payload["type"] == "text":
                await bot.send_message(uid, payload["text"])
            elif payload["type"] == "photo":
                await bot.send_photo(uid, payload["file_id"], caption=payload.get("caption", ""))
            elif payload["type"] == "video":
                await bot.send_video(uid, payload["file_id"], caption=payload.get("caption", ""))
            elif payload["type"] == "video_note":
                await bot.send_video_note(uid, payload["file_id"])
            elif payload["type"] == "document":
                await bot.send_document(uid, payload["file_id"], caption=payload.get("caption", ""))
            elif payload["type"] == "animation":
                await bot.send_animation(uid, payload["file_id"], caption=payload.get("caption", ""))
            else:
                await bot.send_message(uid, "Message from admin")
            sent += 1
        except Exception:
            failed += 1

        if idx % 10 == 0 or idx == total:
            await update_progress(idx)

        await asyncio.sleep(0.03)

    if progress_msg:
        try:
            await bot.edit_message_text(
                f"✅ ᴅᴏɴᴇ.\nᴛᴏᴛᴀʟ: {total}\n✅ {sent} | ❌ {failed}",
                progress_msg.chat.id,
                progress_msg.message_id
            )
        except Exception:
            pass

# ---------------- UPLOAD + MEDIA HANDLER ----------------
async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_obj = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg_obj:
        return

    user = update.effective_user
    if is_banned(user.id):
        return await msg_obj.reply_text("🚫 ʏᴏᴜ ᴀʀᴇ ʙᴀɴɴᴇᴅ.")

    ok, missing = await check_force_join_for_user(context.bot, user.id)
    if not ok:
        if any_pending_request(user.id, missing):
            await send_pending_screen(update, context, missing, "")
        else:
            await send_join_required_screen(update, context, missing, "")
        return

    if not (is_admin(user.id) or is_premium(user.id)):
        return await msg_obj.reply_text("⚠️ ʏᴏᴜ ᴀʀᴇ ɴᴏᴛ ᴀᴅᴍɪɴ/ᴘʀᴇᴍɪᴜᴍ.")

    context.user_data["upload_files"] = []
    context.user_data["media_id"] = gen_id()
    await msg_obj.reply_text(
        "sᴇɴᴅ ғɪʟᴇs ɴᴏᴡ. ᴘʀᴇss ✅ ᴡʜᴇɴ ᴅᴏɴᴇ.",
        reply_markup=ReplyKeyboardMarkup([["✅"]], resize_keyboard=True),
    )

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    if await _handle_getid_mode(update, context):
        return

    user = update.effective_user
    ensure_user_record(user.id, user.username)

    if context.user_data.get("awaiting_broadcast"):
        return await _capture_broadcast_content(update, context)

    if is_banned(user.id):
        return await msg.reply_text("🚫 ʏᴏᴜ ᴀʀᴇ ʙᴀɴɴᴇᴅ.")

    ok, missing = await check_force_join_for_user(context.bot, user.id)
    if not ok:
        if any_pending_request(user.id, missing):
            await send_pending_screen(update, context, missing, "")
        else:
            await send_join_required_screen(update, context, missing, "")
        return

    if msg.text and msg.text.strip() == "✅":
        files = context.user_data.get("upload_files", [])
        if not files:
            await msg.reply_text("❌ ɴᴏ ᴍᴇᴅɪᴀ.", reply_markup=ReplyKeyboardRemove())
            return

        media_id = context.user_data.get("media_id")
        if not media_id:
            await msg.reply_text("❌ sᴇssɪᴏɴ ᴇxᴘɪʀᴇᴅ.")
            context.user_data.pop("upload_files", None)
            return

        save_data(media_id, files)
        me = await context.bot.get_me()
        share_link = f"https://t.me/{me.username}?start={media_id}"
        await msg.reply_text(f"✅ ᴜᴘʟᴏᴀᴅᴇᴅ.\n🔗 {share_link}", reply_markup=ReplyKeyboardRemove())

        if PRIVATE_CHANNEL_ID is not None:
            try:
                uname = f"@{user.username}" if user.username else "NoUsername"
                p_text = f"📦 New Upload\n👤 {uname} ({user.id})\n🔗 {share_link}"
                await context.bot.send_message(PRIVATE_CHANNEL_ID, p_text)
            except Exception:
                pass

        context.user_data.clear()
        return

    f = None
    caption = msg.caption or ""

    if msg.photo:
        f = {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": caption}
    elif msg.video:
        f = {"type": "video", "file_id": msg.video.file_id, "caption": caption}
    elif getattr(msg, "video_note", None):
        f = {"type": "video_note", "file_id": msg.video_note.file_id, "caption": ""}
    elif msg.document:
        f = {"type": "document", "file_id": msg.document.file_id, "caption": caption}
    elif msg.animation:
        f = {"type": "animation", "file_id": msg.animation.file_id, "caption": caption}

    if f:
        context.user_data.setdefault("upload_files", []).append(f)
        await msg.reply_text("✅ sᴀᴠᴇᴅ. sᴇɴᴅ ᴍᴏʀᴇ ᴏʀ ᴘʀᴇss ✅.")

# ---------------- JOIN REQUEST HANDLER ----------------
async def on_chat_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr = update.chat_join_request
    if not jr:
        return

    chat_id = jr.chat.id
    user = jr.from_user
    username = user.username

    # store pending
    upsert_join_request(str(chat_id), int(user.id), username, "pending")

    # send to request channel
    if REQUEST_CHANNEL_ID is None:
        return

    uname = f"@{username}" if username else "NoUsername"
    text = (
        "🟡 JOIN REQUEST\n\n"
        f"Chat: {jr.chat.title}\n"
        f"ChatID: {chat_id}\n\n"
        f"User: {uname}\n"
        f"UserID: {user.id}\n"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ ACCEPT", callback_data=f"jr_accept:{chat_id}:{user.id}"),
            InlineKeyboardButton("❌ REJECT", callback_data=f"jr_reject:{chat_id}:{user.id}"),
        ]
    ])

    try:
        await context.bot.send_message(REQUEST_CHANNEL_ID, text, reply_markup=kb)
    except Exception as e:
        logger.warning(f"Failed to send join request to request-channel: {e}")

# ---------------- CALLBACK ROUTER ----------------
async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "ui_about":
        return await send_about_screen(update, context)

    if data == "ui_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data.startswith("confirm_join:"):
        media_id = data.split(":", 1)[1] if ":" in data else ""
        ok, missing = await check_force_join_for_user(context.bot, update.effective_user.id)

        if not ok:
            if any_pending_request(update.effective_user.id, missing):
                await send_pending_screen(update, context, missing, media_id or "")
            else:
                await send_join_required_screen(update, context, missing, media_id or "")
            return

        try:
            await query.message.delete()
        except Exception:
            pass

        if media_id:
            await _send_media_for_media_id(update, context, media_id)
            return

        await send_start_screen(update, context)
        return

    if data == "upload":
        await upload(update, context)
        return

    # ✅ Join Request Approve/Reject
    if data.startswith("jr_accept:") or data.startswith("jr_reject:"):
        if not is_admin(update.effective_user.id):
            return

        parts = data.split(":")
        if len(parts) != 3:
            return

        chat_id = parts[1]
        user_id = int(parts[2])

        if data.startswith("jr_accept:"):
            ok = set_join_request_decision(chat_id, user_id, "approved", update.effective_user.id)
            if ok:
                try:
                    await context.bot.approve_chat_join_request(chat_id=int(chat_id), user_id=user_id)
                except Exception as e:
                    logger.warning(f"approve_chat_join_request failed: {e}")
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                await query.message.reply_text(f"✅ APPROVED: {user_id}")
            else:
                await query.message.reply_text("⚠️ Already decided.")
            return

        if data.startswith("jr_reject:"):
            ok = set_join_request_decision(chat_id, user_id, "rejected", update.effective_user.id)
            if ok:
                try:
                    await context.bot.decline_chat_join_request(chat_id=int(chat_id), user_id=user_id)
                except Exception as e:
                    logger.warning(f"decline_chat_join_request failed: {e}")
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                await query.message.reply_text(f"❌ REJECTED: {user_id}")
            else:
                await query.message.reply_text("⚠️ Already decided.")
            return

    # Broadcast confirm/cancel
    if data.startswith("bc_confirm:") or data.startswith("bc_cancel:"):
        admin_id = int(data.split(":", 1)[1])
        if update.effective_user.id != admin_id:
            return await query.message.reply_text("ᴏɴʟʏ ᴀᴅᴍɪɴ ᴄᴀɴ ᴄᴏɴғɪʀᴍ/ᴄᴀɴᴄᴇʟ.")

        if data.startswith("bc_cancel:"):
            context.user_data.pop("broadcast_pending", None)
            context.user_data.pop("broadcast_preview_message", None)
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return await query.message.reply_text("❌ ᴄᴀɴᴄᴇʟʟᴇᴅ.")

        payload = context.user_data.get("broadcast_pending")
        info = context.user_data.get("broadcast_preview_message")
        if not payload or not info:
            return await query.message.reply_text("ɴᴏ ʙʀᴏᴀᴅᴄᴀsᴛ.")

        try:
            await context.bot.edit_message_reply_markup(info["chat_id"], info["message_id"], reply_markup=None)
        except Exception:
            pass

        progress_msg = None
        try:
            progress_msg = await context.bot.send_message(info["chat_id"], "📡 ʙʀᴏᴀᴅᴄᴀsᴛɪɴɢ...")
        except Exception:
            pass

        asyncio.create_task(_run_broadcast_task(context.bot, payload, progress_msg))
        context.user_data.pop("broadcast_pending", None)
        context.user_data.pop("broadcast_preview_message", None)
        await query.message.reply_text("✅ sᴛᴀʀᴛᴇᴅ.")
        return

# ---------------- ERROR HANDLER ----------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Unhandled error: {context.error}")
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ ᴇʀʀᴏʀ. ᴛʀʏ ᴀɢᴀɪɴ.")
    except Exception:
        pass

# ---------------- BOT COMMAND MENU ----------------
async def set_bot_commands(app):
    commands = [
        BotCommand("start", "sᴛᴀʀᴛ"),
        BotCommand("about", "ᴀʙᴏᴜᴛ"),
        BotCommand("credits", "ᴀʙᴏᴜᴛ"),
        BotCommand("profile", "ᴘʀᴏғɪʟᴇ"),
        BotCommand("request", "sᴇɴᴅ ʀᴇǫᴜᴇsᴛ (to admins)"),
        BotCommand("givefont", "ғᴏɴᴛ ᴄᴏᴘʏ"),
        BotCommand("getfont", "convert text to fancy font"),
        BotCommand("getid", "ɢᴇᴛ ғɪʟᴇ_ɪᴅ"),
        BotCommand("upload", "ᴜᴘʟᴏᴀᴅ (ᴀᴅᴍɪɴ/ᴘʀᴇᴍɪᴜᴍ)"),
        BotCommand("cmd", "ᴀᴅᴍɪɴ ᴍᴇɴᴜ"),
        BotCommand("stats", "sᴛᴀᴛs (ᴀᴅᴍɪɴ)"),
        BotCommand("users", "ᴜsᴇʀs (ᴀᴅᴍɪɴ)"),
        BotCommand("broadcast", "ʙʀᴏᴀᴅᴄᴀsᴛ (ᴀᴅᴍɪɴ)"),
        BotCommand("pbroadcast", "ᴘʀᴇᴍɪᴜᴍ ʙʀᴏᴀᴅᴄᴀsᴛ (ᴀᴅᴍɪɴ)"),
        BotCommand("ban", "ʙᴀɴ (ᴀᴅᴍɪɴ)"),
        BotCommand("unban", "ᴜɴʙᴀɴ (ᴀᴅᴍɪɴ)"),
        BotCommand("premium", "ᴀᴅᴅ ᴘʀᴇᴍɪᴜᴍ (ᴀᴅᴍɪɴ)"),
        BotCommand("unpremium", "ʀᴇᴍᴏᴠᴇ ᴘʀᴇᴍɪᴜᴍ (ᴀᴅᴍɪɴ)"),
        BotCommand("premiumusers", "ᴘʀᴇᴍɪᴜᴍ ᴜsᴇʀs (ᴀᴅᴍɪɴ)"),
        BotCommand("setphoto", "sᴇᴛ sᴛᴀʀᴛ ᴘʜᴏᴛᴏ (ᴀᴅᴍɪɴ)"),
        BotCommand("set", "ᴀᴅᴅ ғᴏʀᴄᴇ-ᴊᴏɪɴ (ᴀᴅᴍɪɴ)"),
        BotCommand("remove", "ʀᴇᴍᴏᴠᴇ ғᴏʀᴄᴇ-ᴊᴏɪɴ (ᴀᴅᴍɪɴ)"),
        BotCommand("listchannels", "ʟɪsᴛ ғᴏʀᴄᴇ-ᴊᴏɪɴ (ᴀᴅᴍɪɴ)"),
        BotCommand("del", "ᴅᴇʟ ᴍᴇᴅɪᴀ (ᴀᴅᴍɪɴ)"),
        BotCommand("genlink", "ɢᴇɴ ʟɪɴᴋ (ᴀᴅᴍɪɴ)"),
        BotCommand("usage", "ᴜsᴀɢᴇ (ᴀᴅᴍɪɴ)"),
        BotCommand("addadmin", "ᴀᴅᴅ ᴀᴅᴍɪɴ (ᴏᴡɴᴇʀ)"),
        BotCommand("removeadmin", "ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ (ᴏᴡɴᴇʀ)"),
        BotCommand("adminlist", "ᴀᴅᴍɪɴ ʟɪsᴛ (ᴏᴡɴᴇʀ)"),
    ]
    try:
        await app.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Could not set bot commands: {e}")

# ---------------- MAIN (Timeout Fix + Retry) ----------------
async def main_async():
    ensure_default_force_channel()

    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .build()
    )

    # Core
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("credits", cmd_about))

    # User
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("request", cmd_request))
    app.add_handler(CommandHandler("givefont", cmd_givefont))
    app.add_handler(CommandHandler("getfont", cmd_getfont))

    # Tools
    app.add_handler(CommandHandler("getid", cmd_getid))
    app.add_handler(CommandHandler("upload", upload))

    # Admin
    app.add_handler(CommandHandler("cmd", cmd_cmd))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("users", cmd_users))

    app.add_handler(CommandHandler("premium", make_premium))
    app.add_handler(CommandHandler("unpremium", remove_premium))
    app.add_handler(CommandHandler("premiumusers", cmd_premiumusers))

    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))

    app.add_handler(CommandHandler("del", cmd_delete))
    app.add_handler(CommandHandler("genlink", cmd_genlink))
    app.add_handler(CommandHandler("usage", cmd_usage))

    app.add_handler(CommandHandler("setphoto", cmd_setphoto))

    app.add_handler(CommandHandler("set", cmd_set_force))
    app.add_handler(CommandHandler("remove", cmd_remove_force))
    app.add_handler(CommandHandler("listchannels", cmd_listchannels))

    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("pbroadcast", pbroadcast_command))

    # Owner
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("adminlist", cmd_adminlist))

    # ✅ Join request updates (Invite link "Request Admin Approval")
    app.add_handler(ChatJoinRequestHandler(on_chat_join_request))

    # Callbacks + media
    app.add_handler(CallbackQueryHandler(callback_query_router))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_media))

    app.add_error_handler(error_handler)

    for attempt in range(1, 6):
        try:
            await app.initialize()
            break
        except Exception as e:
            logger.warning(f"Init failed (attempt {attempt}/5): {e}")
            await asyncio.sleep(3)
    else:
        raise RuntimeError("Failed to initialize bot. Check internet/VPN/DNS/Token.")

    await set_bot_commands(app)
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    logger.info("🚀 Bot running (PostgreSQL + Force-Join + Join-Request Approval + /getfont)...")

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        try:
            conn.close()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main_async())

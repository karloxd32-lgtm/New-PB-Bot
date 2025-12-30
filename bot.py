import asyncio
import json
import logging
import os
import random
import string
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# ---------------------------- CONFIG ----------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "7975253707:AAF7-qHadg8CZKYxMsQD7QcIPoMg7DwLkvo").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres.gitugrdddywbgjinmmhj:LuffyBotsX11@aws-1-ap-south-1.pooler.supabase.com:5432/postgres").strip()

OWNER_ID = int(os.getenv("OWNER_ID", "6847499628").strip())

DEFAULT_FORCE_CHANNEL_LINK = os.getenv(
    "DEFAULT_FORCE_CHANNEL_LINK", "https://t.me/+UKFj-D0zB85hNDNl"
).strip()
DEFAULT_FORCE_CHANNEL_ID = os.getenv("DEFAULT_FORCE_CHANNEL_ID", "-1002699957030").strip()
DEFAULT_FORCE_BUTTON_NAME = os.getenv("DEFAULT_FORCE_BUTTON_NAME", "✅ Join Channel").strip()

PRIVATE_CHANNEL_ID_ENV = os.getenv("PRIVATE_CHANNEL_ID", "").strip()
PRIVATE_CHANNEL_ID = int(PRIVATE_CHANNEL_ID_ENV) if PRIVATE_CHANNEL_ID_ENV else None

MAIN_CHANNEL_LINK = os.getenv("MAIN_CHANNEL_LINK", DEFAULT_FORCE_CHANNEL_LINK).strip()

# Auto delete after 3 hours (10800 seconds)
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", str(3 * 60 * 60)).strip())

DAILY_LIMIT_TZ = os.getenv("DAILY_LIMIT_TZ", "Asia/Kolkata").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set BOT_TOKEN in Railway/Hosting env variables.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing. Set DATABASE_URL in Railway/Hosting env variables.")

# ---------------------------- LOGGING ----------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("file_store_bot")

# ---------------------------- DB (POOL) ----------------------------

_db_pool: Optional[ThreadedConnectionPool] = None


def init_db_pool() -> ThreadedConnectionPool:
    global _db_pool
    if _db_pool is not None:
        return _db_pool

    _db_pool = ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=DATABASE_URL,
        sslmode="require",
    )
    return _db_pool


def _db_exec(
    query: str,
    params: Optional[Tuple[Any, ...]] = None,
    fetchone: bool = False,
    fetchall: bool = False,
    commit: bool = False,
) -> Any:
    pool = init_db_pool()
    conn = None
    try:
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(query, params)
            result = None
            if fetchone:
                result = cur.fetchone()
            elif fetchall:
                result = cur.fetchall()
            if commit:
                conn.commit()
            return result
    except psycopg2.OperationalError as e:
        logger.error("DB OperationalError: %s", e)
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        # Recreate pool once (idle disconnects)
        try:
            global _db_pool
            if _db_pool:
                _db_pool.closeall()
            _db_pool = None
            init_db_pool()
        except Exception as e2:
            logger.error("DB pool recreate failed: %s", e2)
        raise
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn:
            pool.putconn(conn)


def ensure_schema() -> None:
    _db_exec(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            active INTEGER DEFAULT 1,
            premium INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0
        )
        """,
        commit=True,
    )
    _db_exec(
        """
        CREATE TABLE IF NOT EXISTS media_files (
            media_id TEXT PRIMARY KEY,
            files TEXT
        )
        """,
        commit=True,
    )
    _db_exec(
        """
        CREATE TABLE IF NOT EXISTS force_join_channels (
            id SERIAL PRIMARY KEY,
            channel_link TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            button_name TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            UNIQUE(channel_link, chat_id, button_name)
        )
        """,
        commit=True,
    )
    _db_exec(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """,
        commit=True,
    )
    _db_exec(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            id SERIAL PRIMARY KEY,
            media_id TEXT NOT NULL,
            user_id BIGINT NOT NULL,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        commit=True,
    )
    _db_exec(
        """
        CREATE TABLE IF NOT EXISTS admins (
            user_id BIGINT PRIMARY KEY,
            added_by BIGINT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        commit=True,
    )


# ---------------------------- SETTINGS HELPERS ----------------------------

def set_setting(key: str, value: str) -> None:
    _db_exec(
        """
        INSERT INTO settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        (key, value),
        commit=True,
    )


def get_setting(key: str) -> Optional[str]:
    row = _db_exec("SELECT value FROM settings WHERE key = %s", (key,), fetchone=True)
    return row[0] if row else None


def get_start_photo_id() -> Optional[str]:
    return get_setting("start_photo_file_id")


# Delivery join button settings (button shown AFTER media delivery)
def get_delivery_button() -> Tuple[str, str, str]:
    link = get_setting("delivery_channel_link") or MAIN_CHANNEL_LINK
    chat_id = get_setting("delivery_chat_id") or ""
    name = get_setting("delivery_button_name") or "📢 Join Channel"
    return link, chat_id, name


# ---------------------------- FONT SYSTEM ----------------------------

FONT_STYLES = {"normal", "smallcaps", "bold", "italic", "mono"}
FONT_STYLE = "smallcaps"  # loaded from DB

_SMALLCAPS_MAP = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ғ", "g": "ɢ", "h": "ʜ",
    "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ", "o": "ᴏ", "p": "ᴘ",
    "q": "ǫ", "r": "ʀ", "s": "s", "t": "ᴛ", "u": "ᴜ", "v": "ᴠ", "w": "ᴡ", "x": "x",
    "y": "ʏ", "z": "ᴢ",
}


def _math_alpha(ch: str, base_upper: int, base_lower: int, base_digit: Optional[int] = None) -> str:
    o = ord(ch)
    if 65 <= o <= 90:  # A-Z
        return chr(base_upper + (o - 65))
    if 97 <= o <= 122:  # a-z
        return chr(base_lower + (o - 97))
    if base_digit is not None and 48 <= o <= 57:  # 0-9
        return chr(base_digit + (o - 48))
    return ch


def apply_font(text: str, style: Optional[str] = None) -> str:
    st = (style or FONT_STYLE or "normal").strip().lower()
    if st not in FONT_STYLES:
        st = "normal"

    if st == "normal":
        return text

    if st == "smallcaps":
        out = []
        for ch in text:
            low = ch.lower()
            out.append(_SMALLCAPS_MAP.get(low, ch))
        return "".join(out)

    if st == "bold":
        return "".join(_math_alpha(ch, 0x1D400, 0x1D41A, 0x1D7CE) for ch in text)

    if st == "italic":
        return "".join(_math_alpha(ch, 0x1D434, 0x1D44E, None) for ch in text)

    if st == "mono":
        return "".join(_math_alpha(ch, 0x1D670, 0x1D68A, 0x1D7F6) for ch in text)

    return text


def load_font_from_db() -> None:
    global FONT_STYLE
    v = (get_setting("font_style") or "smallcaps").strip().lower()
    FONT_STYLE = v if v in FONT_STYLES else "smallcaps"


# ---------------------------- MESSAGE HELPERS ----------------------------

def protect_kwargs() -> Dict[str, Any]:
    # Protected content: blocks forward/save on supported Telegram clients.
    return {"protect_content": True}


async def send_text(msg: Message, text: str, protect: bool = True, **kwargs):
    # Styled text (may break links if used on URLs)
    txt = apply_font(text)
    if protect:
        return await msg.reply_text(txt, **protect_kwargs(), **kwargs)
    return await msg.reply_text(txt, **kwargs)


async def send_plain_text(msg: Message, text: str, **kwargs):
    # No font + no protect_content => easy COPY + links work
    return await msg.reply_text(text, **kwargs)


async def send_plain_html(msg: Message, html: str, **kwargs):
    return await msg.reply_text(html, parse_mode="HTML", disable_web_page_preview=True, **kwargs)


# ---------------------------- USERS / ADMIN ----------------------------

def ensure_user_record(user_id: int, username: Optional[str]) -> None:
    _db_exec(
        """
        INSERT INTO users (user_id, username, active, premium, banned)
        VALUES (%s, %s, 1, 0, 0)
        ON CONFLICT (user_id) DO UPDATE
          SET username = EXCLUDED.username, active = 1
        """,
        (user_id, username),
        commit=True,
    )


def is_owner(user_id: int) -> bool:
    return int(user_id) == int(OWNER_ID)


def get_admin_ids_from_db() -> List[int]:
    rows = _db_exec("SELECT user_id FROM admins", fetchall=True) or []
    return [int(r[0]) for r in rows]


def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or (int(user_id) in set(get_admin_ids_from_db()))


def add_admin_db(user_id: int, added_by: int) -> None:
    if is_owner(user_id):
        return
    _db_exec(
        """
        INSERT INTO admins (user_id, added_by)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET added_by = EXCLUDED.added_by
        """,
        (int(user_id), int(added_by)),
        commit=True,
    )


def remove_admin_db(user_id: int) -> bool:
    if is_owner(user_id):
        return False
    _db_exec("DELETE FROM admins WHERE user_id = %s", (int(user_id),), commit=True)
    return True


def list_admins_all() -> List[int]:
    ids = set(get_admin_ids_from_db())
    ids.add(int(OWNER_ID))
    return sorted(ids)


def set_premium(user_id: int, value: bool) -> None:
    _db_exec(
        """
        INSERT INTO users (user_id, username, active, premium, banned)
        VALUES (%s, NULL, 1, 0, 0)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id,),
        commit=True,
    )
    _db_exec(
        "UPDATE users SET premium = %s WHERE user_id = %s",
        (1 if value else 0, user_id),
        commit=True,
    )


def is_premium(user_id: int) -> bool:
    row = _db_exec("SELECT premium FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    return bool(row and row[0])


def ban_user(user_id: int) -> None:
    _db_exec(
        """
        INSERT INTO users (user_id, username, active, premium, banned)
        VALUES (%s, NULL, 1, 0, 0)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id,),
        commit=True,
    )
    _db_exec("UPDATE users SET banned = 1 WHERE user_id = %s", (user_id,), commit=True)


def unban_user(user_id: int) -> None:
    _db_exec(
        """
        INSERT INTO users (user_id, username, active, premium, banned)
        VALUES (%s, NULL, 1, 0, 0)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id,),
        commit=True,
    )
    _db_exec("UPDATE users SET banned = 0 WHERE user_id = %s", (user_id,), commit=True)


def is_banned(user_id: int) -> bool:
    row = _db_exec("SELECT banned FROM users WHERE user_id = %s", (user_id,), fetchone=True)
    return bool(row and row[0])


# ---------------------------- MEDIA STORAGE ----------------------------

def gen_id(length: int = 12) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def save_data(media_id: str, files: list) -> None:
    _db_exec(
        """
        INSERT INTO media_files (media_id, files) VALUES (%s, %s)
        ON CONFLICT (media_id) DO UPDATE SET files = EXCLUDED.files
        """,
        (media_id, json.dumps(files, ensure_ascii=False)),
        commit=True,
    )


def get_data(media_id: str) -> Optional[list]:
    row = _db_exec("SELECT files FROM media_files WHERE media_id = %s", (media_id,), fetchone=True)
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def log_download(media_id: str, user_id: int) -> None:
    _db_exec("INSERT INTO downloads (media_id, user_id) VALUES (%s, %s)", (media_id, user_id), commit=True)


def get_nonbanned_user_ids() -> List[int]:
    rows = _db_exec("SELECT user_id FROM users WHERE banned = 0", fetchall=True) or []
    return [int(r[0]) for r in rows]


def get_premium_user_ids() -> List[int]:
    rows = _db_exec("SELECT user_id FROM users WHERE banned = 0 AND premium = 1", fetchall=True) or []
    return [int(r[0]) for r in rows]


# ---------------------------- DAILY LIMIT ----------------------------

def get_daily_limit() -> int:
    v = get_setting("daily_limit")
    if not v:
        return 0
    try:
        return max(0, int(v))
    except Exception:
        return 0


def set_daily_limit(limit: int) -> None:
    set_setting("daily_limit", str(max(0, int(limit))))


def remove_daily_limit() -> None:
    set_setting("daily_limit", "0")


def count_user_downloads_today(user_id: int) -> int:
    row = _db_exec(
        """
        SELECT COUNT(*)
        FROM downloads
        WHERE user_id = %s
          AND DATE(timezone(%s, ts)) = DATE(timezone(%s, now()))
        """,
        (user_id, DAILY_LIMIT_TZ, DAILY_LIMIT_TZ),
        fetchone=True,
    )
    return int(row[0]) if row else 0


# ---------------------------- FORCE JOIN ----------------------------

def add_force_channel(channel_link: str, chat_id: str, button_name: str) -> None:
    _db_exec(
        """
        INSERT INTO force_join_channels (channel_link, chat_id, button_name, enabled)
        VALUES (%s, %s, %s, 1)
        ON CONFLICT (channel_link, chat_id, button_name) DO UPDATE SET enabled = 1
        """,
        (channel_link.strip(), str(chat_id).strip(), button_name.strip()),
        commit=True,
    )


def remove_force_channel(channel_link: str, chat_id: str, button_name: str) -> None:
    _db_exec(
        "DELETE FROM force_join_channels WHERE channel_link = %s AND chat_id = %s AND button_name = %s",
        (channel_link.strip(), str(chat_id).strip(), button_name.strip()),
        commit=True,
    )


def get_force_channels() -> List[Tuple[str, str, str]]:
    rows = _db_exec(
        "SELECT channel_link, chat_id, button_name FROM force_join_channels WHERE enabled = 1 ORDER BY id ASC",
        fetchall=True,
    ) or []
    return [(r[0], r[1], r[2]) for r in rows]


def ensure_default_force_channel() -> None:
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
            missing.append((channel_link, chat_id, button_name))

    return (len(missing) == 0), missing


# ---------------------------- AUTO DELETE ----------------------------

async def schedule_delete_message(bot, chat_id: int, message_id: int, delay: int):
    if delay <= 0:
        return

    async def _delete():
        try:
            await asyncio.sleep(delay)
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            return

    asyncio.create_task(_delete())


# ---------------------------- UI TEXT ----------------------------

BTN_ABOUT = "About"
BTN_CLOSE = "Close"
BTN_UPLOAD = "📤 Start Upload"
BTN_I_JOINED = "✅ I Joined"

START_TEXT = (
    "Hello!\n\n"
    "I am a file store bot.\n"
    "• Admin/Premium users can upload files.\n"
    "• Others can access files using a special link.\n"
)

ABOUT_TEXT_HTML = (
    "<b>About</b>\n\n"
    "Developer: @LuffyBots\n"
    'Built with: <a href="https://github.com/python-telegram-bot/python-telegram-bot">python-telegram-bot</a>\n'
    "Language: Python\n"
    "Database: PostgreSQL\n"
)


async def send_start_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = get_start_photo_id()
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(BTN_ABOUT, callback_data="ui_about"),
                InlineKeyboardButton(BTN_CLOSE, callback_data="ui_close"),
            ],
            [InlineKeyboardButton(BTN_UPLOAD, callback_data="upload")],
        ]
    )
    msg = update.effective_message
    if not msg:
        return

    # Start screen should remain normal + copy-friendly
    if photo_id:
        await msg.reply_photo(photo_id, caption=START_TEXT, reply_markup=keyboard)
    else:
        await send_plain_text(msg, START_TEXT, reply_markup=keyboard)


async def send_about_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = get_start_photo_id()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(BTN_CLOSE, callback_data="ui_close")]])
    msg = update.effective_message
    if not msg:
        return

    if photo_id:
        await msg.reply_photo(photo_id, caption=ABOUT_TEXT_HTML, reply_markup=kb, parse_mode="HTML")
    else:
        await send_plain_html(msg, ABOUT_TEXT_HTML, reply_markup=kb)


async def send_join_required_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, missing, media_id: str):
    photo_id = get_start_photo_id()
    buttons = []
    for channel_link, _, button_name in missing:
        buttons.append([InlineKeyboardButton(button_name, url=channel_link)])
    buttons.append([InlineKeyboardButton(BTN_I_JOINED, callback_data=f"confirm_join:{media_id}")])
    markup = InlineKeyboardMarkup(buttons)

    text = "Your file is ready, but you must join our channel(s) first."
    msg = update.effective_message
    if not msg:
        return

    if photo_id:
        await msg.reply_photo(photo_id, caption=text, reply_markup=markup, **protect_kwargs())
    else:
        await send_text(msg, text, protect=True, reply_markup=markup)


# ---------------------------- MEDIA DELIVERY ----------------------------

async def _send_media_for_media_id(update: Update, context: ContextTypes.DEFAULT_TYPE, media_id: str):
    target_msg = update.effective_message
    if not target_msg:
        return

    user_id = update.effective_user.id

    # Daily limit (premium/admin unlimited)
    limit = get_daily_limit()
    if limit > 0 and (not is_premium(user_id)) and (not is_admin(user_id)):
        used = count_user_downloads_today(user_id)
        if used >= limit:
            await send_plain_text(
                target_msg,
                f"Daily limit reached.\n\nLimit: {limit}/day\nUsed today: {used}\n\nContact admin for premium (unlimited).",
            )
            return

    files = get_data(media_id)
    if not files:
        await send_plain_text(target_msg, "This media is expired or not found.")
        return

    # Processing msg can be styled, doesn't matter
    processing = await send_text(target_msg, "Processing...", protect=True)
    await asyncio.sleep(0.6)

    sent_messages: List[Message] = []

    # IMPORTANT FIX:
    # Remove protect_content from delivered media so users can share/forward/download.
    for f in files:
        try:
            t = f.get("type")
            caption = f.get("caption", "") or ""
            file_id = f.get("file_id")

            sent_msg: Optional[Message] = None
            if t == "photo":
                sent_msg = await target_msg.reply_photo(file_id, caption=caption)
            elif t == "video":
                sent_msg = await target_msg.reply_video(file_id, caption=caption)
            elif t == "document":
                sent_msg = await target_msg.reply_document(file_id, caption=caption)
            elif t == "animation":
                sent_msg = await target_msg.reply_animation(file_id, caption=caption)
            elif t == "video_note":
                sent_msg = await context.bot.send_video_note(target_msg.chat.id, file_id)

            if sent_msg:
                sent_messages.append(sent_msg)
        except Exception as e:
            logger.exception("Send failed for media_id=%s: %s", media_id, e)

    try:
        await processing.delete()
    except Exception:
        pass

    try:
        log_download(media_id, user_id)
    except Exception:
        pass

    # After media: join button (normal)
    delivery_link, _, delivery_btn_name = get_delivery_button()
    join_btn = InlineKeyboardMarkup([[InlineKeyboardButton(delivery_btn_name, url=delivery_link)]])

    msg2 = await send_plain_text(
        target_msg,
        f"Auto-delete time: {AUTO_DELETE_SECONDS // 3600} hours.\nJoin the channel below:",
        reply_markup=join_btn,
    )

    if AUTO_DELETE_SECONDS > 0:
        for m in sent_messages:
            await schedule_delete_message(context.bot, m.chat.id, m.message_id, AUTO_DELETE_SECONDS)
        await schedule_delete_message(context.bot, msg2.chat.id, msg2.message_id, AUTO_DELETE_SECONDS)


# ---------------------------- BROADCAST (ADMIN) ----------------------------

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return

    args = context.args
    if args:
        text = " ".join(args)
        context.user_data["broadcast_pending"] = {"type": "text", "text": text, "target": "all"}
        await _send_broadcast_preview(update, context)
        return

    context.user_data["awaiting_broadcast"] = True
    context.user_data["broadcast_target"] = "all"
    await send_text(update.effective_message, "Send broadcast content now.", protect=True)


async def pbroadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return

    args = context.args
    if args:
        text = " ".join(args)
        context.user_data["broadcast_pending"] = {"type": "text", "text": text, "target": "premium"}
        await _send_broadcast_preview(update, context)
        return

    context.user_data["awaiting_broadcast"] = True
    context.user_data["broadcast_target"] = "premium"
    await send_text(update.effective_message, "Send premium broadcast content now.", protect=True)


async def _capture_broadcast_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    if not is_admin(update.effective_user.id):
        context.user_data.pop("awaiting_broadcast", None)
        await send_text(msg, "Admin only.", protect=True)
        return

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
        await send_text(msg, "Unsupported content.", protect=True)
        return

    context.user_data.pop("awaiting_broadcast", None)
    context.user_data["broadcast_pending"] = payload
    await _send_broadcast_preview(update, context)


async def _send_broadcast_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = context.user_data.get("broadcast_pending")
    if not payload:
        await send_text(update.effective_message, "No broadcast content.", protect=True)
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Confirm", callback_data=f"bc_confirm:{update.effective_user.id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"bc_cancel:{update.effective_user.id}")],
        ]
    )

    try:
        if payload["type"] == "text":
            preview = await update.effective_message.reply_text(
                apply_font(f"Broadcast preview:\n\n{payload['text']}"),
                reply_markup=keyboard,
                **protect_kwargs(),
            )
        elif payload["type"] == "photo":
            preview = await update.effective_message.reply_photo(
                payload["file_id"],
                caption=payload.get("caption", ""),
                reply_markup=keyboard,
                **protect_kwargs(),
            )
        elif payload["type"] == "video":
            preview = await update.effective_message.reply_video(
                payload["file_id"],
                caption=payload.get("caption", ""),
                reply_markup=keyboard,
                **protect_kwargs(),
            )
        elif payload["type"] == "document":
            preview = await update.effective_message.reply_document(
                payload["file_id"],
                caption=payload.get("caption", ""),
                reply_markup=keyboard,
                **protect_kwargs(),
            )
        elif payload["type"] == "animation":
            preview = await update.effective_message.reply_animation(
                payload["file_id"],
                caption=payload.get("caption", ""),
                reply_markup=keyboard,
                **protect_kwargs(),
            )
        elif payload["type"] == "video_note":
            preview = await update.effective_message.reply_video_note(
                payload["file_id"],
                reply_markup=keyboard,
                **protect_kwargs(),
            )
        else:
            await send_text(update.effective_message, "Unsupported preview.", protect=True)
            return
    except Exception as e:
        logger.exception("Preview send failed: %s", e)
        await send_text(update.effective_message, "Failed to send preview.", protect=True)
        return

    context.user_data["broadcast_preview_message"] = {"chat_id": preview.chat.id, "message_id": preview.message_id}


async def _run_broadcast_task(bot, payload: Dict[str, Any], progress_msg: Optional[Message]):
    target = payload.get("target", "all")
    users = get_premium_user_ids() if target == "premium" else get_nonbanned_user_ids()

    total = len(users)
    sent = 0
    failed = 0

    async def update_progress(done: int):
        if not progress_msg:
            return
        try:
            await bot.edit_message_text(
                apply_font(f"Broadcasting...\nSent: {done}/{total}\n✅ {sent} | ❌ {failed}"),
                progress_msg.chat.id,
                progress_msg.message_id,
            )
        except Exception:
            pass

    for idx, uid in enumerate(users, start=1):
        try:
            if payload["type"] == "text":
                await bot.send_message(uid, payload["text"], **protect_kwargs())
            elif payload["type"] == "photo":
                await bot.send_photo(uid, payload["file_id"], caption=payload.get("caption", ""), **protect_kwargs())
            elif payload["type"] == "video":
                await bot.send_video(uid, payload["file_id"], caption=payload.get("caption", ""), **protect_kwargs())
            elif payload["type"] == "video_note":
                await bot.send_video_note(uid, payload["file_id"], **protect_kwargs())
            elif payload["type"] == "document":
                await bot.send_document(uid, payload["file_id"], caption=payload.get("caption", ""), **protect_kwargs())
            elif payload["type"] == "animation":
                await bot.send_animation(uid, payload["file_id"], caption=payload.get("caption", ""), **protect_kwargs())
            else:
                await bot.send_message(uid, "Message from admin", **protect_kwargs())
            sent += 1
        except Exception:
            failed += 1

        if idx % 10 == 0 or idx == total:
            await update_progress(idx)

        await asyncio.sleep(0.03)

    if progress_msg:
        try:
            await bot.edit_message_text(
                apply_font(f"Done.\nTotal: {total}\n✅ {sent} | ❌ {failed}"),
                progress_msg.chat.id,
                progress_msg.message_id,
            )
        except Exception:
            pass


# ---------------------------- COMMANDS ----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user_record(user.id, user.username)

    if is_banned(user.id):
        await send_text(update.effective_message, "You are banned.", protect=True)
        return

    media_id = context.args[0] if context.args else ""
    ok, missing = await check_force_join_for_user(context.bot, user.id)

    if media_id:
        if not ok:
            await send_join_required_screen(update, context, missing, media_id)
            return
        await _send_media_for_media_id(update, context, media_id)
        return

    if not ok:
        await send_join_required_screen(update, context, missing, "")
        return

    await send_start_screen(update, context)


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_about_screen(update, context)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user_record(u.id, u.username)
    prem = "YES" if is_premium(u.id) else "NO"
    ban = "YES" if is_banned(u.id) else "NO"
    adm = "YES" if is_admin(u.id) else "NO"

    text = (
        "Profile\n\n"
        f"ID: {u.id}\n"
        f"Username: @{u.username or 'None'}\n"
        f"Admin: {adm}\n"
        f"Premium: {prem}\n"
        f"Banned: {ban}\n"
    )
    # COPY FIX
    await send_plain_text(update.effective_message, text)


# /getfont (ADMIN ONLY + COPY FIX)
async def cmd_getfont(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /getfont <text>", protect=True)
        return

    raw = " ".join(context.args)
    styled = apply_font(raw)
    await send_plain_text(update.effective_message, styled)


async def cmd_givefont(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_getfont(update, context)


# /setfont (ADMIN ONLY)
async def cmd_setfont(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return
    if not context.args:
        await send_text(
            update.effective_message,
            "Usage: /setfont <style>\n\nAvailable styles: normal, smallcaps, bold, italic, mono",
            protect=True,
        )
        return

    style = context.args[0].strip().lower()
    if style not in FONT_STYLES:
        await send_text(
            update.effective_message,
            "Invalid style.\nAvailable styles: normal, smallcaps, bold, italic, mono",
            protect=True,
        )
        return

    set_setting("font_style", style)
    load_font_from_db()
    await send_text(update.effective_message, f"Font style updated to: {style}", protect=True)


# /dset (ADMIN ONLY): delivery join button after media
async def cmd_dset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return
    if len(context.args) < 3:
        await send_text(update.effective_message, "Usage: /dset <channel_link> <chat_id> <button_name>", protect=True)
        return

    channel_link = context.args[0].strip()
    chat_id = context.args[1].strip()
    button_name = " ".join(context.args[2:]).strip()

    set_setting("delivery_channel_link", channel_link)
    set_setting("delivery_chat_id", chat_id)
    set_setting("delivery_button_name", button_name)

    await send_text(update.effective_message, "Delivery join button updated.", protect=True)


# /getid
async def cmd_getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_getid"] = True
    await send_text(update.effective_message, "Send any photo/video/document.\nI will reply with file_id.", protect=True)


async def _handle_getid_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("awaiting_getid"):
        return False

    msg = update.effective_message
    if not msg:
        return True

    context.user_data["awaiting_getid"] = False

    if msg.photo:
        best = msg.photo[-1].file_id
        await send_plain_text(msg, f"Photo file_id:\n\n{best}")
        return True
    if msg.video:
        await send_plain_text(msg, f"Video file_id:\n\n{msg.video.file_id}")
        return True
    if msg.document:
        await send_plain_text(msg, f"Document file_id:\n\n{msg.document.file_id}")
        return True
    if msg.animation:
        await send_plain_text(msg, f"Animation file_id:\n\n{msg.animation.file_id}")
        return True
    if getattr(msg, "video_note", None):
        await send_plain_text(msg, f"Video note file_id:\n\n{msg.video_note.file_id}")
        return True

    await send_text(msg, "Supported: photo / video / document / animation / video_note", protect=True)
    return True


# ---------------------------- DAILY LIMIT COMMANDS ----------------------------

async def cmd_setlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /setlimit <number>", protect=True)
        return
    try:
        n = int(context.args[0])
        if n < 0:
            n = 0
    except Exception:
        await send_text(update.effective_message, "Invalid number.", protect=True)
        return

    set_daily_limit(n)
    if n == 0:
        await send_text(update.effective_message, "Daily limit disabled.", protect=True)
    else:
        await send_text(update.effective_message, f"Daily limit set to {n}/day (premium/admin unlimited).", protect=True)


async def cmd_removelimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return
    remove_daily_limit()
    await send_text(update.effective_message, "Daily limit removed (disabled).", protect=True)


# ---------------------------- FORCE JOIN COMMANDS ----------------------------

async def cmd_set_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return
    if len(context.args) < 3:
        await send_text(update.effective_message, "Usage: /set <channel_link> <chat_id> <button_name>", protect=True)
        return
    channel_link = context.args[0]
    chat_id = context.args[1]
    button_name = " ".join(context.args[2:]).strip()
    add_force_channel(channel_link, chat_id, button_name)
    await send_text(update.effective_message, "Force-join channel added.", protect=True)


async def cmd_remove_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return
    if len(context.args) < 3:
        await send_text(update.effective_message, "Usage: /remove <channel_link> <chat_id> <button_name>", protect=True)
        return
    channel_link = context.args[0]
    chat_id = context.args[1]
    button_name = " ".join(context.args[2:]).strip()
    remove_force_channel(channel_link, chat_id, button_name)
    await send_text(update.effective_message, "Removed (if exact match existed).", protect=True)


async def cmd_listchannels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return
    rows = get_force_channels()
    if not rows:
        await send_plain_text(update.effective_message, "No force-join channels.")
        return

    lines = ["Force-join channels:"]
    for i, (link, cid, name) in enumerate(rows, start=1):
        lines.append(f"{i}. {name} | {cid}\n{link}")

    # COPY FIX: plain, no font, no protect
    await send_plain_text(update.effective_message, "\n\n".join(lines))


# ---------------------------- SET START PHOTO ----------------------------

async def cmd_setphoto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /setphoto <file_id>", protect=True)
        return
    file_id = context.args[0].strip()
    set_setting("start_photo_file_id", file_id)
    await send_text(update.effective_message, "Start/About photo saved.", protect=True)


# ---------------------------- ADMIN MENU ----------------------------

async def cmd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    text = (
        "Admin Menu\n\n"
        "User:\n"
        "/start\n/about\n/credits\n/profile\n/getid\n\n"
        "Font:\n"
        "/setfont <style>\n/getfont <text>\n\n"
        "Admin:\n"
        "/upload\n/stats\n/users\n/broadcast\n/pbroadcast\n"
        "/ban <id>\n/unban <id>\n"
        "/premium <id>\n/unpremium <id>\n/premiumusers\n"
        "/del <media_id>\n/genlink <media_id>\n/usage <media_id>\n"
        "/setphoto <file_id>\n"
        "/set <channel_link> <chat_id> <button_name>\n"
        "/remove <channel_link> <chat_id> <button_name>\n"
        "/listchannels\n"
        "/dset <channel_link> <chat_id> <button_name>\n"
        "/setlimit <number>\n/removelimit\n\n"
        "Owner:\n"
        "/addadmin <id>\n/removeadmin <id>\n/adminlist\n"
    )
    await send_text(update.effective_message, text, protect=True)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send_text(update.effective_message, "Admin only.", protect=True)
        return

    total = (_db_exec("SELECT COUNT(*) FROM users", fetchone=True) or [0])[0]
    banned = (_db_exec("SELECT COUNT(*) FROM users WHERE banned = 1", fetchone=True) or [0])[0]
    premium = (_db_exec("SELECT COUNT(*) FROM users WHERE premium = 1", fetchone=True) or [0])[0]
    downloads = (_db_exec("SELECT COUNT(*) FROM downloads", fetchone=True) or [0])[0]
    limit = get_daily_limit()

    # COPY FIX: plain
    await send_plain_text(
        update.effective_message,
        "Bot Stats\n"
        f"Users: {total}\n"
        f"Premium: {premium}\n"
        f"Banned: {banned}\n"
        f"Downloads: {downloads}\n"
        f"Daily limit: {limit if limit > 0 else 'OFF'}",
    )


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = _db_exec(
        "SELECT user_id, username, premium, banned FROM users ORDER BY user_id DESC LIMIT 50",
        fetchall=True,
    ) or []
    lines = ["Last 50 users:"]
    for uid, uname, prem, ban in rows:
        tag = "PREMIUM" if prem else "-"
        tag2 = "BANNED" if ban else ""
        lines.append(f"{uid}  @{uname or 'None'}  {tag} {tag2}".strip())
    await send_plain_text(update.effective_message, "\n".join(lines))


async def make_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /premium <id>", protect=True)
        return
    try:
        uid = int(context.args[0])
    except Exception:
        await send_text(update.effective_message, "Invalid user id.", protect=True)
        return
    set_premium(uid, True)
    await send_text(update.effective_message, f"Premium added: {uid}", protect=True)


async def remove_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /unpremium <id>", protect=True)
        return
    try:
        uid = int(context.args[0])
    except Exception:
        await send_text(update.effective_message, "Invalid user id.", protect=True)
        return
    set_premium(uid, False)
    await send_text(update.effective_message, f"Premium removed: {uid}", protect=True)


async def cmd_premiumusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = _db_exec(
        "SELECT user_id, username FROM users WHERE premium = 1 AND banned = 0 ORDER BY user_id DESC LIMIT 200",
        fetchall=True,
    ) or []
    if not rows:
        await send_text(update.effective_message, "No premium users.", protect=True)
        return
    text = "Premium users:\n" + "\n".join([f"{uid}  @{uname or 'None'}" for uid, uname in rows])
    await send_plain_text(update.effective_message, text)


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /ban <id>", protect=True)
        return
    try:
        uid = int(context.args[0])
    except Exception:
        await send_text(update.effective_message, "Invalid user id.", protect=True)
        return
    ban_user(uid)
    await send_text(update.effective_message, f"Banned: {uid}", protect=True)


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /unban <id>", protect=True)
        return
    try:
        uid = int(context.args[0])
    except Exception:
        await send_text(update.effective_message, "Invalid user id.", protect=True)
        return
    unban_user(uid)
    await send_text(update.effective_message, f"Unbanned: {uid}", protect=True)


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /del <media_id>", protect=True)
        return
    media_id = context.args[0]
    _db_exec("DELETE FROM media_files WHERE media_id = %s", (media_id,), commit=True)
    await send_text(update.effective_message, "Deleted (if it existed).", protect=True)


async def cmd_genlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /genlink <media_id>", protect=True)
        return
    media_id = context.args[0]
    if not get_data(media_id):
        await send_text(update.effective_message, "Media not found.", protect=True)
        return
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={media_id}"
    # COPY FIX
    await send_plain_text(update.effective_message, f"Media ID: {media_id}\nLink:\n{link}")


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /usage <media_id>", protect=True)
        return
    media_id = context.args[0]
    row = _db_exec("SELECT COUNT(*) FROM downloads WHERE media_id = %s", (media_id,), fetchone=True) or [0]
    await send_plain_text(update.effective_message, f"Usage ({media_id}): {row[0]}")


# ---------------------------- OWNER ----------------------------

async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /addadmin <user_id>", protect=True)
        return
    try:
        uid = int(context.args[0])
    except Exception:
        await send_text(update.effective_message, "Invalid id.", protect=True)
        return
    add_admin_db(uid, update.effective_user.id)
    await send_text(update.effective_message, f"Admin added: {uid}", protect=True)


async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await send_text(update.effective_message, "Usage: /removeadmin <user_id>", protect=True)
        return
    try:
        uid = int(context.args[0])
    except Exception:
        await send_text(update.effective_message, "Invalid id.", protect=True)
        return
    ok = remove_admin_db(uid)
    if ok:
        await send_text(update.effective_message, f"Admin removed: {uid}", protect=True)
    else:
        await send_text(update.effective_message, "Not found / owner cannot be removed.", protect=True)


async def cmd_adminlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    ids = list_admins_all()
    lines = ["Admin list:"]
    for i, uid in enumerate(ids, start=1):
        tag = " (OWNER)" if is_owner(uid) else ""
        lines.append(f"{i}. {uid}{tag}")
    await send_plain_text(update.effective_message, "\n".join(lines))


# ---------------------------- UPLOAD ----------------------------

async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_obj = update.effective_message
    if not msg_obj:
        return

    user = update.effective_user
    if is_banned(user.id):
        await send_text(msg_obj, "You are banned.", protect=True)
        return

    ok, missing = await check_force_join_for_user(context.bot, user.id)
    if not ok:
        await send_join_required_screen(update, context, missing, "")
        return

    if not (is_admin(user.id) or is_premium(user.id)):
        await send_text(msg_obj, "Only Admin/Premium users can upload.", protect=True)
        return

    context.user_data["upload_files"] = []
    context.user_data["media_id"] = gen_id()
    await msg_obj.reply_text(
        apply_font("Send files now. When finished, press ✅."),
        reply_markup=ReplyKeyboardMarkup([["✅"]], resize_keyboard=True),
        **protect_kwargs(),
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    if await _handle_getid_mode(update, context):
        return

    user = update.effective_user
    ensure_user_record(user.id, user.username)

    # Broadcast capture mode
    if context.user_data.get("awaiting_broadcast"):
        await _capture_broadcast_content(update, context)
        return

    if is_banned(user.id):
        await send_text(msg, "You are banned.", protect=True)
        return

    ok, missing = await check_force_join_for_user(context.bot, user.id)
    if not ok:
        await send_join_required_screen(update, context, missing, "")
        return

    # Finalize upload
    if msg.text and msg.text.strip() == "✅":
        files = context.user_data.get("upload_files", [])
        if not files:
            await msg.reply_text("No media received.", reply_markup=ReplyKeyboardRemove())
            return

        media_id = context.user_data.get("media_id")
        if not media_id:
            await msg.reply_text("Session expired.", reply_markup=ReplyKeyboardRemove())
            context.user_data.clear()
            return

        save_data(media_id, files)
        me = await context.bot.get_me()
        share_link = f"https://t.me/{me.username}?start={media_id}"

        # COPY FIX: no font + no protect_content
        await msg.reply_text(
            f"Uploaded successfully ✅\n\nMedia ID: {media_id}\nLink:\n{share_link}",
            reply_markup=ReplyKeyboardRemove(),
            disable_web_page_preview=True,
        )

        if PRIVATE_CHANNEL_ID is not None:
            try:
                uname = f"@{user.username}" if user.username else "NoUsername"
                p_text = f"New Upload\nUser: {uname} ({user.id})\nMedia ID: {media_id}\nLink: {share_link}"
                await context.bot.send_message(PRIVATE_CHANNEL_ID, p_text)
            except Exception:
                pass

        context.user_data.clear()
        return

    # Save incoming media during upload session
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

    if f and context.user_data.get("media_id"):
        context.user_data.setdefault("upload_files", []).append(f)
        await send_text(msg, "Saved. Send more or press ✅.", protect=True)


# ---------------------------- CALLBACKS ----------------------------

async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""

    if data == "ui_about":
        await send_about_screen(update, context)
        return

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

    # Broadcast confirm/cancel
    if data.startswith("bc_confirm:") or data.startswith("bc_cancel:"):
        admin_id = int(data.split(":", 1)[1])
        if update.effective_user.id != admin_id:
            await send_text(query.message, "Only the initiating admin can confirm/cancel.", protect=True)
            return

        if data.startswith("bc_cancel:"):
            context.user_data.pop("broadcast_pending", None)
            context.user_data.pop("broadcast_preview_message", None)
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await send_text(query.message, "Broadcast cancelled.", protect=True)
            return

        payload = context.user_data.get("broadcast_pending")
        info = context.user_data.get("broadcast_preview_message")
        if not payload or not info:
            await send_text(query.message, "No broadcast payload found.", protect=True)
            return

        try:
            await context.bot.edit_message_reply_markup(info["chat_id"], info["message_id"], reply_markup=None)
        except Exception:
            pass

        progress_msg = None
        try:
            progress_msg = await context.bot.send_message(info["chat_id"], apply_font("Broadcasting..."), **protect_kwargs())
        except Exception:
            pass

        asyncio.create_task(_run_broadcast_task(context.bot, payload, progress_msg))
        context.user_data.pop("broadcast_pending", None)
        context.user_data.pop("broadcast_preview_message", None)
        await send_text(query.message, "Broadcast started.", protect=True)
        return


# ---------------------------- ERROR HANDLER ----------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                apply_font("An error occurred. Please try again."),
                **protect_kwargs(),
            )
    except Exception:
        pass


# ---------------------------- BOT COMMANDS MENU ----------------------------

async def set_bot_commands(app: Application):
    commands = [
        BotCommand("start", "Start"),
        BotCommand("about", "About"),
        BotCommand("credits", "About"),
        BotCommand("profile", "Profile"),
        BotCommand("getid", "Get file_id of media"),
        BotCommand("upload", "Upload (admin/premium)"),

        BotCommand("setfont", "Set global font style (admin)"),
        BotCommand("getfont", "Get styled text (admin)"),
        BotCommand("givefont", "Alias of /getfont"),

        BotCommand("cmd", "Admin menu"),
        BotCommand("stats", "Stats (admin)"),
        BotCommand("users", "Users (admin)"),

        BotCommand("broadcast", "Broadcast (admin)"),
        BotCommand("pbroadcast", "Premium broadcast (admin)"),

        BotCommand("ban", "Ban user (admin)"),
        BotCommand("unban", "Unban user (admin)"),
        BotCommand("premium", "Add premium (admin)"),
        BotCommand("unpremium", "Remove premium (admin)"),
        BotCommand("premiumusers", "List premium users (admin)"),

        BotCommand("setphoto", "Set start photo (admin)"),
        BotCommand("set", "Add force-join (admin)"),
        BotCommand("remove", "Remove force-join (admin)"),
        BotCommand("listchannels", "List force-join (admin)"),

        BotCommand("dset", "Set delivery join button (admin)"),

        BotCommand("del", "Delete media (admin)"),
        BotCommand("genlink", "Generate link (admin)"),
        BotCommand("usage", "Media usage (admin)"),

        BotCommand("setlimit", "Set daily limit (admin)"),
        BotCommand("removelimit", "Remove daily limit (admin)"),

        BotCommand("addadmin", "Add admin (owner)"),
        BotCommand("removeadmin", "Remove admin (owner)"),
        BotCommand("adminlist", "Admin list (owner)"),
    ]
    try:
        await app.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning("Could not set bot commands: %s", e)


# ---------------------------- MAIN ----------------------------

def build_app() -> Application:
    ensure_schema()
    ensure_default_force_channel()
    load_font_from_db()

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
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("getid", cmd_getid))

    # Font
    app.add_handler(CommandHandler("getfont", cmd_getfont))
    app.add_handler(CommandHandler("givefont", cmd_givefont))
    app.add_handler(CommandHandler("setfont", cmd_setfont))

    # Delivery join button
    app.add_handler(CommandHandler("dset", cmd_dset))

    # Admin
    app.add_handler(CommandHandler("upload", upload))
    app.add_handler(CommandHandler("cmd", cmd_cmd))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("users", cmd_users))

    # Broadcast
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("pbroadcast", pbroadcast_command))

    # Others (admin/owner)
    app.add_handler(CommandHandler("setphoto", cmd_setphoto))
    app.add_handler(CommandHandler("set", cmd_set_force))
    app.add_handler(CommandHandler("remove", cmd_remove_force))
    app.add_handler(CommandHandler("listchannels", cmd_listchannels))
    app.add_handler(CommandHandler("setlimit", cmd_setlimit))
    app.add_handler(CommandHandler("removelimit", cmd_removelimit))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("premium", make_premium))
    app.add_handler(CommandHandler("unpremium", remove_premium))
    app.add_handler(CommandHandler("premiumusers", cmd_premiumusers))
    app.add_handler(CommandHandler("del", cmd_delete))
    app.add_handler(CommandHandler("genlink", cmd_genlink))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("adminlist", cmd_adminlist))

    # Callbacks + media
    app.add_handler(CallbackQueryHandler(callback_query_router))
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), handle_media))

    app.add_error_handler(error_handler)
    return app


def main() -> None:
    app = build_app()

    async def _post_init(application: Application):
        await set_bot_commands(application)
        logger.info("Bot started.")

    app.post_init = _post_init
    app.run_polling(drop_pending_updates=True, close_loop=False)


if __name__ == "__main__":
    init_db_pool()
    main()

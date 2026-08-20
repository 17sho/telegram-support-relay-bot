import asyncio
import html
import json
import logging
import os
import random
import secrets
from io import BytesIO

from dotenv import load_dotenv

load_dotenv()
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BASE_DIR = Path(os.environ.get("TG_RELAY_BASE_DIR", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "relay.db"
BOT_TOKEN = os.environ["BOT_TOKEN"]
_ADMIN_VALUES = [int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x]
if len(_ADMIN_VALUES) != 1:
    raise RuntimeError("ADMIN_IDS must contain exactly one administrator ID")
ADMIN_ID = _ADMIN_VALUES[0]
MAX_TEXT = 3500
DEFAULT_VERIFY_INTERVAL_MINUTES = int(os.environ.get("DEFAULT_VERIFY_INTERVAL_MINUTES", "360"))
MESSAGES_PER_MINUTE = max(1, int(os.environ.get("MESSAGES_PER_MINUTE", "40")))
MIN_VERIFY_INTERVAL_MINUTES = 0
MAX_VERIFY_INTERVAL_MINUTES = 43200
VERIFY_INTERVAL_PRESETS = [(0, "立即验证"), (60, "1小时"), (360, "6小时"), (1440, "24小时")]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tg-relay-bot")
DATA_DIR.mkdir(parents=True, exist_ok=True)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma busy_timeout=10000")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        create table if not exists users(
          user_id integer primary key,
          username text,
          first_name text,
          last_name text,
          created_at text,
          updated_at text
        );
        create table if not exists messages(
          id integer primary key autoincrement,
          user_id integer not null,
          direction text not null,
          kind text not null,
          text text,
          file_id text,
          admin_message_id integer,
          user_message_id integer,
          delivery_status text not null default 'pending',
          delivered_count integer not null default 0,
          retracted_at text,
          created_at text not null
        );
        create index if not exists idx_messages_user_id on messages(user_id,id desc);
        create index if not exists idx_messages_user_direction_id on messages(user_id,direction,id desc);
        create index if not exists idx_messages_created_at on messages(created_at);
        create table if not exists admin_message_map(
          admin_chat_id integer not null,
          admin_message_id integer not null,
          user_id integer not null,
          created_at text not null,
          primary key(admin_chat_id, admin_message_id)
        );
        create index if not exists idx_admin_message_map_user on admin_message_map(admin_chat_id,user_id,created_at desc);
        create table if not exists admin_state(
          admin_id integer primary key,
          selected_user_id integer,
          updated_at text
        );
        create table if not exists admin_onboarding(
          admin_id integer primary key,
          welcomed_at text not null
        );
        create table if not exists user_status(
          user_id integer primary key,
          verified integer not null default 0,
          blocked integer not null default 0,
          verify_exempt integer not null default 0,
          verify_interval_minutes integer,
          challenge_answer text,
          challenge_at text,
          verified_at text,
          blocked_at text,
          updated_at text
        );
        create table if not exists rate_limits(
          user_id integer primary key,
          window_started_at text not null,
          message_count integer not null default 0,
          notified integer not null default 0
        );
        create table if not exists verification_attempts(
          user_id integer primary key,
          failed_attempts integer not null default 0,
          locked_until text,
          updated_at text not null
        );
        create table if not exists settings(
          key text primary key,
          value text not null,
          updated_at text
        );
        """)
        existing_user_cols = {row["name"] for row in conn.execute("pragma table_info(users)").fetchall()}
        if "unread_count" not in existing_user_cols:
            conn.execute("alter table users add column unread_count integer not null default 0")
        existing_cols = {row["name"] for row in conn.execute("pragma table_info(user_status)").fetchall()}
        if "verify_exempt" not in existing_cols:
            conn.execute("alter table user_status add column verify_exempt integer not null default 0")
        if "verify_interval_minutes" not in existing_cols:
            conn.execute("alter table user_status add column verify_interval_minutes integer")
        message_cols = {row["name"] for row in conn.execute("pragma table_info(messages)").fetchall()}
        if "delivery_status" not in message_cols:
            conn.execute("alter table messages add column delivery_status text not null default 'delivered'")
        if "delivered_count" not in message_cols:
            conn.execute("alter table messages add column delivered_count integer not null default 0")
        if "retracted_at" not in message_cols:
            conn.execute("alter table messages add column retracted_at text")
        rate_limit_cols = {row["name"] for row in conn.execute("pragma table_info(rate_limits)").fetchall()}
        if "notified" not in rate_limit_cols:
            conn.execute("alter table rate_limits add column notified integer not null default 0")
        old_global_interval = conn.execute("select value from settings where key='verify_interval_hours'").fetchone()
        if old_global_interval:
            try:
                old_minutes = int(old_global_interval["value"]) * 60
            except ValueError:
                old_minutes = DEFAULT_VERIFY_INTERVAL_MINUTES
            conn.execute(
                "update user_status set verify_interval_minutes=? where verify_interval_minutes is null",
                (old_minutes,),
            )


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def consume_rate_limit(user_id: int) -> tuple[bool, bool]:
    """Return (allowed, should_notify) for a fixed one-minute window."""
    current = datetime.now()
    with db() as conn:
        row = conn.execute("select window_started_at,message_count,notified from rate_limits where user_id=?", (user_id,)).fetchone()
        started = parse_time(row["window_started_at"]) if row else None
        if not row or not started or current - started >= timedelta(minutes=1):
            conn.execute(
                "insert into rate_limits(user_id,window_started_at,message_count,notified) values(?,?,1,0) "
                "on conflict(user_id) do update set window_started_at=excluded.window_started_at,message_count=1,notified=0",
                (user_id, now()),
            )
            return True, False
        count = int(row["message_count"]) + 1
        allowed = count <= MESSAGES_PER_MINUTE
        should_notify = not allowed and not bool(row["notified"])
        conn.execute(
            "update rate_limits set message_count=?,notified=? where user_id=?",
            (count, 1 if should_notify or row["notified"] else 0, user_id),
        )
        return allowed, should_notify


def reset_rate_limit(user_id: int) -> None:
    with db() as conn:
        conn.execute("delete from rate_limits where user_id=?", (user_id,))


def cleanup_transient_data() -> None:
    """Delete expired transient state only; never delete users or chat history."""
    rate_cutoff = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    verify_cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        conn.execute("delete from rate_limits where window_started_at < ?", (rate_cutoff,))
        conn.execute(
            "delete from verification_attempts where updated_at < ? and (locked_until is null or locked_until < ?)",
            (verify_cutoff, now()),
        )



def verification_lock_until(user_id: int) -> datetime | None:
    with db() as conn:
        row = conn.execute("select locked_until from verification_attempts where user_id=?", (user_id,)).fetchone()
        lock = parse_time(row["locked_until"]) if row else None
        if lock and lock <= datetime.now():
            conn.execute("delete from verification_attempts where user_id=?", (user_id,))
            return None
        return lock


def record_verification_failure(user_id: int) -> datetime | None:
    with db() as conn:
        row = conn.execute("select failed_attempts from verification_attempts where user_id=?", (user_id,)).fetchone()
        failures = (int(row["failed_attempts"]) if row else 0) + 1
        lock = datetime.now() + timedelta(hours=6) if failures >= 2 else None
        conn.execute("insert into verification_attempts(user_id,failed_attempts,locked_until,updated_at) values(?,?,?,?) on conflict(user_id) do update set failed_attempts=excluded.failed_attempts,locked_until=excluded.locked_until,updated_at=excluded.updated_at", (user_id, failures, lock.strftime("%Y-%m-%d %H:%M:%S") if lock else None, now()))
        if lock:
            conn.execute("update user_status set challenge_answer='',challenge_at=null,updated_at=? where user_id=?", (now(), user_id))
        return lock


def is_verification_locked(user_id: int) -> bool:
    return verification_lock_until(user_id) is not None


def reset_verification_failures(user_id: int) -> None:
    with db() as conn:
        conn.execute("delete from verification_attempts where user_id=?", (user_id,))


def lock_message(lock: datetime) -> str:
    return f"验证失败次数过多，已暂停验证6小时。可于 {lock.strftime('%Y-%m-%d %H:%M')} 后重试。"


def challenge_expired(row: sqlite3.Row | None) -> bool:
    started = parse_time(row["challenge_at"]) if row and row["challenge_at"] else None
    return not started or datetime.now() - started > timedelta(minutes=2)


def challenge_pending(row: sqlite3.Row | None) -> bool:
    return bool(row and row["challenge_answer"] and not challenge_expired(row))


def force_reverify(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            "insert into user_status(user_id,verified,blocked,challenge_answer,updated_at) values(?,?,?,?,?) "
            "on conflict(user_id) do update set verified=0, challenge_answer='', updated_at=excluded.updated_at",
            (user_id, 0, 0, "", now()),
        )


def is_admin(user_id: int | None) -> bool:
    return user_id == ADMIN_ID


def parse_user_id(args: list[str]) -> int | None:
    if len(args) != 1:
        return None
    try:
        value = int(args[0])
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled update error", exc_info=context.error)


def upsert_user(user: Any) -> None:
    with db() as conn:
        conn.execute(
            """insert into users(user_id,username,first_name,last_name,created_at,updated_at)
               values(?,?,?,?,?,?)
               on conflict(user_id) do update set username=excluded.username, first_name=excluded.first_name,
               last_name=excluded.last_name, updated_at=excluded.updated_at""",
            (user.id, user.username or "", user.first_name or "", user.last_name or "", now(), now()),
        )


def display_name(row: sqlite3.Row | Any) -> str:
    username = row["username"] if isinstance(row, sqlite3.Row) else (row.username or "")
    first = row["first_name"] if isinstance(row, sqlite3.Row) else (row.first_name or "")
    last = row["last_name"] if isinstance(row, sqlite3.Row) else (row.last_name or "")
    uid = row["user_id"] if isinstance(row, sqlite3.Row) else row.id
    name = " ".join(x for x in [first, last] if x).strip() or (f"@{username}" if username else "用户")
    return f"{name} / @{username or '-'} / {uid}"


def message_kind(message: Message) -> str:
    # Telegram media may include a caption. Detect the attachment before text so
    # a captioned photo/video/document is still relayed and stored as media.
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.voice:
        return "voice"
    if message.audio:
        return "audio"
    if message.document:
        return "document"
    if message.sticker:
        return "sticker"
    if message.text:
        return "text"
    return "message"


def message_text(message: Message) -> str:
    return message.text or message.caption or ""


def file_id_for(message: Message) -> str:
    if message.photo:
        return message.photo[-1].file_id
    if message.video:
        return message.video.file_id
    if message.voice:
        return message.voice.file_id
    if message.audio:
        return message.audio.file_id
    if message.document:
        return message.document.file_id
    if message.sticker:
        return message.sticker.file_id
    return ""


def selected_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ 当前会话 · 取消选中", callback_data=f"unselect:{user_id}")]])


def mark_unread(user_id: int) -> None:
    with db() as conn:
        conn.execute("update users set unread_count=coalesce(unread_count,0)+1 where user_id=?", (user_id,))


def mark_read(user_id: int) -> None:
    with db() as conn:
        conn.execute("update users set unread_count=0 where user_id=?", (user_id,))


def unread_count(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("select unread_count from users where user_id=?", (user_id,)).fetchone()
    return int(row["unread_count"] or 0) if row else 0


def user_keyboard(user_id: int, blocked: bool = False) -> InlineKeyboardMarkup:
    status = get_user_status(user_id)
    verify_exempt = bool(status and status["verify_exempt"])
    interval_minutes = get_user_verify_interval_minutes(user_id)
    block_label = "解除屏蔽" if blocked else "屏蔽联系人"
    block_action = "unblock" if blocked else "block"
    exempt_label = "取消验证豁免" if verify_exempt else "永久豁免验证"
    exempt_action = "unexempt" if verify_exempt else "exempt"
    rows = [
        [InlineKeyboardButton("打开个人资料", url=f"tg://user?id={user_id}"), InlineKeyboardButton("选中会话", callback_data=f"select:{user_id}")],
        [InlineKeyboardButton("查看会话记录", callback_data=f"history_open:{user_id}:0"), InlineKeyboardButton(block_label, callback_data=f"{block_action}:{user_id}")],
        [InlineKeyboardButton(f"验证时间：{format_interval(interval_minutes)}", callback_data=f"verify_menu:{user_id}"), InlineKeyboardButton(exempt_label, callback_data=f"{exempt_action}:{user_id}")],
    ]
    if is_verification_locked(user_id):
        rows.append([InlineKeyboardButton("🔓 解除验证锁定", callback_data=f"unlockverify:{user_id}")])
    return InlineKeyboardMarkup(rows)


def verify_time_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"setverify:{user_id}:{minutes}") for minutes, label in VERIFY_INTERVAL_PRESETS[:2]],
        [InlineKeyboardButton(label, callback_data=f"setverify:{user_id}:{minutes}") for minutes, label in VERIFY_INTERVAL_PRESETS[2:]],
        [InlineKeyboardButton("返回会话卡片", callback_data=f"back_user:{user_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def challenge_keyboard(user_id: int, correct_label: str, choices: list[str]) -> tuple[InlineKeyboardMarkup, str]:
    """Render shuffled choices while keeping the answer out of callback_data."""
    unique = list(dict.fromkeys(choices))
    if correct_label not in unique:
        unique.append(correct_label)
    random.shuffle(unique)
    correct_token = ""
    buttons = []
    for label in unique:
        token = secrets.token_urlsafe(6)
        if label == correct_label:
            correct_token = token
        buttons.append(InlineKeyboardButton(label, callback_data=f"verify:{user_id}:{token}"))
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    return InlineKeyboardMarkup(rows), correct_token


def numeric_choices(answer_value: int, count: int = 8) -> list[str]:
    values = {answer_value}
    deltas = [-12, -8, -5, -3, -2, -1, 1, 2, 3, 5, 8, 12]
    random.shuffle(deltas)
    for delta in deltas:
        if answer_value + delta >= 0:
            values.add(answer_value + delta)
        if len(values) >= count:
            break
    while len(values) < count:
        values.add(random.randint(max(0, answer_value - 20), answer_value + 20))
    return [str(value) for value in values]


def media_label(kind: str) -> str:
    return {"photo": "🖼 图片", "sticker": "🎭 表情包", "video": "🎬 视频", "voice": "🎤 语音", "audio": "🎵 音频", "document": "📎 文件"}.get(kind, "📦 媒体")


def history_keyboard(user_id: int, offset: int, has_more: bool, media_rows=()) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"查看 {media_label(row['kind'])} · {format_beijing_time(row['created_at'])}", callback_data=f"media:{row['id']}")] for row in media_rows]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("上一页", callback_data=f"history:{user_id}:{max(0, offset-10)}"))
    if has_more:
        nav.append(InlineKeyboardButton("下一页", callback_data=f"history:{user_id}:{offset+10}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("返回会话卡片", callback_data=f"history_close:{user_id}")])
    rows.append([InlineKeyboardButton("选中会话", callback_data=f"select:{user_id}"), InlineKeyboardButton("打开个人资料", url=f"tg://user?id={user_id}")])
    return InlineKeyboardMarkup(rows)


def save_admin_message_map(admin_chat_id: int, admin_message_id: int, user_id: int) -> None:
    with db() as conn:
        conn.execute("insert or replace into admin_message_map(admin_chat_id,admin_message_id,user_id,created_at) values(?,?,?,?)", (admin_chat_id, admin_message_id, user_id, now()))


def resolve_admin_reply(admin_chat_id: int, admin_message_id: int) -> int | None:
    with db() as conn:
        row = conn.execute("select user_id from admin_message_map where admin_chat_id=? and admin_message_id=?", (admin_chat_id, admin_message_id)).fetchone()
    return int(row["user_id"]) if row else None


def save_message(user_id: int, direction: str, kind: str, text: str = "", file_id: str = "", admin_message_id: int | None = None, user_message_id: int | None = None) -> int:
    with db() as conn:
        cursor = conn.execute(
            "insert into messages(user_id,direction,kind,text,file_id,admin_message_id,user_message_id,created_at) values(?,?,?,?,?,?,?,?)",
            (user_id, direction, kind, text, file_id, admin_message_id, user_message_id, now()),
        )
        return int(cursor.lastrowid)


def get_user_status(user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("select * from user_status where user_id=?", (user_id,)).fetchone()


def format_interval(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}分钟"
    if minutes % 60 == 0:
        return f"{minutes // 60}小时"
    return f"{minutes}分钟"


def get_user_verify_interval_minutes(user_id: int) -> int:
    row = get_user_status(user_id)
    value = row["verify_interval_minutes"] if row and row["verify_interval_minutes"] is not None else DEFAULT_VERIFY_INTERVAL_MINUTES
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = DEFAULT_VERIFY_INTERVAL_MINUTES
    return max(MIN_VERIFY_INTERVAL_MINUTES, min(MAX_VERIFY_INTERVAL_MINUTES, minutes))


def set_user_verify_interval_minutes(user_id: int, minutes: int) -> None:
    minutes = max(MIN_VERIFY_INTERVAL_MINUTES, min(MAX_VERIFY_INTERVAL_MINUTES, minutes))
    with db() as conn:
        conn.execute(
            """insert into user_status(user_id,verified,blocked,verify_interval_minutes,updated_at)
               values(?,?,?,?,?)
               on conflict(user_id) do update set verify_interval_minutes=excluded.verify_interval_minutes, updated_at=excluded.updated_at""",
            (user_id, 0, 0, minutes, now()),
        )


def is_verified(user_id: int) -> bool:
    row = get_user_status(user_id)
    if row and row["verify_exempt"]:
        return True
    if not row or not row["verified"]:
        return False
    # 验证成功本身也是一次有效活动。取验证时间和最后一条用户消息中较新的时间，
    # 避免历史消息使用旧时区/旧时间戳时，用户刚验证成功就立刻再次被要求验证。
    verified_at = parse_time(row["verified_at"])
    last_user_message = last_inbound_message_at(user_id)
    activity_at = max((value for value in (verified_at, last_user_message) if value), default=None)
    if activity_at is None:
        return True
    return datetime.now() - activity_at <= timedelta(minutes=get_user_verify_interval_minutes(user_id))


def last_inbound_message_at(user_id: int) -> datetime | None:
    with db() as conn:
        row = conn.execute(
            "select created_at from messages where user_id=? and direction='user' order by id desc limit 1",
            (user_id,),
        ).fetchone()
    return parse_time(row["created_at"]) if row else None


def is_blocked(user_id: int) -> bool:
    row = get_user_status(user_id)
    return bool(row and row["blocked"])


def set_blocked(user_id: int, blocked: bool) -> None:
    with db() as conn:
        conn.execute(
            """insert into user_status(user_id,verified,blocked,blocked_at,updated_at)
               values(?,?,?,?,?)
               on conflict(user_id) do update set blocked=excluded.blocked, blocked_at=excluded.blocked_at, updated_at=excluded.updated_at""",
            (user_id, 0, 1 if blocked else 0, now() if blocked else None, now()),
        )


def set_verified(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            """insert into user_status(user_id,verified,blocked,challenge_answer,challenge_at,verified_at,updated_at)
               values(?,?,?,?,?,?,?)
               on conflict(user_id) do update set verified=1, challenge_answer='', challenge_at=null, verified_at=excluded.verified_at, updated_at=excluded.updated_at""",
            (user_id, 1, 0, "", None, now(), now()),
        )


def set_verify_exempt(user_id: int, exempt: bool) -> None:
    with db() as conn:
        conn.execute(
            """insert into user_status(user_id,verified,blocked,verify_exempt,challenge_answer,challenge_at,verified_at,updated_at)
               values(?,?,?,?,?,?,?,?)
               on conflict(user_id) do update set verify_exempt=excluded.verify_exempt, verified=case when excluded.verify_exempt=1 then 1 else verified end, challenge_answer='', updated_at=excluded.updated_at""",
            (user_id, 1 if exempt else 0, 0, 1 if exempt else 0, "", None, now() if exempt else None, now()),
        )


def captcha_choices(code: str, count: int = 6) -> list[str]:
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    values = {code}
    while len(values) < count:
        chars = list(code)
        operation = random.choice(("replace", "swap"))
        if operation == "swap":
            pos = random.randrange(len(chars) - 1)
            chars[pos], chars[pos + 1] = chars[pos + 1], chars[pos]
        else:
            pos = random.randrange(len(chars))
            chars[pos] = random.choice(alphabet.replace(chars[pos], ""))
        values.add("".join(chars))
    return list(values)


def captcha_image(code: str) -> BytesIO:
    width, height = 420, 150
    image = Image.new("RGB", (width, height), (random.randint(225, 250), random.randint(225, 250), random.randint(225, 250)))
    draw = ImageDraw.Draw(image)
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    font_path = next((path for path in font_paths if Path(path).exists()), None)
    for _ in range(9):
        draw.line(
            [(random.randrange(width), random.randrange(height)), (random.randrange(width), random.randrange(height))],
            fill=tuple(random.randint(70, 190) for _ in range(3)), width=random.randint(1, 4),
        )
    for index, char in enumerate(code):
        font = ImageFont.truetype(font_path, random.randint(63, 78)) if font_path else ImageFont.load_default()
        layer = Image.new("RGBA", (90, 110), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.text((12, 4), char, font=font, fill=tuple(random.randint(15, 120) for _ in range(3)) + (255,))
        layer = layer.rotate(random.randint(-25, 25), resample=Image.Resampling.BICUBIC, expand=False)
        image.paste(layer, (18 + index * 78, random.randint(18, 32)), layer)
    draw = ImageDraw.Draw(image)
    for _ in range(650):
        x, y = random.randrange(width), random.randrange(height)
        draw.point((x, y), fill=tuple(random.randint(30, 220) for _ in range(3)))
    image = image.filter(ImageFilter.GaussianBlur(0.35))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = "verification.png"
    return output


def store_challenge(user_id: int, answer_token: str) -> None:
    timestamp = now()
    with db() as conn:
        conn.execute(
            """insert into user_status(user_id,verified,blocked,challenge_answer,challenge_at,updated_at)
               values(?,?,?,?,?,?)
               on conflict(user_id) do update set verified=0, challenge_answer=excluded.challenge_answer, challenge_at=excluded.challenge_at, updated_at=excluded.updated_at""",
            (user_id, 0, 0, answer_token, timestamp, timestamp),
        )


def create_image_challenge(user_id: int) -> tuple[BytesIO, InlineKeyboardMarkup]:
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(5))
    keyboard, answer_token = challenge_keyboard(user_id, code, captcha_choices(code))
    rows = list(keyboard.inline_keyboard)
    rows.append([InlineKeyboardButton("🔄 看不清，换一张", callback_data=f"refreshverify:{user_id}")])
    store_challenge(user_id, answer_token)
    return captcha_image(code), InlineKeyboardMarkup(rows)


async def send_challenge(message: Message, user_id: int, prefix: str = "请先完成人机验证") -> None:
    challenge_type = random.choice(["image", "emoji_count", "shape_count", "odd_emoji", "emoji_sequence"])
    if challenge_type == "image":
        image, keyboard = create_image_challenge(user_id)
        await message.reply_photo(image, caption=f"{prefix}：请选择与图片中一致的字符组合。", reply_markup=keyboard)
    else:
        question, keyboard = create_challenge(user_id, challenge_type)
        await message.reply_text(f"{prefix}：{question}", reply_markup=keyboard)


def create_challenge(user_id: int, challenge_type: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    challenge_type = challenge_type or random.choice(["add3", "mixed", "sequence", "emoji_count", "odd_emoji", "shape_count", "emoji_sequence"])
    if challenge_type == "add3":
        nums = [random.randint(11, 49) for _ in range(3)]
        value = sum(nums)
        correct, choices = str(value), numeric_choices(value)
        question = f"请计算：{nums[0]} + {nums[1]} + {nums[2]} = ?"
    elif challenge_type == "sub":
        right, value = random.randint(12, 49), random.randint(15, 69)
        correct, choices = str(value), numeric_choices(value)
        question = f"请计算：{right + value} − {right} = ?"
    elif challenge_type == "mul":
        left, right = random.randint(2, 9), random.randint(2, 9)
        value = left * right
        correct, choices = str(value), numeric_choices(value)
        question = f"请计算：{left} × {right} = ?"
    elif challenge_type == "mixed":
        left, middle, right = random.randint(2, 9), random.randint(2, 9), random.randint(3, 15)
        value = left * middle + right
        correct, choices = str(value), numeric_choices(value)
        question = f"先乘后加：{left} × {middle} + {right} = ?"
    elif challenge_type == "sequence":
        start, step = random.randint(3, 25), random.randint(3, 12)
        sequence = [start + step * i for i in range(5)]
        correct, choices = str(sequence[-1]), numeric_choices(sequence[-1])
        question = f"找规律，下一项是？ {sequence[0]}，{sequence[1]}，{sequence[2]}，{sequence[3]}，?"
    elif challenge_type == "second_max":
        numbers = random.sample(range(10, 100), 8)
        correct, choices = str(sorted(numbers, reverse=True)[1]), [str(n) for n in numbers]
        question = "请点击第二大的数字"
    elif challenge_type in ("emoji_count", "shape_count"):
        symbol = random.choice(["🍎", "⭐", "🐱", "🌙", "🔺", "🟦", "❤️", "🌸"])
        count = random.randint(5, 11)
        noise = random.choice(["🍋", "☀️", "🐶", "🟢", "◆"])
        display = [symbol] * count + [noise] * random.randint(2, 4)
        random.shuffle(display)
        correct, choices = str(count), numeric_choices(count)
        question = f"数一数图中有多少个 {symbol}：\n\n{' '.join(display)}"
    elif challenge_type == "odd_emoji":
        common, odd = random.choice([("🐶", "🐺"), ("🍎", "🍅"), ("🌕", "🌑"), ("🟦", "🟪"), ("😀", "😎")])
        display = [common] * 8
        position = random.randrange(8)
        display[position] = odd
        correct, choices = str(position + 1), [str(i) for i in range(1, 9)]
        question = "从左到右编号 1–8，请点击不同表情的位置：\n\n" + " ".join(display)
    else:
        first, second = random.choice([("🍎", "🍋"), ("🐱", "🐶"), ("🔺", "🟦"), ("🌞", "🌙")])
        correct = first
        choices = [first, second, "⭐", "🌸", "❤️", "🟢", "◆", "☂️"]
        question = f"观察规律并点击下一项：\n\n{first} {second} {first} {second} {first} ？"
    keyboard, answer_token = challenge_keyboard(user_id, correct, choices)
    store_challenge(user_id, answer_token)
    return question, keyboard


async def delete_message_quietly(message: Message | None) -> None:
    if not message:
        return
    try:
        await message.delete()
    except BadRequest:
        pass


def format_beijing_time(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return value[5:16].replace("T", " ")


def delivery_label(row: sqlite3.Row) -> str:
    if "retracted_at" in row.keys() and row["retracted_at"]:
        return " · 🗑 已撤回"
    if row["direction"] != "user":
        return ""
    status = row["delivery_status"] if "delivery_status" in row.keys() else "delivered"
    return {"pending": " · ⏳ 待通知", "failed": " · ⚠️ 通知失败"}.get(status, "")


def record_preview(user_id: int, limit: int = 3) -> str:
    with db() as conn:
        user = conn.execute("select * from users where user_id=?", (user_id,)).fetchone()
        rows = conn.execute("select * from messages where user_id=? order by id desc limit ?", (user_id, limit)).fetchall()
    title = html.escape(display_name(user) if user else str(user_id))
    unread = unread_count(user_id)
    lines = [f"<b>{'🔴 未处理 ' + str(unread) + ' 条' if unread else '✅ 已处理'}</b>", "<b>文本会话记录</b>", "────────────", title]
    for row in reversed(rows):
        who = "我" if row["direction"] == "admin" else "对方"
        text = row["text"] or (row["kind"] if row["kind"] != "text" else "")
        lines.append(f"\n<code>{html.escape(format_beijing_time(row['created_at']))}</code> <b>{who}{delivery_label(row)}</b>\n{html.escape((text or '非文本消息')[:700])}")
    return "\n".join(lines)[:MAX_TEXT]


def render_history(user_id: int, offset: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    with db() as conn:
        user = conn.execute("select * from users where user_id=?", (user_id,)).fetchone()
        rows = conn.execute("select * from messages where user_id=? order by id desc limit 11 offset ?", (user_id, offset)).fetchall()
    shown = rows[:10]
    has_more = len(rows) > 10
    title = html.escape(display_name(user) if user else str(user_id))
    first = offset + 1 if shown else 0
    lines = ["<b>文本会话记录</b>", "────────────", title, f"记录 {first}/{offset+len(shown)}"]
    for row in reversed(shown):
        who = "我" if row["direction"] == "admin" else "对方"
        text = row["text"] or (media_label(row["kind"]) if row["file_id"] else f"非文本消息：{row['kind']}")
        lines.append(f"\n<code>{html.escape(format_beijing_time(row['created_at']))}</code> <b>{who}{delivery_label(row)}</b>\n{html.escape(text[:900])}")
    media_rows = [row for row in reversed(shown) if row["file_id"]]
    return "\n".join(lines)[:MAX_TEXT], history_keyboard(user_id, offset, has_more, media_rows)


async def send_history(target_message: Message, user_id: int, offset: int = 0) -> None:
    text, keyboard = render_history(user_id, offset)
    await target_message.reply_html(text, reply_markup=keyboard)


async def edit_history(target_message: Message, user_id: int, offset: int = 0) -> None:
    text, keyboard = render_history(user_id, offset)
    await target_message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if is_admin(user.id):
        with db() as conn:
            first_use = conn.execute("select 1 from admin_onboarding where admin_id=?", (user.id,)).fetchone() is None
            if first_use:
                conn.execute("insert into admin_onboarding(admin_id,welcomed_at) values(?,?)", (user.id, now()))
        if first_use:
            await admin_help(update, context)
        else:
            await update.effective_message.reply_text("管理员已上线。发送 /help 可随时查看使用方法。")
    else:
        upsert_user(user)
        if is_blocked(user.id):
            await update.effective_message.reply_text("当前会话暂不可用。")
            return
        lock = verification_lock_until(user.id)
        if lock:
            await update.effective_message.reply_text(lock_message(lock))
            return
        if is_verified(user.id):
            await update.effective_message.reply_text("你已通过验证，可以直接发送内容。")
            return
        if not challenge_pending(get_user_status(user.id)):
            await send_challenge(update.effective_message, user.id)


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    await update.effective_message.reply_html(
        "<b>🤖 管理员指令帮助</b>\n\n"
        "<b>会话管理</b>\n"
        "/sessions — 查看最近会话（/list 同义）\n"
        "/search &lt;关键词&gt; — 按 ID、@用户名或姓名搜索\n"
        "/select &lt;用户ID&gt; — 选中发送会话\n"
        "/history &lt;用户ID&gt; — 查看完整会话记录\n\n"
        "<b>屏蔽管理</b>\n"
        "/blocked — 查看所有已屏蔽联系人\n"
        "/block &lt;用户ID&gt; — 屏蔽联系人\n"
        "/unblock &lt;用户ID&gt; — 解除屏蔽\n\n"
        "<b>数据维护</b>\n"
        "/cleanup — 清理历史聊天记录（需二次确认）\n\n"
        "<b>基础指令</b>\n"
        "/start — 查看机器人状态\n"
        "/help — 显示本帮助\n\n"
        "<b>快捷操作</b>\n"
        "• 回复用户消息卡片，可直接回复该用户\n"
        "• 也可先选中会话，再直接发送文字或媒体\n"
        "• 会话卡片可设置验证间隔、验证豁免及屏蔽状态"
    )


def cleanup_message_count(days: int | None) -> int:
    with db() as conn:
        if days is None:
            return int(conn.execute("select count(*) from messages").fetchone()[0])
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        return int(conn.execute("select count(*) from messages where created_at < ?", (cutoff,)).fetchone()[0])


def clear_message_history(days: int | None) -> int:
    """Delete message history only; preserve routing maps, contacts and settings."""
    with db() as conn:
        if days is None:
            cursor = conn.execute("delete from messages")
        else:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            cursor = conn.execute("delete from messages where created_at < ?", (cutoff,))
        deleted = cursor.rowcount
    with db() as conn:
        conn.execute("vacuum")
    return max(0, deleted)


def parse_cleanup_scope(value: str) -> int | None:
    if value == "all":
        return None
    days = int(value)
    if days not in (30, 90, 180):
        raise ValueError("unsupported cleanup scope")
    return days


async def cleanup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    total = cleanup_message_count(None)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("清理30天前", callback_data="cleanup_preview:30"), InlineKeyboardButton("清理90天前", callback_data="cleanup_preview:90")],
        [InlineKeyboardButton("清理180天前", callback_data="cleanup_preview:180"), InlineKeyboardButton("清理全部记录", callback_data="cleanup_preview:all")],
        [InlineKeyboardButton("取消", callback_data="cleanup_cancel")],
    ])
    await update.effective_message.reply_text(
        f"🧹 数据清理\n\n当前共有 {total} 条聊天记录。\n只会删除聊天历史，不会删除回复映射、联系人或设置。",
        reply_markup=keyboard,
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    await send_sessions(update.effective_message, update.effective_user.id, 0)


async def render_sessions(admin_id: int, offset: int = 0):
    with db() as conn:
        rows = conn.execute("select u.*, max(m.created_at) as last_at from users u left join messages m on m.user_id=u.user_id group by u.user_id order by coalesce(last_at,u.updated_at) desc limit 11 offset ?", (offset,)).fetchall()
        selected_row = conn.execute("select selected_user_id from admin_state where admin_id=?", (admin_id,)).fetchone()
    shown, has_more = rows[:10], len(rows) > 10
    if not shown:
        return "暂无会话", None
    selected_id = selected_row["selected_user_id"] if selected_row else None
    lines, buttons = ["<b>最近会话</b>"], []
    for row in shown:
        prefix = "✅" if row["user_id"] == selected_id else ("🔴" if row["unread_count"] else "⚪")
        last = format_beijing_time(row["last_at"] or row["updated_at"])
        lines.append(f"{prefix} {html.escape(display_name(row))}\n未处理：{row['unread_count']} · {last}")
        buttons.append([InlineKeyboardButton(f"{prefix} {row['first_name'] or row['username'] or row['user_id']}", callback_data=f"sessions_open:{row['user_id']}:{offset}")])
    nav = []
    if offset > 0: nav.append(InlineKeyboardButton("上一页", callback_data=f"sessions:{max(0, offset-10)}"))
    if has_more: nav.append(InlineKeyboardButton("下一页", callback_data=f"sessions:{offset+10}"))
    if nav: buttons.append(nav)
    return "\n\n".join(lines)[:MAX_TEXT], InlineKeyboardMarkup(buttons)


async def send_sessions(message: Message, admin_id: int, offset: int = 0) -> None:
    text, keyboard = await render_sessions(admin_id, offset)
    await message.reply_html(text, reply_markup=keyboard)


async def blocked_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    await send_blocked(update.effective_message, 0)


async def send_blocked(message: Message, offset: int = 0) -> None:
    with db() as conn:
        rows = conn.execute("select u.*, s.blocked_at from user_status s join users u on u.user_id=s.user_id where s.blocked=1 order by coalesce(s.blocked_at,s.updated_at) desc limit 11 offset ?", (offset,)).fetchall()
    shown, has_more = rows[:10], len(rows) > 10
    if not shown:
        await message.reply_text("暂无已屏蔽联系人")
        return
    lines = ["<b>已屏蔽联系人</b>"]
    buttons = []
    for row in shown:
        blocked_at = format_beijing_time(row["blocked_at"]) if row["blocked_at"] else "时间未知"
        lines.append(f"🚫 {html.escape(display_name(row))}\n屏蔽时间：{blocked_at}")
        short_name = row["first_name"] or row["username"] or str(row["user_id"])
        buttons.append([
            InlineKeyboardButton(f"查看：{short_name}"[:30], callback_data=f"session:{row['user_id']}"),
            InlineKeyboardButton(f"⚠️ 解除：{short_name}"[:30], callback_data=f"blocked_ask:{row['user_id']}:{offset}"),
        ])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("上一页", callback_data=f"blocked_page:{max(0, offset-10)}"))
    if has_more:
        nav.append(InlineKeyboardButton("下一页", callback_data=f"blocked_page:{offset+10}"))
    if nav:
        buttons.append(nav)
    await message.reply_html("\n\n".join(lines)[:MAX_TEXT], reply_markup=InlineKeyboardMarkup(buttons))


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    query = " ".join(context.args).strip().lstrip("@")
    if not query:
        await update.effective_message.reply_text("用法：/search <用户ID、用户名或姓名>")
        return
    pattern = f"%{query}%"
    with db() as conn:
        rows = conn.execute("select * from users where cast(user_id as text)=? or username like ? collate nocase or first_name like ? collate nocase or last_name like ? collate nocase or trim(first_name || ' ' || last_name) like ? collate nocase order by updated_at desc limit 20", (query, pattern, pattern, pattern, pattern)).fetchall()
    if not rows:
        await update.effective_message.reply_text("没有找到匹配用户")
        return
    for row in rows:
        await update.effective_message.reply_html(record_preview(row["user_id"], 2), reply_markup=user_keyboard(row["user_id"], is_blocked(row["user_id"])))


async def select_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    user_id = parse_user_id(context.args)
    if user_id is None:
        await update.effective_message.reply_text("用法：/select <用户ID>")
        return
    with db() as conn:
        conn.execute("insert into admin_state(admin_id,selected_user_id,updated_at) values(?,?,?) on conflict(admin_id) do update set selected_user_id=excluded.selected_user_id, updated_at=excluded.updated_at", (update.effective_user.id, user_id, now()))
    await update.effective_message.reply_text(f"已选中会话：{user_id}")


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    user_id = parse_user_id(context.args)
    if user_id is None:
        await update.effective_message.reply_text("用法：/history <用户ID>")
        return
    await send_history(update.effective_message, user_id, 0)


async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    user_id = parse_user_id(context.args)
    if user_id is None:
        await update.effective_message.reply_text("用法：/block <用户ID>")
        return
    set_blocked(user_id, True)
    await update.effective_message.reply_text(f"已屏蔽联系人：{user_id}")


async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    user_id = parse_user_id(context.args)
    if user_id is None:
        await update.effective_message.reply_text("用法：/unblock <用户ID>")
        return
    set_blocked(user_id, False)
    await update.effective_message.reply_text(f"已解除屏蔽：{user_id}")


async def forward_to_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    # This relay is private-chat only. Never challenge or forward messages
    # from groups where the bot has been added as an administrator.
    if not msg or msg.chat.type != "private" or not user or is_admin(user.id):
        return
    upsert_user(user)
    if is_blocked(user.id):
        await msg.reply_text("当前会话暂不可用。")
        return
    lock = verification_lock_until(user.id)
    if lock:
        await msg.reply_text(lock_message(lock))
        return
    if not is_verified(user.id):
        if challenge_pending(get_user_status(user.id)):
            return
        await send_challenge(msg, user.id)
        return
    allowed, should_notify = consume_rate_limit(user.id)
    if not allowed:
        if should_notify:
            force_reverify(user.id)
            await msg.reply_text(f"发送过于频繁（每分钟最多 {MESSAGES_PER_MINUTE} 条），请稍后重新验证。")
        return
    kind = message_kind(msg)
    text = message_text(msg)
    file_id = file_id_for(msg)
    with db() as conn:
        previous_row = conn.execute("select admin_message_id from messages where user_id=? and direction='user' and admin_message_id is not null order by id desc limit 1", (user.id,)).fetchone()
    previous_admin_message_id = previous_row["admin_message_id"] if previous_row else None
    message_row_id = save_message(user.id, "user", kind, text, file_id, user_message_id=msg.message_id)
    mark_unread(user.id)
    header = f"<b>🔴 新消息</b>\n{html.escape(display_name(user))}\n\n{html.escape(text[:1600] if text else kind)}"
    delivered = 0
    admin_id = ADMIN_ID
    try:
        with db() as conn:
            old_message_ids = [row["admin_message_id"] for row in conn.execute("select admin_message_id from admin_message_map where admin_chat_id=? and user_id=? order by created_at desc limit 20", (admin_id, user.id)).fetchall()]
        for old_message_id in old_message_ids:
            try:
                await context.bot.edit_message_reply_markup(admin_id, old_message_id, reply_markup=None)
            except BadRequest:
                pass
        with db() as conn:
            selected_row = conn.execute("select selected_user_id from admin_state where admin_id=?", (admin_id,)).fetchone()
        reply_markup = selected_keyboard(user.id) if selected_row and selected_row["selected_user_id"] == user.id else user_keyboard(user.id)
        sent = await context.bot.send_message(admin_id, header, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        delivered = 1
        save_admin_message_map(admin_id, sent.message_id, user.id)
        with db() as conn:
            conn.execute("update messages set admin_message_id=? where id=?", (sent.message_id, message_row_id))
        if kind != "text":
            forwarded = await msg.forward(admin_id)
            save_admin_message_map(admin_id, forwarded.message_id, user.id)
    except Exception:
        log.exception("failed to forward to administrator")
    with db() as conn:
        conn.execute(
            "update messages set delivery_status=?,delivered_count=? where id=?",
            ("delivered" if delivered else "failed", delivered, message_row_id),
        )
    if delivered == 0:
        await msg.reply_text("消息已保存，但暂时未能通知管理员，请稍后重试。")


async def admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    admin = update.effective_user
    if not msg or not admin or not is_admin(admin.id):
        return
    target_user_id = None
    if msg.reply_to_message:
        target_user_id = resolve_admin_reply(msg.chat_id, msg.reply_to_message.message_id)
        if target_user_id is None:
            await msg.reply_text("无法识别这条引用消息，请从对应会话卡片重新选择后发送。")
            return
    else:
        with db() as conn:
            row = conn.execute("select selected_user_id from admin_state where admin_id=?", (admin.id,)).fetchone()
            if row:
                target_user_id = row["selected_user_id"]
    if target_user_id is None:
        return
    kind = message_kind(msg)
    text = message_text(msg)
    try:
        if kind == "text":
            sent = await context.bot.send_message(target_user_id, text)
        else:
            sent = await msg.copy(target_user_id)
        saved_id = save_message(target_user_id, "admin", kind, text, file_id_for(msg), admin_message_id=msg.message_id, user_message_id=sent.message_id)
        mark_read(target_user_id)
        with db() as conn:
            target_user = conn.execute("select * from users where user_id=?", (target_user_id,)).fetchone()
        target_label = display_name(target_user) if target_user else str(target_user_id)
        await msg.reply_text(f"已发送给：{target_label}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 撤回这条消息", callback_data=f"retract:{saved_id}")]]))
    except Exception:
        log.exception("failed to deliver administrator message")
        await msg.reply_text("发送失败，请稍后重试。")


async def notify_admin_verification_lock(context: ContextTypes.DEFAULT_TYPE, user_id: int, lock: datetime) -> None:
    """Give the sole administrator a rescue card even when no user message was relayed."""
    with db() as conn:
        user = conn.execute("select * from users where user_id=?", (user_id,)).fetchone()
    identity = display_name(user) if user else f"用户 / @- / {user_id}"
    text = (
        "<b>🔒 联系人验证已锁定</b>\n"
        f"{html.escape(identity)}\n\n"
        "原因：连续两次验证失败\n"
        f"锁定至：{html.escape(lock.strftime('%Y-%m-%d %H:%M:%S'))}\n"
        "该联系人的消息尚未转发。可在下方解除锁定或屏蔽联系人。"
    )
    try:
        sent = await context.bot.send_message(
            ADMIN_ID,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=user_keyboard(user_id, is_blocked(user_id)),
        )
        save_admin_message_map(ADMIN_ID, sent.message_id, user_id)
    except Exception:
        log.exception("failed to send verification lock rescue card")


async def _callbacks_unchecked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    if data.startswith("cleanup_preview:"):
        if not is_admin(query.from_user.id):
            return
        value = data.split(":", 1)[1]
        days = parse_cleanup_scope(value)
        count = cleanup_message_count(days)
        label = "全部" if days is None else f"{days}天前的"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⚠️ 确认删除 {count} 条", callback_data=f"cleanup_confirm:{value}")],
            [InlineKeyboardButton("取消", callback_data="cleanup_cancel")],
        ])
        await query.answer()
        await query.message.edit_text(
            f"⚠️ 确认清理{label}聊天记录？\n\n预计删除：{count} 条\n回复映射、联系人及设置将永久保留。此操作无法撤销。",
            reply_markup=keyboard,
        )
        return
    if data.startswith("cleanup_confirm:"):
        if not is_admin(query.from_user.id):
            return
        value = data.split(":", 1)[1]
        days = parse_cleanup_scope(value)
        deleted = await asyncio.to_thread(clear_message_history, days)
        await query.answer("清理完成")
        await query.message.edit_text(f"✅ 已清理 {deleted} 条聊天记录。\n回复映射、联系人及设置均已保留。")
        return
    if data == "cleanup_cancel":
        if not is_admin(query.from_user.id):
            return
        await query.answer("已取消")
        await delete_message_quietly(query.message)
        return
    if data.startswith("refreshverify:"):
        user_id = int(data.split(":", 1)[1])
        if query.from_user.id != user_id:
            await query.answer("这不是你的验证。", show_alert=True)
            return
        lock = verification_lock_until(user_id)
        if lock:
            await query.answer("验证已暂停", show_alert=True)
            return
        await query.answer("已更换验证码")
        await delete_message_quietly(query.message)
        await send_challenge(query.message, user_id)
        return
    if data.startswith("verify:"):
        _, user_id_text, answer = data.split(":", 2)
        user_id = int(user_id_text)
        if query.from_user.id != user_id:
            await query.answer("这不是你的验证。", show_alert=True)
            return
        row = get_user_status(user_id)
        lock = verification_lock_until(user_id)
        if lock:
            await query.answer("验证已暂停", show_alert=True)
            await delete_message_quietly(query.message)
            await query.message.reply_text(lock_message(lock))
            return
        if row and row["verify_exempt"]:
            await query.answer("已豁免验证")
            await delete_message_quietly(query.message)
            await query.message.reply_text("你已被管理员永久豁免验证，可以直接发送内容。")
        elif row and not row["blocked"] and not challenge_expired(row) and answer == (row["challenge_answer"] or ""):
            set_verified(user_id)
            reset_rate_limit(user_id)
            reset_verification_failures(user_id)
            await query.answer("验证通过")
            await delete_message_quietly(query.message)
            await query.message.reply_text("验证通过。现在可以直接发送内容，我会转给管理员。")
        else:
            lock = record_verification_failure(user_id)
            await query.answer("验证失败，已暂停6小时。" if lock else "验证失败，还可重试1次。", show_alert=True)
            await delete_message_quietly(query.message)
            if lock:
                await query.message.reply_text(lock_message(lock))
                await notify_admin_verification_lock(context, user_id, lock)
            else:
                await send_challenge(query.message, user_id, "验证失败，还可重试1次")
        return
    if not is_admin(query.from_user.id):
        return
    await query.answer()
    if data.startswith("select:"):
        user_id = int(data.split(":", 1)[1])
        mark_read(user_id)
        with db() as conn:
            conn.execute("insert into admin_state(admin_id,selected_user_id,updated_at) values(?,?,?) on conflict(admin_id) do update set selected_user_id=excluded.selected_user_id, updated_at=excluded.updated_at", (query.from_user.id, user_id, now()))
        await query.message.edit_reply_markup(reply_markup=selected_keyboard(user_id))
    elif data.startswith("unselect:"):
        user_id = int(data.split(":", 1)[1])
        with db() as conn:
            conn.execute("delete from admin_state where admin_id=?", (query.from_user.id,))
        await query.message.edit_reply_markup(reply_markup=user_keyboard(user_id, is_blocked(user_id)))
    elif data.startswith("blocked_page:"):
        await delete_message_quietly(query.message)
        await send_blocked(query.message, int(data.split(":", 1)[1]))
    elif data.startswith("blocked_ask:"):
        _, user_id, offset = data.split(":")
        with db() as conn:
            row = conn.execute("select * from users where user_id=?", (int(user_id),)).fetchone()
        name = display_name(row) if row else user_id
        await query.answer()
        await query.message.reply_html(
            f"⚠️ <b>确认解除屏蔽？</b>\n\n{html.escape(name)}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 确认解除", callback_data=f"blocked_unblock:{user_id}:{offset}"),
                InlineKeyboardButton("取消", callback_data="blocked_cancel"),
            ]]),
        )
    elif data == "blocked_cancel":
        await query.answer("已取消")
        await delete_message_quietly(query.message)
    elif data.startswith("blocked_unblock:"):
        _, user_id, offset = data.split(":")
        set_blocked(int(user_id), False)
        await query.answer("已解除屏蔽")
        await delete_message_quietly(query.message)
        await send_blocked(query.message, int(offset))
    elif data.startswith("sessions_open:"):
        _, user_id, offset = data.split(":")
        user_id, offset = int(user_id), int(offset)
        mark_read(user_id)
        text = record_preview(user_id)
        keyboard = user_keyboard(user_id, is_blocked(user_id)).inline_keyboard
        keyboard = InlineKeyboardMarkup([*keyboard, [InlineKeyboardButton("⬅️ 返回最近会话", callback_data=f"sessions_back:{offset}")]])
        await query.answer("已标记为已处理")
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif data.startswith("sessions_back:"):
        offset = int(data.split(":", 1)[1])
        text, keyboard = await render_sessions(query.from_user.id, offset)
        await query.answer()
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif data.startswith("sessions:"):
        offset = int(data.split(":", 1)[1])
        text, keyboard = await render_sessions(query.from_user.id, offset)
        await query.answer()
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif data.startswith("session:"):
        user_id = int(data.split(":", 1)[1])
        mark_read(user_id)
        await query.message.reply_html(record_preview(user_id, 3), reply_markup=user_keyboard(user_id, is_blocked(user_id)))
    elif data.startswith("history_open:"):
        _, user_id, offset = data.split(":")
        await send_history(query.message, int(user_id), int(offset))
    elif data.startswith("retract:"):
        saved_id = int(data.split(":", 1)[1])
        with db() as conn:
            row = conn.execute("select * from messages where id=? and direction='admin'", (saved_id,)).fetchone()
        if not row or not row["user_message_id"] or row["retracted_at"]:
            await query.answer("记录不存在或已撤回", show_alert=True)
        else:
            try:
                await context.bot.delete_message(row["user_id"], row["user_message_id"])
                with db() as conn:
                    conn.execute("update messages set retracted_at=? where id=?", (now(), saved_id))
                await query.answer("已从对方聊天中撤回")
                await query.message.edit_text("✅ 消息已撤回")
            except Exception:
                log.exception("failed to retract administrator message")
                await query.answer("撤回失败，请稍后重试。", show_alert=True)
    elif data.startswith("media:"):
        message_id = int(data.split(":", 1)[1])
        with db() as conn:
            row = conn.execute("select * from messages where id=?", (message_id,)).fetchone()
        if not row or not row["file_id"]:
            await query.answer("媒体不存在或已失效", show_alert=True)
        else:
            caption = f"{media_label(row['kind'])} · {format_beijing_time(row['created_at'])}"
            methods = {"photo": context.bot.send_photo, "video": context.bot.send_video, "voice": context.bot.send_voice, "audio": context.bot.send_audio, "document": context.bot.send_document, "sticker": context.bot.send_sticker}
            method = methods.get(row["kind"])
            if not method:
                await query.answer("暂不支持预览此类型", show_alert=True)
            else:
                kwargs = {row["kind"] if row["kind"] != "sticker" else "sticker": row["file_id"]}
                if row["kind"] != "sticker": kwargs["caption"] = caption
                preview = await method(query.message.chat_id, **kwargs)
                await preview.edit_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("关闭预览", callback_data="media_close")]]))
    elif data == "media_close":
        await query.answer()
        await delete_message_quietly(query.message)
    elif data.startswith("history:"):
        _, user_id, offset = data.split(":")
        await edit_history(query.message, int(user_id), int(offset))
    elif data.startswith("history_close:"):
        await delete_message_quietly(query.message)
    elif data.startswith("block:"):
        user_id = int(data.split(":", 1)[1])
        set_blocked(user_id, True)
        await query.message.edit_reply_markup(reply_markup=user_keyboard(user_id, is_blocked(user_id)))
    elif data.startswith("unblock:"):
        user_id = int(data.split(":", 1)[1])
        set_blocked(user_id, False)
        await query.message.edit_reply_markup(reply_markup=user_keyboard(user_id, is_blocked(user_id)))
    elif data.startswith("setverify:"):
        _, user_id_text, minutes_text = data.split(":", 2)
        user_id = int(user_id_text)
        minutes = int(minutes_text)
        set_user_verify_interval_minutes(user_id, minutes)
        await query.message.edit_reply_markup(reply_markup=user_keyboard(user_id, is_blocked(user_id)))
    elif data.startswith("verify_menu:"):
        user_id = int(data.split(":", 1)[1])
        await query.message.edit_reply_markup(reply_markup=verify_time_keyboard(user_id))
    elif data.startswith("back_user:"):
        user_id = int(data.split(":", 1)[1])
        await query.message.edit_reply_markup(reply_markup=user_keyboard(user_id, is_blocked(user_id)))
    elif data.startswith("unlockverify:"):
        user_id = int(data.split(":", 1)[1])
        if not is_verification_locked(user_id):
            await query.answer("该联系人当前未被锁定", show_alert=True)
        else:
            reset_verification_failures(user_id)
            force_reverify(user_id)
            await query.answer("已解除验证锁定")
            await delete_message_quietly(query.message)
            try:
                await context.bot.send_message(user_id, "管理员已解除验证锁定。请重新发送内容并完成新的验证。")
            except Exception:
                log.exception("failed to notify user after verification unlock")
    elif data.startswith("exempt:"):
        user_id = int(data.split(":", 1)[1])
        set_verify_exempt(user_id, True)
        await query.message.edit_reply_markup(reply_markup=user_keyboard(user_id, is_blocked(user_id)))
    elif data.startswith("unexempt:"):
        user_id = int(data.split(":", 1)[1])
        set_verify_exempt(user_id, False)
        await query.message.edit_reply_markup(reply_markup=user_keyboard(user_id, is_blocked(user_id)))
    elif data.startswith("noop:"):
        user_id = int(data.split(":", 1)[1])
        await query.answer(f"当前验证间隔：{format_interval(get_user_verify_interval_minutes(user_id))}")


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _callbacks_unchecked(update, context)
    except (ValueError, TypeError, IndexError):
        query = update.callback_query
        log.warning("ignored malformed or expired callback data")
        if query:
            try:
                await query.answer("操作已失效，请返回会话列表重试。", show_alert=True)
            except BadRequest:
                pass


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    init_db()
    cleanup_transient_data()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(error_handler)
    private_chat = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start_cmd, filters=private_chat))
    app.add_handler(CommandHandler("help", admin_help, filters=private_chat))
    app.add_handler(CommandHandler("list", list_cmd, filters=private_chat))
    app.add_handler(CommandHandler("sessions", list_cmd, filters=private_chat))
    app.add_handler(CommandHandler("search", search_cmd, filters=private_chat))
    app.add_handler(CommandHandler("blocked", blocked_cmd, filters=private_chat))
    app.add_handler(CommandHandler("select", select_cmd, filters=private_chat))
    app.add_handler(CommandHandler("history", history_cmd, filters=private_chat))
    app.add_handler(CommandHandler("block", block_cmd, filters=private_chat))
    app.add_handler(CommandHandler("unblock", unblock_cmd, filters=private_chat))
    app.add_handler(CommandHandler("cleanup", cleanup_cmd, filters=private_chat))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(private_chat & filters.ALL & filters.Chat(ADMIN_ID) & ~filters.COMMAND, admin_message))
    app.add_handler(MessageHandler(private_chat & filters.ALL & ~filters.COMMAND, forward_to_admins))
    log.info("starting relay bot administrator configured")
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

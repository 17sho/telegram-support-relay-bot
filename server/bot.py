import html
import json
import logging
import os
import random

from dotenv import load_dotenv

load_dotenv()
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BASE_DIR = Path(os.environ.get("TG_RELAY_BASE_DIR", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "relay.db"
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
          created_at text not null
        );
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
          message_count integer not null default 0
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


def consume_rate_limit(user_id: int) -> bool:
    """Count an inbound message and return False once the fixed one-minute window is exceeded."""
    current = datetime.now()
    with db() as conn:
        row = conn.execute("select window_started_at,message_count from rate_limits where user_id=?", (user_id,)).fetchone()
        started = parse_time(row["window_started_at"]) if row else None
        if not row or not started or current - started >= timedelta(minutes=1):
            conn.execute(
                "insert into rate_limits(user_id,window_started_at,message_count) values(?,?,1) "
                "on conflict(user_id) do update set window_started_at=excluded.window_started_at,message_count=1",
                (user_id, now()),
            )
            return True
        count = int(row["message_count"]) + 1
        conn.execute("update rate_limits set message_count=? where user_id=?", (count, user_id))
        return count <= MESSAGES_PER_MINUTE


def reset_rate_limit(user_id: int) -> None:
    with db() as conn:
        conn.execute("delete from rate_limits where user_id=?", (user_id,))


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
        lock = datetime.now() + timedelta(hours=24) if failures >= 2 else None
        conn.execute("insert into verification_attempts(user_id,failed_attempts,locked_until,updated_at) values(?,?,?,?) on conflict(user_id) do update set failed_attempts=excluded.failed_attempts,locked_until=excluded.locked_until,updated_at=excluded.updated_at", (user_id, failures, lock.strftime("%Y-%m-%d %H:%M:%S") if lock else None, now()))
        if lock:
            conn.execute("update user_status set challenge_answer='',challenge_at=null,updated_at=? where user_id=?", (now(), user_id))
        return lock


def reset_verification_failures(user_id: int) -> None:
    with db() as conn:
        conn.execute("delete from verification_attempts where user_id=?", (user_id,))


def lock_message(lock: datetime) -> str:
    return f"验证失败次数过多，已暂停验证24小时。可于 {lock.strftime('%Y-%m-%d %H:%M')} 后重试。"


def challenge_expired(row: sqlite3.Row | None) -> bool:
    started = parse_time(row["challenge_at"]) if row and row["challenge_at"] else None
    return not started or datetime.now() - started > timedelta(minutes=2)


def force_reverify(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            "insert into user_status(user_id,verified,blocked,challenge_answer,updated_at) values(?,?,?,?,?) "
            "on conflict(user_id) do update set verified=0, challenge_answer='', updated_at=excluded.updated_at",
            (user_id, 0, 0, "", now()),
        )


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


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
    if message.text or message.caption:
        return "text"
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("打开个人资料", url=f"tg://user?id={user_id}"), InlineKeyboardButton("选中会话", callback_data=f"select:{user_id}")],
        [InlineKeyboardButton("查看会话记录", callback_data=f"history_open:{user_id}:0"), InlineKeyboardButton(block_label, callback_data=f"{block_action}:{user_id}")],
        [InlineKeyboardButton(f"验证时间：{format_interval(interval_minutes)}", callback_data=f"verify_menu:{user_id}"), InlineKeyboardButton(exempt_label, callback_data=f"{exempt_action}:{user_id}")],
    ])


def verify_time_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"setverify:{user_id}:{minutes}") for minutes, label in VERIFY_INTERVAL_PRESETS[:2]],
        [InlineKeyboardButton(label, callback_data=f"setverify:{user_id}:{minutes}") for minutes, label in VERIFY_INTERVAL_PRESETS[2:]],
        [InlineKeyboardButton("返回会话卡片", callback_data=f"back_user:{user_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def challenge_keyboard(user_id: int, answer: str, choices: list[str] | None = None) -> InlineKeyboardMarkup:
    if not choices:
        choices_set = {answer}
        value = int(answer)
        nearby = [value + delta for delta in (-12, -8, -5, -3, -2, -1, 1, 2, 3, 5, 8, 12) if value + delta >= 0]
        random.shuffle(nearby)
        for candidate in nearby:
            choices_set.add(str(candidate))
            if len(choices_set) >= 8:
                break
        while len(choices_set) < 8:
            choices_set.add(str(random.randint(max(0, value - 20), value + 20)))
        choices = list(choices_set)
        random.shuffle(choices)
    buttons = [InlineKeyboardButton(choice, callback_data=f"verify:{user_id}:{choice}") for choice in choices]
    return InlineKeyboardMarkup([buttons[:4], buttons[4:]])


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
    value = row["verify_interval_minutes"] if row and row["verify_interval_minutes"] else DEFAULT_VERIFY_INTERVAL_MINUTES
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
    last_user_message = last_inbound_message_at(user_id)
    if last_user_message is None:
        # 已验证但还没真正发过消息：保持验证有效，避免刚验证完立刻又要验证。
        return True
    return datetime.now() - last_user_message <= timedelta(minutes=get_user_verify_interval_minutes(user_id))


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
               on conflict(user_id) do update set verified=1, challenge_answer='', verified_at=excluded.verified_at, updated_at=excluded.updated_at""",
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


def create_challenge(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    challenge_type = random.choice(["add3", "sub", "mul", "mixed", "sequence", "second_max"])
    if challenge_type == "add3":
        left, middle, right = (random.randint(11, 49) for _ in range(3))
        answer = str(left + middle + right)
        question = f"请计算：{left} + {middle} + {right} = ?"
        keyboard = challenge_keyboard(user_id, answer)
    elif challenge_type == "sub":
        right = random.randint(12, 49)
        answer_value = random.randint(15, 69)
        left = right + answer_value
        answer = str(answer_value)
        question = f"请计算：{left} − {right} = ?"
        keyboard = challenge_keyboard(user_id, answer)
    elif challenge_type == "mul":
        left = random.randint(11, 19)
        right = random.randint(3, 9)
        answer = str(left * right)
        question = f"请计算：{left} × {right} = ?"
        keyboard = challenge_keyboard(user_id, answer)
    elif challenge_type == "mixed":
        left, middle, right = random.randint(3, 12), random.randint(2, 9), random.randint(5, 25)
        answer = str(left * middle + right)
        question = f"先乘后加：{left} × {middle} + {right} = ?"
        keyboard = challenge_keyboard(user_id, answer)
    elif challenge_type == "sequence":
        start, step = random.randint(3, 25), random.randint(3, 12)
        sequence = [start + step * i for i in range(5)]
        answer = str(sequence[-1])
        question = f"找规律，下一项是？ {sequence[0]}，{sequence[1]}，{sequence[2]}，{sequence[3]}，?"
        keyboard = challenge_keyboard(user_id, answer)
    else:
        numbers = random.sample(range(10, 100), 8)
        answer = str(sorted(numbers, reverse=True)[1])
        choices = [str(n) for n in numbers]
        random.shuffle(choices)
        question = "请点击第二大的数字"
        keyboard = challenge_keyboard(user_id, answer, choices)
    with db() as conn:
        conn.execute(
            """insert into user_status(user_id,verified,blocked,challenge_answer,challenge_at,updated_at)
               values(?,?,?,?,?,?)
               on conflict(user_id) do update set challenge_answer=excluded.challenge_answer, challenge_at=excluded.challenge_at, updated_at=excluded.updated_at""",
            (user_id, 0, 0, answer, now(), now()),
        )
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
        lines.append(f"\n<code>{html.escape(format_beijing_time(row['created_at']))}</code> <b>{who}</b>\n{html.escape((text or '非文本消息')[:700])}")
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
        lines.append(f"\n<code>{html.escape(format_beijing_time(row['created_at']))}</code> <b>{who}</b>\n{html.escape(text[:900])}")
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
        question, keyboard = create_challenge(user.id)
        await update.effective_message.reply_text(f"请先完成人机验证：{question}", reply_markup=keyboard)


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
        "<b>基础指令</b>\n"
        "/start — 查看机器人状态\n"
        "/help — 显示本帮助\n\n"
        "<b>快捷操作</b>\n"
        "• 回复用户消息卡片，可直接回复该用户\n"
        "• 也可先选中会话，再直接发送文字或媒体\n"
        "• 会话卡片可设置验证间隔、验证豁免及屏蔽状态"
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
    if not context.args:
        await update.effective_message.reply_text("用法：/select <用户ID>")
        return
    user_id = int(context.args[0])
    with db() as conn:
        conn.execute("insert into admin_state(admin_id,selected_user_id,updated_at) values(?,?,?) on conflict(admin_id) do update set selected_user_id=excluded.selected_user_id, updated_at=excluded.updated_at", (update.effective_user.id, user_id, now()))
    await update.effective_message.reply_text(f"已选中会话：{user_id}")


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    if not context.args:
        await update.effective_message.reply_text("用法：/history <用户ID>")
        return
    await send_history(update.effective_message, int(context.args[0]), 0)


async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    if not context.args:
        await update.effective_message.reply_text("用法：/block <用户ID>")
        return
    user_id = int(context.args[0])
    set_blocked(user_id, True)
    await update.effective_message.reply_text(f"已屏蔽联系人：{user_id}")


async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id if update.effective_user else None):
        return
    if not context.args:
        await update.effective_message.reply_text("用法：/unblock <用户ID>")
        return
    user_id = int(context.args[0])
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
    status_row = get_user_status(user.id)
    if not (status_row and status_row["verify_exempt"]) and not consume_rate_limit(user.id):
        force_reverify(user.id)
        question, keyboard = create_challenge(user.id)
        await msg.reply_text(f"发送过于频繁（每分钟最多 {MESSAGES_PER_MINUTE} 条），请重新完成人机验证：{question}", reply_markup=keyboard)
        return
    if not is_verified(user.id):
        question, keyboard = create_challenge(user.id)
        await msg.reply_text(f"请先完成人机验证：{question}", reply_markup=keyboard)
        return
    kind = message_kind(msg)
    text = message_text(msg)
    file_id = file_id_for(msg)
    with db() as conn:
        previous_row = conn.execute("select admin_message_id from messages where user_id=? and direction='user' and admin_message_id is not null order by id desc limit 1", (user.id,)).fetchone()
    previous_admin_message_id = previous_row["admin_message_id"] if previous_row else None
    save_message(user.id, "user", kind, text, file_id, user_message_id=msg.message_id)
    mark_unread(user.id)
    header = f"<b>🔴 新消息</b>\n{html.escape(display_name(user))}\n\n{html.escape(text[:1600] if text else kind)}"
    for admin_id in ADMIN_IDS:
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
            save_admin_message_map(admin_id, sent.message_id, user.id)
            with db() as conn:
                conn.execute("update messages set admin_message_id=? where user_id=? and user_message_id=?", (sent.message_id, user.id, msg.message_id))
            if kind != "text":
                forwarded = await msg.forward(admin_id)
                save_admin_message_map(admin_id, forwarded.message_id, user.id)
        except Exception:
            log.exception("failed to forward to admin %s", admin_id)


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
    except Exception as exc:
        await msg.reply_text(f"发送失败：{exc}")


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
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
            await query.answer("验证失败，已暂停24小时。" if lock else "验证失败，还可重试1次。", show_alert=True)
            await delete_message_quietly(query.message)
            if lock:
                await query.message.reply_text(lock_message(lock))
            else:
                question, keyboard = create_challenge(user_id)
                await query.message.reply_text(f"验证失败，还可重试1次：{question}", reply_markup=keyboard)
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
        if not row or not row["user_message_id"]:
            await query.answer("记录不存在或已撤回", show_alert=True)
        else:
            try:
                await context.bot.delete_message(row["user_id"], row["user_message_id"])
                with db() as conn:
                    conn.execute("delete from messages where id=?", (saved_id,))
                await query.answer("已从对方聊天中撤回")
                await query.message.edit_text("✅ 消息已撤回")
            except Exception as exc:
                await query.answer(f"撤回失败：{exc}", show_alert=True)
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


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS is required")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
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
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(private_chat & filters.ALL & filters.Chat(list(ADMIN_IDS)) & ~filters.COMMAND, admin_message))
    app.add_handler(MessageHandler(private_chat & filters.ALL & ~filters.COMMAND, forward_to_admins))
    log.info("starting relay bot admins=%s", sorted(ADMIN_IDS))
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

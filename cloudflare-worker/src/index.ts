type Env = {
  DB: D1Database;
  BOT_TOKEN: string;
  ADMIN_IDS: string;
  DEFAULT_VERIFY_INTERVAL_MINUTES?: string;
  MESSAGES_PER_MINUTE?: string;
};

type TgUser = { id: number; is_bot?: boolean; first_name?: string; last_name?: string; username?: string };
type TgChat = { id: number; type: string };
type TgMessage = {
  message_id: number;
  from?: TgUser;
  chat: TgChat;
  text?: string;
  caption?: string;
  photo?: { file_id: string }[];
  video?: { file_id: string };
  voice?: { file_id: string };
  audio?: { file_id: string };
  document?: { file_id: string };
  sticker?: { file_id: string };
  reply_to_message?: TgMessage;
};
type TgCallback = { id: string; from: TgUser; message?: TgMessage; data?: string };
type TgUpdate = { message?: TgMessage; callback_query?: TgCallback };
type DbUser = TgUser & { user_id: number; unread_count?: number; created_at?: string; updated_at?: string };
type DbMsg = { id: number; user_id: number; direction: string; kind: string; text?: string; file_id?: string; admin_message_id?: number; user_message_id?: number; created_at: string };

const MAX_TEXT = 3500;
const VERIFY_INTERVAL_PRESETS: [number, string][] = [[0, "立即验证"], [60, "1小时"], [360, "6小时"], [1440, "24小时"]];
const DEFAULT_VERIFY_INTERVAL_MINUTES = 360;
const DEFAULT_MESSAGES_PER_MINUTE = 40;
const MIN_VERIFY_INTERVAL_MINUTES = 0;
const MAX_VERIFY_INTERVAL_MINUTES = 43200;

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/health") return json({ ok: true });
    if (url.pathname === "/setup-webhook") {
      const secret = url.searchParams.get("secret");
      if (!secret || secret !== env.BOT_TOKEN.slice(-16)) return new Response("forbidden", { status: 403 });
      return json(await tg(env, "setWebhook", { url: `${url.origin}/webhook`, allowed_updates: ["message", "callback_query"] }));
    }
    if (url.pathname !== "/webhook" || req.method !== "POST") return new Response("not found", { status: 404 });
    const update = await req.json<TgUpdate>();
    await ensureSchema(env);
    await handleUpdate(update, env);
    return json({ ok: true });
  },
};

function json(data: unknown, init?: ResponseInit) { return new Response(JSON.stringify(data), { ...init, headers: { "content-type": "application/json; charset=utf-8", ...(init?.headers || {}) } }); }
function esc(s: string) { return (s || "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]!)); }
function admins(env: Env) { return new Set((env.ADMIN_IDS || "").replace(/\s/g, "").split(",").filter(Boolean).map(Number)); }
function isAdmin(env: Env, id?: number) { return !!id && admins(env).has(id); }
function now() { return new Date().toISOString().slice(0, 19).replace("T", " "); }
function formatInterval(minutes: number) { if (minutes < 60) return `${minutes}分钟`; if (minutes % 60 === 0) return `${minutes / 60}小时`; return `${minutes}分钟`; }
function formatBeijingTime(value: string) { const normalized = value.includes("T") ? value : value.replace(" ", "T") + "Z"; const d = new Date(normalized); if (Number.isNaN(d.getTime())) return value.slice(5, 16).replace("T", " "); const parts = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(d); const get = (type: string) => parts.find(p => p.type === type)?.value || ""; return `${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`; }
function textOf(m: TgMessage) { return m.text || m.caption || ""; }
function kindOf(m: TgMessage) { if (m.text || m.caption) return "text"; if (m.photo) return "photo"; if (m.video) return "video"; if (m.voice) return "voice"; if (m.audio) return "audio"; if (m.document) return "document"; if (m.sticker) return "sticker"; return "message"; }
function fileIdFor(m: TgMessage) { return m.photo?.at(-1)?.file_id || m.video?.file_id || m.voice?.file_id || m.audio?.file_id || m.document?.file_id || m.sticker?.file_id || ""; }
function displayName(u: Partial<DbUser>) { return `${[u.first_name, u.last_name].filter(Boolean).join(" ") || (u.username ? `@${u.username}` : "用户")} / @${u.username || "-"} / ${u.user_id || u.id}`; }
function rand(a: number, b: number) { return Math.floor(Math.random() * (b - a + 1)) + a; }
function commandArg(text: string) { return text.trim().split(/\s+/)[1]; }

async function tg(env: Env, method: string, body: unknown) {
  const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}
async function send(env: Env, chat_id: number, text: string, extra: Record<string, unknown> = {}) { return tg(env, "sendMessage", { chat_id, text, parse_mode: "HTML", disable_web_page_preview: true, ...extra }); }
async function edit(env: Env, chat_id: number, message_id: number, text: string, extra: Record<string, unknown> = {}) { return tg(env, "editMessageText", { chat_id, message_id, text, parse_mode: "HTML", disable_web_page_preview: true, ...extra }); }
async function clearKeyboard(env: Env, chat_id: number, message_id?: number) { if (message_id) await tg(env, "editMessageReplyMarkup", { chat_id, message_id, reply_markup: { inline_keyboard: [] } }).catch(() => null); }
async function answerCb(env: Env, id: string, text?: string, show_alert = false) { return tg(env, "answerCallbackQuery", { callback_query_id: id, text, show_alert }); }
async function del(env: Env, chat_id?: number, message_id?: number) { if (chat_id && message_id) await tg(env, "deleteMessage", { chat_id, message_id }).catch(() => null); }
async function copyMessage(env: Env, chat_id: number, from_chat_id: number, message_id: number) { return tg(env, "copyMessage", { chat_id, from_chat_id, message_id }); }
async function forwardMessage(env: Env, chat_id: number, from_chat_id: number, message_id: number) { return tg(env, "forwardMessage", { chat_id, from_chat_id, message_id }); }

async function ensureSchema(env: Env) {
  await env.DB.batch([
    env.DB.prepare("CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, unread_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, direction TEXT NOT NULL, kind TEXT NOT NULL, text TEXT, file_id TEXT, admin_message_id INTEGER, user_message_id INTEGER, created_at TEXT NOT NULL)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS admin_message_map(admin_chat_id INTEGER NOT NULL, admin_message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(admin_chat_id,admin_message_id))"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS idx_admin_message_map_user ON admin_message_map(admin_chat_id,user_id,created_at DESC)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS admin_state(admin_id INTEGER PRIMARY KEY, selected_user_id INTEGER, updated_at TEXT NOT NULL)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS admin_onboarding(admin_id INTEGER PRIMARY KEY, welcomed_at TEXT NOT NULL)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS user_status(user_id INTEGER PRIMARY KEY, verified INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0, verify_exempt INTEGER NOT NULL DEFAULT 0, verify_interval_minutes INTEGER, challenge_answer TEXT, challenge_at TEXT, verified_at TEXT, blocked_at TEXT, updated_at TEXT)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS rate_limits(user_id INTEGER PRIMARY KEY, window_started_at TEXT NOT NULL, message_count INTEGER NOT NULL DEFAULT 0)"),
    env.DB.prepare("CREATE TABLE IF NOT EXISTS verification_attempts(user_id INTEGER PRIMARY KEY, failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until TEXT, updated_at TEXT NOT NULL)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS idx_messages_user_created ON messages(user_id, created_at DESC)"),
    env.DB.prepare("CREATE INDEX IF NOT EXISTS idx_messages_admin_message ON messages(admin_message_id)"),
  ]);
}
async function upsertUser(env: Env, u: TgUser) { await env.DB.prepare("INSERT INTO users(user_id,username,first_name,last_name,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name, updated_at=excluded.updated_at").bind(u.id, u.username || "", u.first_name || "", u.last_name || "", now(), now()).run(); }
async function getUser(env: Env, uid: number) { return env.DB.prepare("SELECT * FROM users WHERE user_id=?").bind(uid).first<DbUser>(); }
async function status(env: Env, uid: number) { return env.DB.prepare("SELECT * FROM user_status WHERE user_id=?").bind(uid).first<any>(); }
async function getInterval(env: Env, uid: number) { const s = await status(env, uid); const raw = Number(s?.verify_interval_minutes ?? env.DEFAULT_VERIFY_INTERVAL_MINUTES ?? DEFAULT_VERIFY_INTERVAL_MINUTES); return Math.max(MIN_VERIFY_INTERVAL_MINUTES, Math.min(MAX_VERIFY_INTERVAL_MINUTES, raw)); }
async function lastInboundAt(env: Env, uid: number) { const row = await env.DB.prepare("SELECT created_at FROM messages WHERE user_id=? AND direction='user' ORDER BY id DESC LIMIT 1").bind(uid).first<any>(); return row?.created_at ? Date.parse(row.created_at) : null; }
async function isVerified(env: Env, uid: number) { const s = await status(env, uid); if (s?.verify_exempt) return true; if (!s?.verified) return false; const last = await lastInboundAt(env, uid); const verifiedAt = s?.verified_at ? Date.parse(String(s.verified_at).replace(" ", "T") + "Z") : NaN; const activityAt = Math.max(Number.isFinite(last) ? Number(last) : 0, Number.isFinite(verifiedAt) ? verifiedAt : 0); if (!activityAt) return true; return Date.now() - activityAt <= (await getInterval(env, uid)) * 60000; }
async function isBlocked(env: Env, uid: number) { return !!(await status(env, uid))?.blocked; }
async function consumeRateLimit(env: Env, uid: number) {
  const limit = Math.max(1, Number(env.MESSAGES_PER_MINUTE || DEFAULT_MESSAGES_PER_MINUTE));
  const row = await env.DB.prepare("SELECT window_started_at,message_count FROM rate_limits WHERE user_id=?").bind(uid).first<any>();
  const started = row?.window_started_at ? Date.parse(row.window_started_at.replace(" ", "T") + "Z") : NaN;
  if (!row || !Number.isFinite(started) || Date.now() - started >= 60000) {
    await env.DB.prepare("INSERT INTO rate_limits(user_id,window_started_at,message_count) VALUES(?,?,1) ON CONFLICT(user_id) DO UPDATE SET window_started_at=excluded.window_started_at,message_count=1").bind(uid, now()).run();
    return true;
  }
  const count = Number(row.message_count) + 1;
  await env.DB.prepare("UPDATE rate_limits SET message_count=? WHERE user_id=?").bind(count, uid).run();
  return count <= limit;
}
async function resetRateLimit(env: Env, uid: number) { await env.DB.prepare("DELETE FROM rate_limits WHERE user_id=?").bind(uid).run(); }
async function verificationLockUntil(env: Env, uid: number) { const row = await env.DB.prepare("SELECT locked_until FROM verification_attempts WHERE user_id=?").bind(uid).first<any>(); if (!row?.locked_until) return null; const lock = Date.parse(row.locked_until.replace(" ", "T") + "Z"); if (!Number.isFinite(lock) || lock <= Date.now()) { await env.DB.prepare("DELETE FROM verification_attempts WHERE user_id=?").bind(uid).run(); return null; } return lock; }
async function recordVerificationFailure(env: Env, uid: number) { const row = await env.DB.prepare("SELECT failed_attempts FROM verification_attempts WHERE user_id=?").bind(uid).first<any>(); const failures = Number(row?.failed_attempts || 0) + 1; const lock = failures >= 2 ? Date.now() + 86400000 : null; const lockText = lock ? new Date(lock).toISOString().slice(0, 19).replace("T", " ") : null; await env.DB.prepare("INSERT INTO verification_attempts(user_id,failed_attempts,locked_until,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET failed_attempts=excluded.failed_attempts,locked_until=excluded.locked_until,updated_at=excluded.updated_at").bind(uid, failures, lockText, now()).run(); if (lock) await env.DB.prepare("UPDATE user_status SET challenge_answer='',challenge_at=null,updated_at=? WHERE user_id=?").bind(now(), uid).run(); return lock; }
async function resetVerificationFailures(env: Env, uid: number) { await env.DB.prepare("DELETE FROM verification_attempts WHERE user_id=?").bind(uid).run(); }
function lockMessage(lock: number) { return `验证失败次数过多，已暂停验证24小时。可于 ${new Date(lock).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })} 后重试。`; }
function challengeExpired(s: any) { if (!s?.challenge_at) return true; const t = Date.parse(String(s.challenge_at).replace(" ", "T") + "Z"); return !Number.isFinite(t) || Date.now() - t > 120000; }
async function forceReverify(env: Env, uid: number) { await env.DB.prepare("INSERT INTO user_status(user_id,verified,blocked,challenge_answer,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET verified=0,challenge_answer='',updated_at=excluded.updated_at").bind(uid, 0, 0, "", now()).run(); }
async function setBlocked(env: Env, uid: number, blocked: boolean) { await env.DB.prepare("INSERT INTO user_status(user_id,verified,blocked,blocked_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET blocked=excluded.blocked, blocked_at=excluded.blocked_at, updated_at=excluded.updated_at").bind(uid, 0, blocked ? 1 : 0, blocked ? now() : null, now()).run(); }
async function setVerifyExempt(env: Env, uid: number, exempt: boolean) { await env.DB.prepare("INSERT INTO user_status(user_id,verified,blocked,verify_exempt,challenge_answer,challenge_at,verified_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET verify_exempt=excluded.verify_exempt, verified=case when excluded.verify_exempt=1 then 1 else verified end, challenge_answer='', updated_at=excluded.updated_at").bind(uid, exempt ? 1 : 0, 0, exempt ? 1 : 0, "", null, exempt ? now() : null, now()).run(); }
async function setInterval(env: Env, uid: number, minutes: number) { const m = Math.max(MIN_VERIFY_INTERVAL_MINUTES, Math.min(MAX_VERIFY_INTERVAL_MINUTES, minutes)); await env.DB.prepare("INSERT INTO user_status(user_id,verified,blocked,verify_interval_minutes,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET verify_interval_minutes=excluded.verify_interval_minutes, updated_at=excluded.updated_at").bind(uid, 0, 0, m, now()).run(); }
async function setSelected(env: Env, adminId: number, uid: number) { await env.DB.prepare("INSERT INTO admin_state(admin_id,selected_user_id,updated_at) VALUES(?,?,?) ON CONFLICT(admin_id) DO UPDATE SET selected_user_id=excluded.selected_user_id, updated_at=excluded.updated_at").bind(adminId, uid, now()).run(); }
async function clearSelected(env: Env, adminId: number) { await env.DB.prepare("DELETE FROM admin_state WHERE admin_id=?").bind(adminId).run(); }
async function selected(env: Env, adminId: number) { return (await env.DB.prepare("SELECT selected_user_id FROM admin_state WHERE admin_id=?").bind(adminId).first<any>())?.selected_user_id as number | undefined; }
async function saveMessage(env: Env, uid: number, direction: "user" | "admin", kind: string, text = "", file_id = "", admin_message_id?: number, user_message_id?: number) { const r: any = await env.DB.prepare("INSERT INTO messages(user_id,direction,kind,text,file_id,admin_message_id,user_message_id,created_at) VALUES(?,?,?,?,?,?,?,?) RETURNING id").bind(uid, direction, kind, text, file_id, admin_message_id ?? null, user_message_id ?? null, now()).first<any>(); return Number(r?.id); }
async function saveAdminMessageMap(env: Env, adminChatId: number, adminMessageId: number, uid: number) { await env.DB.prepare("INSERT OR REPLACE INTO admin_message_map(admin_chat_id,admin_message_id,user_id,created_at) VALUES(?,?,?,?)").bind(adminChatId, adminMessageId, uid, now()).run(); }
async function resolveAdminReply(env: Env, adminChatId: number, adminMessageId: number) { return (await env.DB.prepare("SELECT user_id FROM admin_message_map WHERE admin_chat_id=? AND admin_message_id=?").bind(adminChatId, adminMessageId).first<any>())?.user_id as number | undefined; }
async function updateAdminMessageId(env: Env, uid: number, userMid: number, adminMid: number) { await env.DB.prepare("UPDATE messages SET admin_message_id=? WHERE user_id=? AND user_message_id=?").bind(adminMid, uid, userMid).run(); }
async function previousAdminMessageIds(env: Env, adminChatId: number, uid: number) { return ((await env.DB.prepare("SELECT admin_message_id FROM admin_message_map WHERE admin_chat_id=? AND user_id=? ORDER BY created_at DESC LIMIT 20").bind(adminChatId, uid).all<any>()).results || []).map(row => Number(row.admin_message_id)); }

function challengeKeyboard(uid: number, answer: string, choices?: string[]) {
  if (!choices?.length) { const value = Number(answer), set = new Set([answer]); const nearby = [-12,-8,-5,-3,-2,-1,1,2,3,5,8,12].map(d => value + d).filter(n => n >= 0).sort(() => Math.random() - 0.5); for (const n of nearby) { set.add(String(n)); if (set.size >= 8) break; } while (set.size < 8) set.add(String(rand(Math.max(0, value - 20), value + 20))); choices = [...set].sort(() => Math.random() - 0.5); }
  const buttons = choices.map(c => ({ text: c, callback_data: `verify:${uid}:${c}` }));
  return { inline_keyboard: [buttons.slice(0, 4), buttons.slice(4)] };
}
async function createChallenge(env: Env, uid: number) {
  const type = ["add3", "sub", "mul", "mixed", "sequence", "second_max"][rand(0, 5)]; let q = ""; let ans = ""; let choices: string[] | undefined;
  if (type === "add3") { const a=rand(11,49), b=rand(11,49), c=rand(11,49); ans=String(a+b+c); q=`请计算：${a} + ${b} + ${c} = ?`; }
  else if (type === "sub") { const b=rand(12,49), result=rand(15,69), a=b+result; ans=String(result); q=`请计算：${a} − ${b} = ?`; }
  else if (type === "mul") { const a=rand(2,9), b=rand(2,9); ans=String(a*b); q=`请计算：${a} × ${b} = ?`; }
  else if (type === "mixed") { const a=rand(2,9), b=rand(2,5), c=rand(3,15); ans=String(a*b+c); q=`先乘后加：${a} × ${b} + ${c} = ?`; }
  else if (type === "sequence") { const start=rand(3,25), step=rand(3,12), nums=[0,1,2,3,4].map(i=>start+step*i); ans=String(nums[4]); q=`找规律，下一项是？ ${nums[0]}，${nums[1]}，${nums[2]}，${nums[3]}，?`; }
  else { const nums = new Set<number>(); while (nums.size < 8) nums.add(rand(10, 99)); choices = [...nums].map(String).sort(() => Math.random() - 0.5); ans = String([...nums].sort((a,b)=>b-a)[1]); q = "请点击第二大的数字"; }
  await env.DB.prepare("INSERT INTO user_status(user_id,verified,blocked,challenge_answer,challenge_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET challenge_answer=excluded.challenge_answer, challenge_at=excluded.challenge_at, updated_at=excluded.updated_at").bind(uid, 0, 0, ans, now(), now()).run();
  return { q, reply_markup: challengeKeyboard(uid, ans, choices) };
}
function selectedKeyboard(uid: number) { return { inline_keyboard: [[{ text: "✅ 当前会话 · 取消选中", callback_data: `unselect:${uid}` }]] }; }
async function markUnread(env: Env, uid: number) { await env.DB.prepare("UPDATE users SET unread_count=coalesce(unread_count,0)+1 WHERE user_id=?").bind(uid).run(); }
async function markRead(env: Env, uid: number) { await env.DB.prepare("UPDATE users SET unread_count=0 WHERE user_id=?").bind(uid).run(); }
async function unreadCount(env: Env, uid: number) { return Number((await env.DB.prepare("SELECT unread_count FROM users WHERE user_id=?").bind(uid).first<any>())?.unread_count || 0); }
function userKeyboard(uid: number, blocked: boolean, exempt: boolean, minutes: number) { return { inline_keyboard: [[{ text: "打开个人资料", url: `tg://user?id=${uid}` }, { text: "选中会话", callback_data: `select:${uid}` }], [{ text: "查看会话记录", callback_data: `history_open:${uid}:0` }, { text: blocked ? "解除屏蔽" : "屏蔽联系人", callback_data: `${blocked ? "unblock" : "block"}:${uid}` }], [{ text: `验证时间：${formatInterval(minutes)}`, callback_data: `verify_menu:${uid}` }, { text: exempt ? "取消验证豁免" : "永久豁免验证", callback_data: `${exempt ? "unexempt" : "exempt"}:${uid}` }]] }; }
function verifyTimeKeyboard(uid: number) { return { inline_keyboard: [[...VERIFY_INTERVAL_PRESETS.slice(0, 2).map(([m, l]) => ({ text: l, callback_data: `setverify:${uid}:${m}` }))], [...VERIFY_INTERVAL_PRESETS.slice(2).map(([m, l]) => ({ text: l, callback_data: `setverify:${uid}:${m}` }))], [{ text: "返回会话卡片", callback_data: `back_user:${uid}` }]] }; }
function mediaLabel(kind: string) { return ({ photo: "🖼 图片", sticker: "🎭 表情包", video: "🎬 视频", voice: "🎤 语音", audio: "🎵 音频", document: "📎 文件" } as Record<string,string>)[kind] || "📦 媒体"; }
function historyKeyboard(uid: number, offset: number, hasMore: boolean, mediaRows: DbMsg[] = []) { const rows: any[] = mediaRows.map(row => [{ text: `查看 ${mediaLabel(row.kind)} · ${formatBeijingTime(row.created_at)}`, callback_data: `media:${row.id}` }]); const nav = []; if (offset > 0) nav.push({ text: "上一页", callback_data: `history:${uid}:${Math.max(0, offset - 10)}` }); if (hasMore) nav.push({ text: "下一页", callback_data: `history:${uid}:${offset + 10}` }); if (nav.length) rows.push(nav); rows.push([{ text: "返回会话卡片", callback_data: `history_close:${uid}` }]); rows.push([{ text: "选中会话", callback_data: `select:${uid}` }, { text: "打开个人资料", url: `tg://user?id=${uid}` }]); return { inline_keyboard: rows }; }
async function cardKeyboard(env: Env, uid: number) { const s = await status(env, uid); return userKeyboard(uid, !!s?.blocked, !!s?.verify_exempt, await getInterval(env, uid)); }

async function recordPreview(env: Env, uid: number, limit = 3) {
  const user = await getUser(env, uid);
  const rows = (await env.DB.prepare("SELECT * FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?").bind(uid, limit).all<DbMsg>()).results || [];
  const title = esc(user ? displayName(user) : String(uid));
  const unread = await unreadCount(env, uid);
  const lines = [`<b>${unread ? `🔴 未处理 ${unread} 条` : "✅ 已处理"}</b>`, "<b>文本会话记录</b>", "────────────", title];
  for (const row of [...rows].reverse()) { const who = row.direction === "admin" ? "我" : "对方"; const text = row.text || (row.kind !== "text" ? row.kind : ""); lines.push(`\n<code>${esc(formatBeijingTime(row.created_at))}</code> <b>${who}</b>\n${esc((text || "非文本消息").slice(0, 700))}`); }
  return lines.join("\n").slice(0, MAX_TEXT);
}
async function renderHistory(env: Env, uid: number, offset = 0) {
  const user = await getUser(env, uid);
  const rows = (await env.DB.prepare("SELECT * FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 11 OFFSET ?").bind(uid, offset).all<DbMsg>()).results || [];
  const shown = rows.slice(0, 10), hasMore = rows.length > 10, title = esc(user ? displayName(user) : String(uid));
  const lines = ["<b>文本会话记录</b>", "────────────", title, `记录 ${shown.length ? offset + 1 : 0}/${offset + shown.length}`];
  for (const row of [...shown].reverse()) { const who = row.direction === "admin" ? "我" : "对方"; const text = row.text || (row.file_id ? mediaLabel(row.kind) : `非文本消息：${row.kind}`); lines.push(`\n<code>${esc(formatBeijingTime(row.created_at))}</code> <b>${who}</b>\n${esc(text.slice(0, 900))}`); }
  const mediaRows = [...shown].reverse().filter(row => !!row.file_id);
  return { text: lines.join("\n").slice(0, MAX_TEXT), reply_markup: historyKeyboard(uid, offset, hasMore, mediaRows) };
}
async function sendHistory(env: Env, chat: number, uid: number, offset = 0) {
  const page = await renderHistory(env, uid, offset);
  await send(env, chat, page.text, { reply_markup: page.reply_markup });
}
async function editHistory(env: Env, chat: number, messageId: number, uid: number, offset = 0) {
  const page = await renderHistory(env, uid, offset);
  await edit(env, chat, messageId, page.text, { reply_markup: page.reply_markup });
}

async function sendMediaPreview(env: Env, chat: number, row: DbMsg) {
  const map: Record<string, [string,string]> = { photo:["sendPhoto","photo"], video:["sendVideo","video"], voice:["sendVoice","voice"], audio:["sendAudio","audio"], document:["sendDocument","document"], sticker:["sendSticker","sticker"] };
  const entry = map[row.kind]; if (!entry || !row.file_id) return send(env, chat, "媒体不存在或暂不支持预览");
  const body: any = { chat_id: chat, [entry[1]]: row.file_id, reply_markup: { inline_keyboard: [[{ text: "关闭预览", callback_data: "media_close" }]] } };
  if (row.kind !== "sticker") body.caption = `${mediaLabel(row.kind)} · ${formatBeijingTime(row.created_at)}`;
  return tg(env, entry[0], body);
}
async function handleUpdate(up: TgUpdate, env: Env) { if (up.callback_query) return callback(up.callback_query, env); if (up.message) return message(up.message, env); }
async function message(m: TgMessage, env: Env) {
  // The relay is intentionally private-chat only. Ignore groups, supergroups,
  // and channels even when the bot is installed there as an administrator.
  if (m.chat.type !== "private") return;
  const u = m.from; if (!u) return;
  if (isAdmin(env, u.id)) return adminMessage(m, env);
  await upsertUser(env, u);
  const t = textOf(m);
  if (t === "/start") { if (await isBlocked(env, u.id)) return send(env, m.chat.id, "当前会话暂不可用。"); const lock = await verificationLockUntil(env, u.id); if (lock) return send(env, m.chat.id, lockMessage(lock)); if (await isVerified(env, u.id)) return send(env, m.chat.id, "你已通过验证，可以直接发送内容。"); const c = await createChallenge(env, u.id); return send(env, m.chat.id, `请先完成人机验证：${c.q}`, { reply_markup: c.reply_markup }); }
  if (await isBlocked(env, u.id)) return send(env, m.chat.id, "当前会话暂不可用。");
  const verificationLock = await verificationLockUntil(env, u.id); if (verificationLock) return send(env, m.chat.id, lockMessage(verificationLock));
  const s = await status(env, u.id);
  if (!s?.verify_exempt && !(await consumeRateLimit(env, u.id))) { await forceReverify(env, u.id); const c = await createChallenge(env, u.id); const limit = Math.max(1, Number(env.MESSAGES_PER_MINUTE || DEFAULT_MESSAGES_PER_MINUTE)); return send(env, m.chat.id, `发送过于频繁（每分钟最多 ${limit} 条），请重新完成人机验证：${c.q}`, { reply_markup: c.reply_markup }); }
  if (!(await isVerified(env, u.id))) { const c = await createChallenge(env, u.id); return send(env, m.chat.id, `请先完成人机验证：${c.q}`, { reply_markup: c.reply_markup }); }
  const kind = kindOf(m), text = textOf(m), fileId = fileIdFor(m);
  await saveMessage(env, u.id, "user", kind, text, fileId, undefined, m.message_id);
  await markUnread(env, u.id);
  const header = `<b>🔴 新消息</b>\n${esc(displayName({ ...u, user_id: u.id }))}\n\n${esc((text || kind).slice(0, 1600))}`.slice(0, MAX_TEXT);
  for (const admin of admins(env)) {
    for (const oldMid of await previousAdminMessageIds(env, admin, u.id)) await clearKeyboard(env, admin, oldMid);
    const isCurrent = (await selected(env, admin)) === u.id;
    const sent: any = await send(env, admin, header, { reply_markup: isCurrent ? selectedKeyboard(u.id) : await cardKeyboard(env, u.id) });
    const adminMid = sent?.result?.message_id;
    if (adminMid) { await updateAdminMessageId(env, u.id, m.message_id, adminMid); await saveAdminMessageMap(env, admin, adminMid, u.id); }
    if (kind !== "text") {
      const forwarded: any = await forwardMessage(env, admin, m.chat.id, m.message_id);
      const forwardedMid = forwarded?.result?.message_id;
      if (forwardedMid) await saveAdminMessageMap(env, admin, forwardedMid, u.id);
    }
  }
}
async function adminMessage(m: TgMessage, env: Env) {
  const admin = m.from!; const t = textOf(m).trim();
  if (t === "/start") {
    const result: any = await env.DB.prepare("INSERT OR IGNORE INTO admin_onboarding(admin_id,welcomed_at) VALUES(?,?)").bind(admin.id, now()).run();
    const firstUse = Number(result?.meta?.changes || 0) > 0;
    if (firstUse) return send(env, m.chat.id, "<b>🤖 管理员指令帮助</b>\n\n<b>会话管理</b>\n/sessions — 查看最近会话（/list 同义）\n/search &lt;关键词&gt; — 按 ID、@用户名或姓名搜索\n/select &lt;用户ID&gt; — 选中发送会话\n/history &lt;用户ID&gt; — 查看完整会话记录\n\n<b>屏蔽管理</b>\n/blocked — 查看所有已屏蔽联系人\n/block &lt;用户ID&gt; — 屏蔽联系人\n/unblock &lt;用户ID&gt; — 解除屏蔽\n\n<b>基础指令</b>\n/start — 查看机器人状态\n/help — 显示本帮助\n\n<b>快捷操作</b>\n• 回复用户消息卡片，可直接回复该用户\n• 也可先选中会话，再直接发送文字或媒体\n• 会话卡片可设置验证间隔、验证豁免及屏蔽状态");
    return send(env, m.chat.id, "管理员已上线。发送 /help 可随时查看使用方法。");
  }
  if (t === "/help") return send(env, m.chat.id, "<b>🤖 管理员指令帮助</b>\n\n<b>会话管理</b>\n/sessions — 查看最近会话（/list 同义）\n/search &lt;关键词&gt; — 按 ID、@用户名或姓名搜索\n/select &lt;用户ID&gt; — 选中发送会话\n/history &lt;用户ID&gt; — 查看完整会话记录\n\n<b>屏蔽管理</b>\n/blocked — 查看所有已屏蔽联系人\n/block &lt;用户ID&gt; — 屏蔽联系人\n/unblock &lt;用户ID&gt; — 解除屏蔽\n\n<b>基础指令</b>\n/start — 查看机器人状态\n/help — 显示本帮助\n\n<b>快捷操作</b>\n• 回复用户消息卡片，可直接回复该用户\n• 也可先选中会话，再直接发送文字或媒体\n• 会话卡片可设置验证间隔、验证豁免及屏蔽状态");
  if (t === "/list" || t === "/sessions") return listCmd(env, m.chat.id, admin.id, 0);
  if (t === "/blocked") return blockedCmd(env, m.chat.id, 0);
  if (t.startsWith("/search ")) return searchCmd(env, m.chat.id, t.slice(t.indexOf(" ") + 1));
  if (t.startsWith("/select ")) { const uid = Number(commandArg(t)); if (!uid) return send(env, m.chat.id, "用法：/select <用户ID>"); await setSelected(env, admin.id, uid); return send(env, m.chat.id, `已选中会话：${uid}`); }
  if (t.startsWith("/history ")) { const uid = Number(commandArg(t)); if (!uid) return send(env, m.chat.id, "用法：/history <用户ID>"); return sendHistory(env, m.chat.id, uid, 0); }
  if (t.startsWith("/block ")) { const uid = Number(commandArg(t)); if (!uid) return send(env, m.chat.id, "用法：/block <用户ID>"); await setBlocked(env, uid, true); return send(env, m.chat.id, `已屏蔽联系人：${uid}`); }
  if (t.startsWith("/unblock ")) { const uid = Number(commandArg(t)); if (!uid) return send(env, m.chat.id, "用法：/unblock <用户ID>"); await setBlocked(env, uid, false); return send(env, m.chat.id, `已解除屏蔽：${uid}`); }
  let uid: number | undefined;
  const replyMid = m.reply_to_message?.message_id;
  if (replyMid) {
    uid = await resolveAdminReply(env, m.chat.id, replyMid);
    if (!uid) return send(env, m.chat.id, "无法识别这条引用消息，请从对应会话卡片重新选择后发送。");
  } else {
    uid = await selected(env, admin.id);
  }
  if (!uid) return;
  const kind = kindOf(m), text = textOf(m), fileId = fileIdFor(m);
  try {
    const sent: any = kind === "text" ? await send(env, uid, esc(text)) : await copyMessage(env, uid, m.chat.id, m.message_id);
    const savedId = await saveMessage(env, uid, "admin", kind, text, fileId, m.message_id, sent?.result?.message_id);
    await markRead(env, uid);
    const targetUser = await getUser(env, uid);
    const targetLabel = targetUser ? displayName(targetUser) : String(uid);
    await send(env, m.chat.id, `已发送给：${esc(targetLabel)}`, { reply_markup: { inline_keyboard: [[{ text: "🗑 撤回这条消息", callback_data: `retract:${savedId}` }]] } });
  } catch (e: any) { await send(env, m.chat.id, `发送失败：${esc(String(e?.message || e))}`); }
}
async function listCmd(env: Env, chat: number, adminId: number, offset = 0, messageId?: number) {
  const rows = (await env.DB.prepare("SELECT u.*, MAX(m.created_at) last_at FROM users u LEFT JOIN messages m ON m.user_id=u.user_id GROUP BY u.user_id ORDER BY coalesce(last_at,u.updated_at) DESC LIMIT 11 OFFSET ?").bind(offset).all<any>()).results || [];
  const shown = rows.slice(0, 10), hasMore = rows.length > 10;
  if (!shown.length) return send(env, chat, "暂无会话");
  const current = await selected(env, adminId), lines = ["<b>最近会话</b>"], buttons: any[] = [];
  for (const row of shown) { const prefix = row.user_id === current ? "✅" : (row.unread_count ? "🔴" : "⚪"); lines.push(`${prefix} ${esc(displayName(row))}\n未处理：${row.unread_count || 0} · ${formatBeijingTime(row.last_at || row.updated_at)}`); buttons.push([{ text: `${prefix} ${row.first_name || row.username || row.user_id}`, callback_data: `sessions_open:${row.user_id}:${offset}` }]); }
  const nav: any[] = []; if (offset > 0) nav.push({ text: "上一页", callback_data: `sessions:${Math.max(0, offset - 10)}` }); if (hasMore) nav.push({ text: "下一页", callback_data: `sessions:${offset + 10}` }); if (nav.length) buttons.push(nav);
  const body = lines.join("\n\n").slice(0, MAX_TEXT), extra = { reply_markup: { inline_keyboard: buttons } };
  return messageId ? edit(env, chat, messageId, body, extra) : send(env, chat, body, extra);
}
async function blockedCmd(env: Env, chat: number, offset = 0) {
  const rows = (await env.DB.prepare("SELECT u.*,s.blocked_at FROM user_status s JOIN users u ON u.user_id=s.user_id WHERE s.blocked=1 ORDER BY coalesce(s.blocked_at,s.updated_at) DESC LIMIT 11 OFFSET ?").bind(offset).all<any>()).results || [];
  const shown = rows.slice(0, 10), hasMore = rows.length > 10;
  if (!shown.length) return send(env, chat, "暂无已屏蔽联系人");
  const lines = ["<b>已屏蔽联系人</b>"], buttons: any[] = [];
  for (const row of shown) { const shortName = String(row.first_name || row.username || row.user_id).slice(0, 18); lines.push(`🚫 ${esc(displayName(row))}\n屏蔽时间：${row.blocked_at ? formatBeijingTime(row.blocked_at) : "时间未知"}`); buttons.push([{ text: `查看：${shortName}`, callback_data: `session:${row.user_id}` }, { text: `⚠️ 解除：${shortName}`, callback_data: `blocked_ask:${row.user_id}:${offset}` }]); }
  const nav: any[] = []; if (offset > 0) nav.push({ text: "上一页", callback_data: `blocked_page:${Math.max(0, offset - 10)}` }); if (hasMore) nav.push({ text: "下一页", callback_data: `blocked_page:${offset + 10}` }); if (nav.length) buttons.push(nav);
  return send(env, chat, lines.join("\n\n").slice(0, MAX_TEXT), { reply_markup: { inline_keyboard: buttons } });
}
async function searchCmd(env: Env, chat: number, raw: string) {
  const q = raw.trim().replace(/^@/, ""); if (!q) return send(env, chat, "用法：/search <用户ID、用户名或姓名>"); const p = `%${q}%`;
  const rows = (await env.DB.prepare("SELECT * FROM users WHERE cast(user_id as text)=? OR username LIKE ? COLLATE NOCASE OR first_name LIKE ? COLLATE NOCASE OR last_name LIKE ? COLLATE NOCASE OR trim(first_name || ' ' || last_name) LIKE ? COLLATE NOCASE ORDER BY updated_at DESC LIMIT 20").bind(q, p, p, p, p).all<DbUser>()).results || [];
  if (!rows.length) return send(env, chat, "没有找到匹配用户"); for (const row of rows) await send(env, chat, await recordPreview(env, row.user_id, 2), { reply_markup: await cardKeyboard(env, row.user_id) });
}
async function callback(q: TgCallback, env: Env) {
  const data = q.data || "", msg = q.message;
  if (data.startsWith("verify:")) {
    const [, uidS, ans] = data.split(":", 3); const uid = Number(uidS);
    if (q.from.id !== uid) return answerCb(env, q.id, "这不是你的验证。", true);
    const s = await status(env, uid);
    const lock = await verificationLockUntil(env, uid); if (lock) { await answerCb(env, q.id, "验证已暂停", true); await del(env, msg?.chat.id, msg?.message_id); return send(env, q.from.id, lockMessage(lock)); }
    if (s?.verify_exempt) { await answerCb(env, q.id, "已豁免验证"); await del(env, msg?.chat.id, msg?.message_id); return send(env, q.from.id, "你已被管理员永久豁免验证，可以直接发送内容。"); }
    if (s && !s.blocked && !challengeExpired(s) && ans === String(s.challenge_answer || "")) { await env.DB.prepare("INSERT INTO user_status(user_id,verified,blocked,challenge_answer,challenge_at,verified_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET verified=1, challenge_answer='', challenge_at=null, verified_at=excluded.verified_at, updated_at=excluded.updated_at").bind(uid, 1, 0, "", null, now(), now()).run(); await resetRateLimit(env, uid); await resetVerificationFailures(env, uid); await answerCb(env, q.id, "验证通过"); await del(env, msg?.chat.id, msg?.message_id); return send(env, q.from.id, "验证通过。现在可以直接发送内容，我会转给管理员。"); }
    const newLock = await recordVerificationFailure(env, uid); await answerCb(env, q.id, newLock ? "验证失败，已暂停24小时。" : "验证失败，还可重试1次。", true); await del(env, msg?.chat.id, msg?.message_id); if (newLock) return send(env, q.from.id, lockMessage(newLock)); const c = await createChallenge(env, uid); return send(env, q.from.id, `验证失败，还可重试1次：${c.q}`, { reply_markup: c.reply_markup });
  }
  if (!isAdmin(env, q.from.id)) return;
  const [act, uidS, val] = data.split(":"); const uid = Number(uidS); await answerCb(env, q.id);
  if (act === "select") { await markRead(env, uid); await setSelected(env, q.from.id, uid); return tg(env, "editMessageReplyMarkup", { chat_id: msg!.chat.id, message_id: msg!.message_id, reply_markup: selectedKeyboard(uid) }); }
  if (act === "unselect") { await clearSelected(env, q.from.id); return tg(env, "editMessageReplyMarkup", { chat_id: msg!.chat.id, message_id: msg!.message_id, reply_markup: await cardKeyboard(env, uid) }); }
  if (act === "blocked_page") { if (msg?.chat.id && msg.message_id) await del(env, msg.chat.id, msg.message_id); return blockedCmd(env, msg?.chat.id || q.from.id, uid); }
  if (act === "blocked_ask") { const target = await getUser(env, uid); return send(env, msg?.chat.id || q.from.id, `⚠️ <b>确认解除屏蔽？</b>\n\n${esc(target ? displayName(target) : String(uid))}`, { reply_markup: { inline_keyboard: [[{ text: "✅ 确认解除", callback_data: `blocked_unblock:${uid}:${val || 0}` }, { text: "取消", callback_data: "blocked_cancel" }]] } }); }
  if (data === "blocked_cancel") { if (msg?.chat.id && msg.message_id) await del(env, msg.chat.id, msg.message_id); return; }
  if (act === "blocked_unblock") { await setBlocked(env, uid, false); if (msg?.chat.id && msg.message_id) await del(env, msg.chat.id, msg.message_id); return blockedCmd(env, msg?.chat.id || q.from.id, Number(val || 0)); }
  if (act === "sessions_open") { await markRead(env, uid); const keyboard = await cardKeyboard(env, uid); keyboard.inline_keyboard.push([{ text: "⬅️ 返回最近会话", callback_data: `sessions_back:${val || 0}` }]); return edit(env, msg!.chat.id, msg!.message_id, await recordPreview(env, uid, 3), { reply_markup: keyboard }); }
  if (act === "sessions_back") return listCmd(env, msg!.chat.id, q.from.id, uid, msg!.message_id);
  if (act === "sessions") return listCmd(env, msg!.chat.id, q.from.id, uid, msg!.message_id);
  if (act === "session") { await markRead(env, uid); return send(env, msg?.chat.id || q.from.id, await recordPreview(env, uid, 3), { reply_markup: await cardKeyboard(env, uid) }); }
  if (act === "history_open") {
    if (msg?.chat.id) return sendHistory(env, msg.chat.id, uid, Number(val || 0));
    return sendHistory(env, q.from.id, uid, Number(val || 0));
  }
  if (act === "retract") { const row = await env.DB.prepare("SELECT * FROM messages WHERE id=? AND direction='admin'").bind(uid).first<DbMsg>(); if (!row?.user_message_id) return answerCb(env, q.id, "记录不存在或已撤回", true); const deleted: any = await tg(env, "deleteMessage", { chat_id: row.user_id, message_id: row.user_message_id }); if (!deleted?.ok) return answerCb(env, q.id, `撤回失败：${deleted?.description || "未知错误"}`, true); await env.DB.prepare("DELETE FROM messages WHERE id=?").bind(uid).run(); return edit(env, msg!.chat.id, msg!.message_id, "✅ 消息已撤回"); }
  if (act === "media") { const row = await env.DB.prepare("SELECT * FROM messages WHERE id=?").bind(uid).first<DbMsg>(); return row ? sendMediaPreview(env, msg?.chat.id || q.from.id, row) : answerCb(env, q.id, "媒体不存在", true); }
  if (data === "media_close") { if (msg?.chat.id && msg.message_id) await del(env, msg.chat.id, msg.message_id); return; }
  if (act === "history") {
    if (msg?.chat.id && msg.message_id) return editHistory(env, msg.chat.id, msg.message_id, uid, Number(val || 0));
    return sendHistory(env, q.from.id, uid, Number(val || 0));
  }
  if (act === "history_close") { if (msg?.chat.id && msg.message_id) return del(env, msg.chat.id, msg.message_id); return; }
  if (act === "block") { await setBlocked(env, uid, true); return tg(env, "editMessageReplyMarkup", { chat_id: msg!.chat.id, message_id: msg!.message_id, reply_markup: await cardKeyboard(env, uid) }); }
  if (act === "unblock") { await setBlocked(env, uid, false); return tg(env, "editMessageReplyMarkup", { chat_id: msg!.chat.id, message_id: msg!.message_id, reply_markup: await cardKeyboard(env, uid) }); }
  if (act === "setverify") { await setInterval(env, uid, Number(val)); return tg(env, "editMessageReplyMarkup", { chat_id: msg!.chat.id, message_id: msg!.message_id, reply_markup: await cardKeyboard(env, uid) }); }
  if (act === "verify_menu") return tg(env, "editMessageReplyMarkup", { chat_id: msg!.chat.id, message_id: msg!.message_id, reply_markup: verifyTimeKeyboard(uid) });
  if (act === "back_user") return tg(env, "editMessageReplyMarkup", { chat_id: msg!.chat.id, message_id: msg!.message_id, reply_markup: await cardKeyboard(env, uid) });
  if (act === "exempt") { await setVerifyExempt(env, uid, true); return tg(env, "editMessageReplyMarkup", { chat_id: msg!.chat.id, message_id: msg!.message_id, reply_markup: await cardKeyboard(env, uid) }); }
  if (act === "unexempt") { await setVerifyExempt(env, uid, false); return tg(env, "editMessageReplyMarkup", { chat_id: msg!.chat.id, message_id: msg!.message_id, reply_markup: await cardKeyboard(env, uid) }); }
  if (act === "noop") return answerCb(env, q.id, `当前验证间隔：${formatInterval(await getInterval(env, uid))}`);
}

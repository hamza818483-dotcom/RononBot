"""
Ronon Bot — Telegram MCQ Bot
Owner-managed access, Gemini-powered /img /pdf MCQ poll generator,
per-poll tags + explanations.
"""
import os
import re
import json
import sqlite3
import logging
import asyncio
import base64
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from datetime import datetime, timedelta

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.constants import ParseMode

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("RononBot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = 7411044846  # backward-compat (primary owner)
OWNER_IDS = {7411044846, 5341425626}  # multi-owner support — সব owner id এখানে থাকবে
DB_PATH = os.environ.get("DB_PATH", "ronon.db")
DAILY_KEY_LIMIT = 20  # প্রতিটা Gemini key-এর daily request quota

# ============================================================
# DATABASE
# ============================================================

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS permitted_users (
        user_id INTEGER PRIMARY KEY,
        added_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        api_key TEXT UNIQUE,
        added_by INTEGER,
        added_at TEXT,
        active INTEGER DEFAULT 1,
        used_today INTEGER DEFAULT 0,
        usage_date TEXT DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS channels (
        channel_id TEXT PRIMARY KEY,
        channel_name TEXT,
        added_by INTEGER,
        added_at TEXT
    )""")
    # migrate: add usage columns if upgrading from an older DB
    try:
        c.execute("ALTER TABLE api_keys ADD COLUMN used_today INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE api_keys ADD COLUMN usage_date TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS exp_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        current_tag TEXT DEFAULT '',
        own_explanation TEXT DEFAULT '',
        own_explanation_on INTEGER DEFAULT 0,
        current_exp_tag TEXT DEFAULT ''
    )""")
    conn.commit()
    conn.close()


def is_permitted(user_id: int) -> bool:
    if user_id in OWNER_IDS:
        return True
    conn = db_conn()
    row = conn.execute(
        "SELECT 1 FROM permitted_users WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None


def db_permit_user(user_id: int):
    conn = db_conn()
    conn.execute(
        "INSERT OR IGNORE INTO permitted_users (user_id, added_at) VALUES (?,?)",
        (user_id, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def db_remove_user(user_id: int) -> bool:
    conn = db_conn()
    cur = conn.execute("DELETE FROM permitted_users WHERE user_id=?", (user_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def db_list_permitted():
    conn = db_conn()
    rows = conn.execute("SELECT user_id FROM permitted_users ORDER BY added_at").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


# ---- API keys ----

def db_add_key(key: str, added_by: int) -> bool:
    conn = db_conn()
    try:
        conn.execute(
            "INSERT INTO api_keys (api_key, added_by, added_at) VALUES (?,?,?)",
            (key, added_by, datetime.utcnow().isoformat())
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def db_get_active_keys():
    conn = db_conn()
    rows = conn.execute(
        "SELECT api_key FROM api_keys WHERE active=1 ORDER BY id"
    ).fetchall()
    conn.close()
    return [r["api_key"] for r in rows]


def _today_utc_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def db_get_all_keys():
    """Returns all keys with today's usage (auto-resets if usage_date is stale)."""
    conn = db_conn()
    rows = conn.execute(
        "SELECT id, api_key, active, used_today, usage_date FROM api_keys ORDER BY id"
    ).fetchall()
    conn.close()
    today = _today_utc_str()
    result = []
    for r in rows:
        used = r["used_today"] if r["usage_date"] == today else 0
        result.append({
            "id": r["id"],
            "api_key": r["api_key"],
            "active": r["active"],
            "used_today": used,
        })
    return result


def db_increment_key_usage(key: str):
    """Call this every time a key is actually used for a Gemini request."""
    conn = db_conn()
    today = _today_utc_str()
    row = conn.execute(
        "SELECT used_today, usage_date FROM api_keys WHERE api_key=?", (key,)
    ).fetchone()
    if row is None:
        conn.close()
        return
    if row["usage_date"] == today:
        new_used = row["used_today"] + 1
    else:
        new_used = 1
    conn.execute(
        "UPDATE api_keys SET used_today=?, usage_date=? WHERE api_key=?",
        (new_used, today, key)
    )
    conn.commit()
    conn.close()


def db_key_usage_today(key: str) -> int:
    conn = db_conn()
    row = conn.execute(
        "SELECT used_today, usage_date FROM api_keys WHERE api_key=?", (key,)
    ).fetchone()
    conn.close()
    if row is None:
        return 0
    return row["used_today"] if row["usage_date"] == _today_utc_str() else 0


# ---- Channels ----

def db_add_channel(channel_id: str, channel_name: str, added_by: int) -> bool:
    conn = db_conn()
    try:
        conn.execute(
            "INSERT INTO channels (channel_id, channel_name, added_by, added_at) VALUES (?,?,?,?)",
            (channel_id, channel_name, added_by, datetime.utcnow().isoformat())
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.execute(
            "UPDATE channels SET channel_name=? WHERE channel_id=?",
            (channel_name, channel_id)
        )
        conn.commit()
        return False
    finally:
        conn.close()


def db_list_channels():
    conn = db_conn()
    rows = conn.execute(
        "SELECT channel_id, channel_name FROM channels ORDER BY added_at"
    ).fetchall()
    conn.close()
    return [(r["channel_id"], r["channel_name"]) for r in rows]


def db_remove_channel(channel_id: str) -> bool:
    conn = db_conn()
    cur = conn.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


# ---- Tags ----

def db_add_tag(user_id: int, name: str):
    conn = db_conn()
    conn.execute(
        "INSERT INTO tags (user_id, name, created_at) VALUES (?,?,?)",
        (user_id, name, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def db_get_settings(user_id: int) -> dict:
    conn = db_conn()
    row = conn.execute(
        "SELECT * FROM user_settings WHERE user_id=?", (user_id,)
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO user_settings (user_id) VALUES (?)", (user_id,)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM user_settings WHERE user_id=?", (user_id,)
        ).fetchone()
    conn.close()
    return dict(row)


def db_update_settings(user_id: int, **fields):
    db_get_settings(user_id)  # ensure row exists
    conn = db_conn()
    keys = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE user_settings SET {keys} WHERE user_id=?",
        (*fields.values(), user_id)
    )
    conn.commit()
    conn.close()


def db_add_exp_tag(user_id: int, name: str):
    conn = db_conn()
    conn.execute(
        "INSERT INTO exp_tags (user_id, name, created_at) VALUES (?,?,?)",
        (user_id, name, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def db_get_exp_tags(user_id: int):
    conn = db_conn()
    rows = conn.execute(
        "SELECT name FROM exp_tags WHERE user_id=? ORDER BY created_at", (user_id,)
    ).fetchall()
    conn.close()
    return [r["name"] for r in rows]


# ============================================================
# GEMINI MCQ GENERATION
# ============================================================

MCQ_PROMPT = """তুমি একজন expert MCQ generator। এই ইমেজ/PDF page থেকে যত সম্ভব ভালো মানের MCQ (Multiple Choice Question) বানাও।

নিয়ম:
- প্রতিটা MCQ-তে 4টি option (A,B,C,D) থাকবে
- Answer অবশ্যই সঠিক হতে হবে
- ছোট explanation দিবে (1-2 লাইন)
- Source content-এর ভাষাতেই MCQ বানাবে (বাংলা হলে বাংলা, English হলে English)

Return ONLY valid JSON array, no markdown, no extra text:
[{"question":"...","options":["...","...","...","..."],"answer":"A","explanation":"..."}]
"""


async def gemini_generate_mcq(image_bytes: bytes, mime_type: str = "image/jpeg") -> tuple:
    """Try all saved Gemini keys in order. Returns (mcqs, error)."""
    keys = db_get_active_keys()
    if not keys:
        return [], "❌ কোনো Gemini API key যোগ করা নেই। /addkey দিয়ে key যোগ করুন।"

    last_err = None
    for key in keys:
        if db_key_usage_today(key) >= DAILY_KEY_LIMIT:
            continue  # এই key-এর আজকের quota শেষ, পরের key ট্রাই করো
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=key)

            def _call():
                return client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[
                        types.Part.from_text(text=MCQ_PROMPT),
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    ]
                )
            resp = await asyncio.to_thread(_call)
            db_increment_key_usage(key)
            text = resp.text or ""
            mcqs = parse_mcq_json(text)
            if mcqs:
                return mcqs, None
            last_err = "Empty/invalid response"
        except Exception as e:
            logger.warning(f"Gemini key failed: {e}")
            last_err = str(e)
            continue

    return [], f"❌ সব Gemini key ব্যর্থ হয়েছে বা আজকের quota শেষ। ({last_err})"


def parse_mcq_json(text: str) -> list:
    t = (text or "").strip()
    if t.startswith("```json"):
        t = t[7:]
    if t.startswith("```"):
        t = t[3:]
    if t.endswith("```"):
        t = t[:-3]
    t = t.strip()
    if not t.startswith("["):
        s, e = t.find("["), t.rfind("]")
        if s != -1 and e != -1 and e > s:
            t = t[s:e + 1]
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    valid = []
    letter_map = {"A": 0, "B": 1, "C": 2, "D": 3}
    for m in data:
        if not all(k in m for k in ("question", "options", "answer")):
            continue
        opts = m.get("options", [])
        if len(opts) < 4:
            continue
        opts = [str(o).strip() for o in opts[:4]]
        ans = m.get("answer", "A")
        if isinstance(ans, str):
            ans_idx = letter_map.get(ans.strip().upper()[:1], None)
            if ans_idx is None:
                continue
        elif isinstance(ans, int) and 0 <= ans <= 3:
            ans_idx = ans
        else:
            continue
        valid.append({
            "question": str(m.get("question", "")).strip(),
            "options": opts,
            "answer_index": ans_idx,
            "explanation": str(m.get("explanation", "")).strip(),
        })
    return valid


# ============================================================
# ACCESS CONTROL DECORATOR
# ============================================================

def require_permit(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not is_permitted(user_id):
            await update.message.reply_text(
                "❌ এই বট ব্যবহারের অনুমতি আপনার নেই।\nOwner-কে যোগাযোগ করুন।"
            )
            return
        return await func(update, context)
    return wrapper


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in OWNER_IDS:
            await update.message.reply_text("❌ এই command শুধু Owner ব্যবহার করতে পারবে।")
            return
        return await func(update, context)
    return wrapper


# ============================================================
# /start
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "বন্ধু"
    text = f"Welcome to Ronon Bot!প্রিয় {name}..😄\n\n"

    if user.id in OWNER_IDS:
        text += (
            "👑 <b>Owner Commands:</b>\n"
            "/permit (user id) — ইউজারকে অনুমতি দিন\n"
            "/remove (user id) — অনুমতি বাতিল করুন\n"
            "/addkey (gemini api key) — Gemini API key যোগ করুন\n"
            "/keys — সব key-এর quota status দেখুন\n"
            "/channel (id) (name) — চ্যানেল যোগ করুন\n"
            "/channellist — যোগ করা চ্যানেলের তালিকা\n"
            "/removechannel (id) — চ্যানেল সরান\n"
            "/tagQ (name) — প্রশ্নের ট্যাগ সেট করুন\n"
            "/exp — Explanation settings\n"
            "/img — ছবি থেকে MCQ বানান\n"
            "/pdf — PDF থেকে MCQ বানান\n"
        )
    else:
        text += (
            "📋 <b>Available Commands:</b>\n"
            "/tagQ (name) — প্রশ্নের ট্যাগ সেট করুন\n"
            "/exp — Explanation settings\n"
            "/img — ছবি থেকে MCQ বানান\n"
            "/pdf — PDF থেকে MCQ বানান\n"
        )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ============================================================
# /permit /remove — Owner only
# ============================================================

@owner_only
async def cmd_permit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /permit (user id)")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ সঠিক user id দিন (সংখ্যা)।")
        return
    db_permit_user(target_id)
    await update.message.reply_text(f"✅ User <code>{target_id}</code> কে অনুমতি দেওয়া হয়েছে।", parse_mode=ParseMode.HTML)


@owner_only
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /remove (user id)")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ সঠিক user id দিন (সংখ্যা)।")
        return
    ok = db_remove_user(target_id)
    if ok:
        await update.message.reply_text(f"✅ User <code>{target_id}</code> এর অনুমতি বাতিল হয়েছে।", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ এই user permitted list-এ নেই।")


# ============================================================
# /addkey — Owner only
# ============================================================

@owner_only
async def cmd_addkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /addkey (gemini api key)")
        return
    key = context.args[0].strip()
    ok = db_add_key(key, update.effective_user.id)
    if ok:
        await update.message.reply_text("✅ API key যোগ হয়েছে। এখন Gemini 2.5 Flash কাজ করবে।")
    else:
        await update.message.reply_text("⚠️ এই key আগে থেকেই আছে।")


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return key[:2] + "..." + key[-2:]
    return key[:6] + "..." + key[-4:]


def _seconds_until_utc_midnight() -> int:
    now = datetime.utcnow()
    tomorrow = datetime(now.year, now.month, now.day) + timedelta(days=1)
    return int((tomorrow - now).total_seconds())


@owner_only
async def cmd_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keys = db_get_all_keys()
    if not keys:
        await update.message.reply_text("❌ কোনো Gemini API key যোগ করা নেই। /addkey দিয়ে key যোগ করুন।")
        return

    secs = _seconds_until_utc_midnight()
    h, rem = divmod(secs, 3600)
    m, _ = divmod(rem, 60)

    total_req = len(keys) * DAILY_KEY_LIMIT
    total_used = sum(k["used_today"] for k in keys)
    total_left = total_req - total_used

    lines = ["🔑 <b>Gemini 2.5 Flash — Key Status</b>\n"]
    for i, k in enumerate(keys, 1):
        used = k["used_today"]
        left = max(DAILY_KEY_LIMIT - used, 0)
        status = "🟢 Active" if k["active"] else "🔴 Disabled"
        lines.append(
            f"{i}. <code>{_mask_key(k['api_key'])}</code> — {status}\n"
            f"    ব্যবহার হয়েছে: {used}/{DAILY_KEY_LIMIT} | বাকি: {left}"
        )

    lines.append(
        f"\n📊 <b>Total (সব key মিলিয়ে)</b>\n"
        f"মোট quota: {total_req}/day\n"
        f"ব্যবহার হয়েছে: {total_used}\n"
        f"বাকি আছে: {total_left}\n\n"
        f"⏳ Reset হবে: {h}h {m}m পরে (UTC midnight অনুযায়ী, Gemini free-tier rule মতে)"
    )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ============================================================
# /channel /channellist — Owner only, interactive button UI
# ============================================================

@owner_only
async def cmd_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Format: /channel (channel id) (channel name)")
        return
    channel_id = context.args[0].strip()
    channel_name = " ".join(context.args[1:]).strip()
    added = db_add_channel(channel_id, channel_name, update.effective_user.id)
    if added:
        await update.message.reply_text(
            f"✅ চ্যানেল যোগ হয়েছে:\nID: <code>{channel_id}</code>\nName: <b>{channel_name}</b>",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            f"⚠️ এই চ্যানেল আগে থেকেই আছে, নাম আপডেট করা হয়েছে: <b>{channel_name}</b>",
            parse_mode=ParseMode.HTML
        )


def _build_channellist_view():
    """Builds (text, keyboard) for the channel list screen."""
    channels = db_list_channels()
    if not channels:
        text = "📍 <b>কোনো চ্যানেল যোগ করা নেই।</b>"
    else:
        lines = ["📍 <b>যোগ করা চ্যানেলসমূহ:</b>\n"]
        for i, (cid, cname) in enumerate(channels, 1):
            lines.append(f"{i}. <b>{cname}</b> — <code>{cid}</code>")
        text = "\n".join(lines)

    kb = []
    for cid, cname in channels:
        label = cname if len(cname) <= 25 else cname[:22] + "..."
        kb.append([InlineKeyboardButton(f"🗑️ Delete: {label}", callback_data=f"chdel_{cid}")])
    kb.append([InlineKeyboardButton("➕ Add Channel", callback_data="chadd")])
    return text, InlineKeyboardMarkup(kb)


@owner_only
async def cmd_channellist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = _build_channellist_view()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


@owner_only
async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /removechannel (channel id)")
        return
    channel_id = context.args[0].strip()
    ok = db_remove_channel(channel_id)
    if ok:
        await update.message.reply_text(f"✅ চ্যানেল <code>{channel_id}</code> সরানো হয়েছে।", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ এই চ্যানেল লিস্টে নেই।")


async def channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the inline buttons on /channellist: delete + add."""
    query = update.callback_query
    if query.from_user.id not in OWNER_IDS:
        await query.answer("❌ শুধু Owner ব্যবহার করতে পারবে।", show_alert=True)
        return
    await query.answer()
    data = query.data

    if data == "chadd":
        context.user_data["awaiting_channel_add"] = True
        await query.message.reply_text(
            "➕ নতুন চ্যানেল যোগ করতে <b>channel id</b> এবং <b>channel name</b> স্পেস দিয়ে লিখে reply করুন।\n"
            "উদাহরণ: <code>-1001234567890 My Channel</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True)
        )
        return

    if data.startswith("chdel_"):
        channel_id = data[len("chdel_"):]
        db_remove_channel(channel_id)
        text, markup = _build_channellist_view()
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return


# ============================================================
# /tagQ — set question tag
# ============================================================

@require_permit
async def cmd_tagq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Format: /tagQ (name)")
        return
    name = " ".join(context.args).strip()
    user_id = update.effective_user.id
    db_add_tag(user_id, name)
    db_update_settings(user_id, current_tag=name)
    await update.message.reply_text(
        f"✅ Question tag সেট হয়েছে: <b>{name}</b>\nএখন থেকে প্রতিটা poll-এর প্রশ্নের ১ লাইন উপরে এই tag বসবে।",
        parse_mode=ParseMode.HTML
    )


# ============================================================
# /exp — explanation settings menu
# ============================================================

@require_permit
async def cmd_exp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("🏷️ Tag Name", callback_data="exp_tagname")],
        [InlineKeyboardButton("✍️ Own", callback_data="exp_own")],
    ]
    await update.message.reply_text(
        "📝 <b>Explanation Settings</b>\nকোনটা সেট করতে চান?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def exp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    if data == "exp_tagname":
        settings = db_get_settings(user_id)
        existing_tags = db_get_exp_tags(user_id)
        text = "🏷️ <b>Explanation Tag Name</b>\n\n"
        if existing_tags:
            text += "Saved tags:\n" + "\n".join(f"• {t}" for t in existing_tags) + "\n\n"
        current = settings.get("current_exp_tag") or "(সেট করা নেই)"
        text += f"বর্তমান: <b>{current}</b>\n\nনতুন tag লিখতে reply করুন।"
        context.user_data["awaiting_exp_tag"] = True
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=ForceReply(selective=True))

    elif data == "exp_own":
        settings = db_get_settings(user_id)
        is_on = bool(settings.get("own_explanation_on"))
        current_text = settings.get("own_explanation") or "(সেট করা নেই)"
        kb = [
            [InlineKeyboardButton(
                f"{'✅ ON' if is_on else '⬜ OFF'} — Toggle",
                callback_data="exp_own_toggle"
            )],
            [InlineKeyboardButton("✏️ Edit/Set Own Explanation", callback_data="exp_own_edit")],
        ]
        await query.message.reply_text(
            f"✍️ <b>Own Explanation</b>\n\nবর্তমান টেক্সট:\n<code>{current_text}</code>\n\n"
            f"Status: {'🟢 ON (সব poll-এ এটাই বসবে)' if is_on else '🔴 OFF'}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == "exp_own_toggle":
        settings = db_get_settings(user_id)
        new_state = 0 if settings.get("own_explanation_on") else 1
        db_update_settings(user_id, own_explanation_on=new_state)
        await query.answer(f"Own Explanation {'ON' if new_state else 'OFF'} করা হয়েছে", show_alert=True)
        kb = [
            [InlineKeyboardButton(
                f"{'✅ ON' if new_state else '⬜ OFF'} — Toggle",
                callback_data="exp_own_toggle"
            )],
            [InlineKeyboardButton("✏️ Edit/Set Own Explanation", callback_data="exp_own_edit")],
        ]
        current_text = settings.get("own_explanation") or "(সেট করা নেই)"
        await query.edit_message_text(
            f"✍️ <b>Own Explanation</b>\n\nবর্তমান টেক্সট:\n<code>{current_text}</code>\n\n"
            f"Status: {'🟢 ON (সব poll-এ এটাই বসবে)' if new_state else '🔴 OFF'}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif data == "exp_own_edit":
        context.user_data["awaiting_own_exp"] = True
        await query.message.reply_text(
            "✏️ Own explanation-এর টেক্সট লিখে reply করুন:",
            reply_markup=ForceReply(selective=True)
        )


async def handle_reply_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches ForceReply responses for exp tag / own explanation setup."""
    user_id = update.effective_user.id
    text = update.message.text or ""

    if context.user_data.get("awaiting_exp_tag"):
        context.user_data["awaiting_exp_tag"] = False
        db_add_exp_tag(user_id, text.strip())
        db_update_settings(user_id, current_exp_tag=text.strip())
        await update.message.reply_text(f"✅ Explanation tag সেট হয়েছে: <b>{text.strip()}</b>", parse_mode=ParseMode.HTML)
        return

    if context.user_data.get("awaiting_own_exp"):
        context.user_data["awaiting_own_exp"] = False
        db_update_settings(user_id, own_explanation=text.strip())
        await update.message.reply_text("✅ Own explanation সেভ হয়েছে।")
        return

    if context.user_data.get("awaiting_channel_add"):
        context.user_data["awaiting_channel_add"] = False
        parts = text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ ভুল ফরম্যাট। channel id এবং name দুটোই দিন।\nউদাহরণ: <code>-1001234567890 My Channel</code>",
                parse_mode=ParseMode.HTML
            )
            return
        channel_id, channel_name = parts[0].strip(), parts[1].strip()
        added = db_add_channel(channel_id, channel_name, user_id)
        if added:
            await update.message.reply_text(
                f"✅ চ্যানেল যোগ হয়েছে: <b>{channel_name}</b> (<code>{channel_id}</code>)",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                f"⚠️ চ্যানেল আগে থেকেই ছিল, নাম আপডেট হয়েছে: <b>{channel_name}</b>",
                parse_mode=ParseMode.HTML
            )
        chlist_text, chlist_markup = _build_channellist_view()
        await update.message.reply_text(chlist_text, parse_mode=ParseMode.HTML, reply_markup=chlist_markup)
        return


# ============================================================
# /img /pdf — generate MCQ and send as polls (no extra buttons)
# ============================================================

def build_final_explanation(user_id: int, mcq_explanation: str) -> str:
    """Applies tag/own-explanation settings to a single MCQ's explanation."""
    settings = db_get_settings(user_id)
    parts = []

    if settings.get("own_explanation_on") and settings.get("own_explanation"):
        parts.append(settings["own_explanation"])
    elif mcq_explanation:
        parts.append(mcq_explanation)

    exp_tag = settings.get("current_exp_tag")
    if exp_tag:
        parts.append(exp_tag)

    return "\n".join(p for p in parts if p).strip()[:200]  # Telegram poll explanation limit


def build_question_text(user_id: int, question: str) -> str:
    settings = db_get_settings(user_id)
    tag = settings.get("current_tag")
    if tag:
        return f"{tag}\n{question}"[:290]  # Telegram poll question limit ~300
    return question[:290]


async def send_mcqs_as_polls(update: Update, context: ContextTypes.DEFAULT_TYPE, mcqs: list):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    sent = 0
    for mcq in mcqs:
        try:
            q_text = build_question_text(user_id, mcq["question"])
            explanation = build_final_explanation(user_id, mcq.get("explanation", ""))
            await context.bot.send_poll(
                chat_id=chat_id,
                question=q_text,
                options=mcq["options"],
                type="quiz",
                correct_option_id=mcq["answer_index"],
                explanation=explanation or None,
                is_anonymous=True,
            )
            sent += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"Poll send failed: {e}")
    return sent


@require_permit
async def cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_img"] = True
    await update.message.reply_text("📷 এখন একটা ছবি পাঠান — তা থেকে MCQ poll বানানো হবে।")


@require_permit
async def cmd_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_pdf"] = True
    await update.message.reply_text("📄 এখন একটা PDF ফাইল পাঠান — তা থেকে MCQ poll বানানো হবে।")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_permitted(user_id):
        return
    if not context.user_data.get("awaiting_img"):
        return
    context.user_data["awaiting_img"] = False

    wait_msg = await update.message.reply_text("⏳ MCQ বানানো হচ্ছে...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        img_bytes = bytes(await file.download_as_bytearray())

        mcqs, error = await gemini_generate_mcq(img_bytes, "image/jpeg")
        if error or not mcqs:
            await wait_msg.edit_text(error or "❌ কোনো MCQ বানানো যায়নি।")
            return

        await wait_msg.delete()
        sent = await send_mcqs_as_polls(update, context, mcqs)
        await update.message.reply_text(f"✅ {sent}টি MCQ poll পাঠানো হয়েছে!")
    except Exception as e:
        logger.error(f"handle_photo error: {e}", exc_info=True)
        await wait_msg.edit_text(f"❌ Error: {e}")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_permitted(user_id):
        return
    if not context.user_data.get("awaiting_pdf"):
        return
    context.user_data["awaiting_pdf"] = False

    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ শুধু PDF ফাইল পাঠান।")
        return

    wait_msg = await update.message.reply_text("⏳ PDF processing হচ্ছে...")
    try:
        file = await context.bot.get_file(doc.file_id)
        pdf_bytes = bytes(await file.download_as_bytearray())

        from pdf2image import convert_from_bytes
        images = await asyncio.to_thread(convert_from_bytes, pdf_bytes, dpi=150)

        total_sent = 0
        for i, img in enumerate(images, 1):
            await wait_msg.edit_text(f"⏳ Page {i}/{len(images)} প্রসেস হচ্ছে...")
            buf = BytesIO()
            img.save(buf, format="JPEG")
            page_bytes = buf.getvalue()

            mcqs, error = await gemini_generate_mcq(page_bytes, "image/jpeg")
            if error or not mcqs:
                continue
            sent = await send_mcqs_as_polls(update, context, mcqs)
            total_sent += sent

        await wait_msg.delete()
        await update.message.reply_text(f"✅ সর্বমোট {total_sent}টি MCQ poll পাঠানো হয়েছে!")
    except Exception as e:
        logger.error(f"handle_document error: {e}", exc_info=True)
        await wait_msg.edit_text(f"❌ Error: {e}")


# ============================================================
# HEALTH SERVER — Render Web Service requires a bound $PORT.
# Bot runs in polling mode (no HTTP server otherwise), so Render would
# mark the service unhealthy/sleep it. This minimal server fixes that.
# ============================================================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # silence default access logs


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    logger.info(f"🩺 Health server listening on 0.0.0.0:{port}")
    server.serve_forever()


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN environment variable সেট করা নেই।")

    db_init()

    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("permit", cmd_permit))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("addkey", cmd_addkey))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("channel", cmd_channel))
    app.add_handler(CommandHandler("channellist", cmd_channellist))
    app.add_handler(CommandHandler("removechannel", cmd_removechannel))
    app.add_handler(CommandHandler("tagQ", cmd_tagq))
    app.add_handler(CommandHandler("exp", cmd_exp))
    app.add_handler(CommandHandler("img", cmd_img))
    app.add_handler(CommandHandler("pdf", cmd_pdf))

    app.add_handler(CallbackQueryHandler(exp_callback, pattern="^exp_"))
    app.add_handler(CallbackQueryHandler(channel_callback, pattern="^(chdel_|chadd)"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, handle_reply_text))

    logger.info("🚀 Ronon Bot starting (polling mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

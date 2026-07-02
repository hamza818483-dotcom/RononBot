
# Let me write the complete fixed bot code and save it

bot_code = r'''"""
Ronon Bot — Telegram MCQ Bot
Owner-managed access, Gemini-powered /img /pdf MCQ poll generator,
per-poll tags + explanations. Webhook mode for Render.
"""
import os
import re
import json
import sqlite3
import logging
import asyncio
import base64
import threading
import csv
import io
from datetime import datetime, timedelta
from io import BytesIO

import aiohttp
from aiohttp import web
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, BotCommand
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
OWNER_ID = 7411044846
OWNER_IDS = {7411044846, 5341425626}
DB_PATH = os.environ.get("DB_PATH", "ronon.db")
DAILY_KEY_LIMIT = 20
RENDER_URL = "https://rononbot.onrender.com"
ERROR_NOTIFY_USER = 5341425626
DEFAULT_TOPIC = "Special MCQ By Ronon"

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
        current_exp_tag TEXT DEFAULT '',
        watermark TEXT DEFAULT ''
    )""")
    # migrate watermark column
    try:
        c.execute("ALTER TABLE user_settings ADD COLUMN watermark TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
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
    db_get_settings(user_id)
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

MCQ_PROMPT_TEMPLATE = """তুমি একজন expert MCQ generator। এই ইমেজ/PDF page থেকে {count_hint} MCQ (Multiple Choice Question) বানাও।

নিয়ম:
- যদি ছবিতে আগে থেকে MCQ থাকে, সেগুলোও extract করে সঠিক format-এ দাও
- পাশাপাশি content থেকে নতুন MCQ তৈরি করো
- প্রতিটা MCQ-তে 4টি option (A,B,C,D) থাকবে
- Answer অবশ্যই সঠিক হতে হবে
- ছোট explanation দিবে (1-2 লাইন)
- Source content-এর ভাষাতেই MCQ বানাবে (বাংলা হলে বাংলা, English হলে English)

Return ONLY valid JSON array, no markdown, no extra text:
[{"question":"...","options":["...","...","...","..."],"answer":"A","explanation":"..."}]
"""


async def gemini_generate_mcq(image_bytes: bytes, mime_type: str = "image/jpeg", count: int = None) -> tuple:
    keys = db_get_active_keys()
    if not keys:
        return [], "❌ কোনো Gemini API key যোগ করা নেই। /addkey দিয়ে key যোগ করুন।"

    if count:
        count_hint = f"সঠিকভাবে {count} টি"
    else:
        count_hint = "যত সম্ভব ভালো মানের (সর্বোচ্চ সংখ্যক)"

    prompt = MCQ_PROMPT_TEMPLATE.format(count_hint=count_hint)

    last_err = None
    for key in keys:
        if db_key_usage_today(key) >= DAILY_KEY_LIMIT:
            continue
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=key)

            def _call():
                return client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[
                        types.Part.from_text(text=prompt),
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
# UTILITIES — CSV + PDF
# ============================================================

def build_final_explanation(user_id: int, mcq_explanation: str) -> str:
    settings = db_get_settings(user_id)
    parts = []
    if settings.get("own_explanation_on") and settings.get("own_explanation"):
        parts.append(settings["own_explanation"])
    elif mcq_explanation:
        parts.append(mcq_explanation)
    exp_tag = settings.get("current_exp_tag")
    if exp_tag:
        parts.append(exp_tag)
    return "\n".join(p for p in parts if p).strip()[:200]


def build_question_text(user_id: int, question: str) -> str:
    settings = db_get_settings(user_id)
    tag = settings.get("current_tag")
    if tag:
        return f"{tag}\n{question}"[:290]
    return question[:290]


def generate_csv(mcqs: list) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["question", "option1", "option2", "option3", "option4", "option5", "answer", "explanation", "type", "section"])
    for m in mcqs:
        opts = m["options"][:4] + [""] * (5 - len(m["options"]))
        ans = m["answer_index"] + 1
        writer.writerow([
            m["question"],
            opts[0], opts[1], opts[2], opts[3], opts[4],
            ans,
            m.get("explanation", ""),
            1,
            1,
        ])
    return output.getvalue().encode('utf-8-sig')


def find_unicode_font():
    """Find a system TTF font that supports Unicode/Bengali."""
    paths = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',
        '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
        '/usr/share/fonts/truetype/ttf-dejavu/DejaVuSans.ttf',
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def generate_pdf(mcqs: list, topic: str, watermark: str = "") -> bytes:
    """Generate a PDF matching the ATLAS MCQ style."""
    try:
        from fpdf import FPDF
    except ImportError:
        logger.error("fpdf2 not installed. Cannot generate PDF.")
        return b""

    class MCQPDF(FPDF):
        def header(self):
            # Watermark / Header banner
            if self.watermark_text:
                self.set_fill_color(240, 248, 255)
                self.rect(0, 0, 210, 18, 'F')
                self.set_font(self.font_name, 'B', 16)
                self.set_text_color(0, 102, 204)
                self.cell(0, 14, f"🚀 {self.watermark_text} —", 0, 1, 'C')
                self.ln(2)
            else:
                self.ln(5)

        def footer(self):
            self.set_y(-12)
            self.set_font(self.font_name, '', 9)
            self.set_text_color(128, 128, 128)
            self.cell(0, 10, f'Page {self.page_no()} | {topic}', 0, 0, 'C')

    pdf = MCQPDF()
    pdf.font_name = 'Arial'
    pdf.watermark_text = watermark

    font_path = find_unicode_font()
    if font_path:
        try:
            pdf.add_font('DejaVu', '', font_path, uni=True)
            bold_path = font_path.replace('Sans.ttf', 'Sans-Bold.ttf')
            if not os.path.exists(bold_path):
                bold_path = font_path
            pdf.add_font('DejaVu', 'B', bold_path, uni=True)
            pdf.font_name = 'DejaVu'
        except Exception as e:
            logger.warning(f"Could not add Unicode font: {e}")

    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # Topic title
    pdf.set_font(pdf.font_name, 'B', 18)
    pdf.set_text_color(33, 37, 41)
    pdf.cell(0, 10, topic, 0, 1, 'C')
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(6)

    for i, mcq in enumerate(mcqs, 1):
        start_y = pdf.get_y()
        if start_y > 250:
            pdf.add_page()
            start_y = pdf.get_y()

        card_h = 52
        # Check if explanation makes card taller
        expl = mcq.get("explanation", "")
        if expl:
            card_h += 14

        # Card border
        pdf.set_draw_color(200, 200, 200)
        pdf.set_line_width(0.3)
        pdf.rect(12, start_y, 186, card_h, 'D')

        # Question number + text
        pdf.set_xy(15, start_y + 3)
        pdf.set_font(pdf.font_name, 'B', 13)
        pdf.set_text_color(0, 0, 0)
        q_text = f"{i:02d}.  {mcq['question']}"
        pdf.multi_cell(180, 7, q_text, 0, 'L')

        pdf.ln(1)
        current_y = pdf.get_y()

        # Options
        pdf.set_font(pdf.font_name, '', 12)
        for j, opt in enumerate(mcq['options']):
            letter = chr(65 + j)
            is_correct = (j == mcq['answer_index'])

            if is_correct:
                # Green highlight for correct answer
                pdf.set_fill_color(212, 237, 218)
                pdf.set_text_color(21, 87, 36)
                pdf.set_draw_color(40, 167, 69)
                pdf.rect(18, current_y - 1, 170, 7, 'DF')
                pdf.set_xy(18, current_y)
                pdf.cell(170, 6, f"({letter})  {opt}   ✓", 0, 1, 'L')
                pdf.set_text_color(0, 0, 0)
                pdf.set_draw_color(200, 200, 200)
            else:
                pdf.set_xy(18, current_y)
                pdf.cell(170, 6, f"({letter})  {opt}", 0, 1, 'L')
            current_y = pdf.get_y()

        # Explanation box
        if expl:
            pdf.set_fill_color(230, 240, 255)
            pdf.set_draw_color(0, 102, 204)
            pdf.set_text_color(0, 51, 102)
            pdf.set_font(pdf.font_name, '', 11)
            pdf.rect(18, current_y + 1, 170, 12, 'DF')
            pdf.set_xy(20, current_y + 2)
            pdf.multi_cell(166, 5, f"ব্যাখ্যা: {expl}", 0, 'L')
            pdf.set_text_color(0, 0, 0)
            pdf.set_draw_color(200, 200, 200)

        pdf.ln(8)

    return bytes(pdf.output(dest='S'))


def parse_page_range(range_str: str, total_pages: int) -> list:
    if not range_str:
        return list(range(1, total_pages + 1))
    pages = set()
    for part in range_str.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-")
                start, end = int(start), int(end)
                for p in range(start, end + 1):
                    if 1 <= p <= total_pages:
                        pages.add(p)
            except ValueError:
                continue
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    pages.add(p)
            except ValueError:
                continue
    return sorted(pages) if pages else list(range(1, total_pages + 1))


async def send_mcqs_as_polls(context: ContextTypes.DEFAULT_TYPE, user_id: int, mcqs: list, chat_id: int) -> int:
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


# ============================================================
# ACCESS CONTROL
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
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    try:
        error_text = f"❌ <b>Bot Error:</b>\n<code>{str(context.error)}</code>\n\nUpdate: {update}"
        await context.bot.send_message(chat_id=ERROR_NOTIFY_USER, text=error_text[:4096], parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ============================================================
# COMMANDS
# ============================================================

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! Bot is online.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "বন্ধু"
    text = f"Welcome to Ronon Bot! প্রিয় {name}..😄\n\n"

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
            "/wm — Watermark সেট করুন\n"
            "/ping — Bot status চেক করুন\n"
        )
    else:
        text += (
            "📋 <b>Available Commands:</b>\n"
            "/tagQ (name) — প্রশ্নের ট্যাগ সেট করুন\n"
            "/exp — Explanation settings\n"
            "/img — ছবি থেকে MCQ বানান\n"
            "/pdf — PDF থেকে MCQ বানান\n"
            "/wm — Watermark সেট করুন\n"
            "/ping — Bot status চেক করুন\n"
        )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


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


@require_permit
async def cmd_wm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        settings = db_get_settings(update.effective_user.id)
        current = settings.get("watermark") or "(সেট করা নেই)"
        await update.message.reply_text(
            f"🎨 <b>Current Watermark:</b> <code>{current}</code>\n\n"
            f"নতুন watermark সেট করতে: <code>/wm Your Watermark Name</code>",
            parse_mode=ParseMode.HTML
        )
        return
    name = " ".join(context.args).strip()
    db_update_settings(update.effective_user.id, watermark=name)
    await update.message.reply_text(
        f"✅ Watermark সেট হয়েছে: <b>{name}</b>\nএখন থেকে সব generated PDF-তে এই watermark থাকবে।",
        parse_mode=ParseMode.HTML
    )


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
# /img — Reply-based (new) + Old awaiting mode
# ============================================================

@require_permit
async def cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # NEW: reply-based immediate processing
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        topic = " ".join(context.args).strip() if context.args else DEFAULT_TOPIC
        photo = update.message.reply_to_message.photo[-1]

        wait_msg = await update.message.reply_text("⏳ ছবি থেকে MCQ generate হচ্ছে...")
        try:
            file = await context.bot.get_file(photo.file_id)
            img_bytes = bytes(await file.download_as_bytearray())

            mcqs, error = await gemini_generate_mcq(img_bytes, "image/jpeg")
            if error or not mcqs:
                await wait_msg.edit_text(error or "❌ কোনো MCQ বানানো যায়নি।")
                return

            context.user_data["img_mcqs"] = mcqs
            context.user_data["img_topic"] = topic
            context.user_data["img_user_id"] = update.effective_user.id

            await wait_msg.delete()

            channels = db_list_channels()
            kb = []
            for cid, cname in channels:
                kb.append([InlineKeyboardButton(f"📢 {cname}", callback_data=f"imgch_{cid}")])
            kb.append([InlineKeyboardButton("📄 CSV + PDF", callback_data="img_csv_pdf")])

            await update.message.reply_text(
                f"✅ <b>{len(mcqs)}</b>টি MCQ তৈরি হয়েছে!\n"
                f"🎯 Topic: <b>{topic}</b>\n\n"
                f"কোথায় পাঠাবেন?",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except Exception as e:
            logger.error(f"cmd_img reply error: {e}", exc_info=True)
            await wait_msg.edit_text(f"❌ Error: {e}")
        return

    # OLD: set awaiting mode
    context.user_data["awaiting_img"] = True
    await update.message.reply_text("📷 এখন একটা ছবি পাঠান — তা থেকে MCQ poll বানানো হবে।")


async def img_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if not is_permitted(user_id):
        await query.answer("❌ অনুমতি নেই।", show_alert=True)
        return

    mcqs = context.user_data.get("img_mcqs", [])
    topic = context.user_data.get("img_topic", DEFAULT_TOPIC)

    if not mcqs:
        await query.edit_message_text("❌ ডেটা মেয়াদ উত্তীর্ণ। আবার চেষ্টা করুন।")
        return

    if data == "img_csv_pdf":
        settings = db_get_settings(user_id)
        watermark = settings.get("watermark") or ""

        csv_bytes = generate_csv(mcqs)
        csv_buffer = io.BytesIO(csv_bytes)
        csv_buffer.name = f"MCQ_{topic.replace(' ', '_')}.csv"

        pdf_bytes = generate_pdf(mcqs, topic, watermark)

        await query.edit_message_text("📄 CSV + PDF তৈরি হচ্ছে...")

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=csv_buffer,
            filename=f"MCQ_{topic.replace(' ', '_')}.csv",
            caption=f"📄 <b>{topic}</b> — MCQ CSV File\nমোট: {len(mcqs)}টি",
            parse_mode=ParseMode.HTML
        )

        if pdf_bytes:
            pdf_buffer = io.BytesIO(pdf_bytes)
            pdf_buffer.name = f"MCQ_{topic.replace(' ', '_')}.pdf"
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_buffer,
                filename=f"MCQ_{topic.replace(' ', '_')}.pdf",
                caption=f"📑 <b>{topic}</b> — MCQ PDF File\nমোট: {len(mcqs)}টি",
                parse_mode=ParseMode.HTML
            )
        return

    if data.startswith("imgch_"):
        channel_id = data[len("imgch_"):]

        pre_text = f"🎯 <b>{topic}</b>\n📊 MCQ Polls Starting...\nমোট প্রশ্ন: {len(mcqs)}"
        try:
            await context.bot.send_message(chat_id=channel_id, text=pre_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            await query.edit_message_text(f"❌ চ্যানেলে পাঠাতে ব্যর্থ: {e}")
            return

        await query.edit_message_text(f"⏳ 📢 চ্যানেলে {len(mcqs)}টি poll পাঠানো হচ্ছে...")

        sent = await send_mcqs_as_polls(context, user_id, mcqs, channel_id)

        end_text = f"✅ MCQ Polls Completed!\n📊 Total: {sent} polls\n🏷️ Topic: {topic}"
        await context.bot.send_message(chat_id=channel_id, text=end_text, parse_mode=ParseMode.HTML)

        await query.edit_message_text(f"✅ {sent}টি poll চ্যানেলে পাঠানো হয়েছে!")
        return


# ============================================================
# OLD Photo handler (backward compat)
# ============================================================

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
        sent = await send_mcqs_as_polls(context, user_id, mcqs, update.effective_chat.id)
        await update.message.reply_text(f"✅ {sent}টি MCQ poll পাঠানো হয়েছে!")
    except Exception as e:
        logger.error(f"handle_photo error: {e}", exc_info=True)
        await wait_msg.edit_text(f"❌ Error: {e}")


# ============================================================
# /pdf — Reply-based (new) + Old awaiting mode
# ============================================================

@require_permit
async def cmd_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # NEW: reply-based immediate processing
    if update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
        if doc.file_name.lower().endswith(".pdf"):
            text = update.message.text or ""
            args = context.args

            page_range = None
            channel_id = None
            topic = DEFAULT_TOPIC
            per_page_count = None

            i = 0
            while i < len(args):
                if args[i] == "-p" and i + 1 < len(args):
                    page_range = args[i + 1]
                    i += 2
                elif args[i] == "-c" and i + 1 < len(args):
                    channel_id = args[i + 1]
                    i += 2
                elif args[i] == "-m" and i + 1 < len(args):
                    topic = args[i + 1].strip('"\'')
                    i += 2
                elif args[i] == "-t" and i + 1 < len(args):
                    # -t also sets topic (alternative to -m for groups)
                    topic = args[i + 1].strip('"\'')
                    i += 2
                else:
                    i += 1

            bracket_match = re.search(r'\[(\d+)\]', text)
            if bracket_match:
                per_page_count = int(bracket_match.group(1))

            context.user_data["pdf_doc"] = doc
            context.user_data["pdf_topic"] = topic
            context.user_data["pdf_page_range"] = page_range
            context.user_data["pdf_per_page"] = per_page_count
            context.user_data["pdf_user_id"] = update.effective_user.id

            if channel_id:
                await process_pdf(update, context, channel_id)
            else:
                channels = db_list_channels()
                if not channels:
                    await update.message.reply_text("❌ কোনো চ্যানেল যোগ করা নেই। /channel দিয়ে যোগ করুন।")
                    return

                kb = []
                for cid, cname in channels:
                    kb.append([InlineKeyboardButton(f"📢 {cname}", callback_data=f"pdfch_{cid}")])
                kb.append([InlineKeyboardButton("📄 CSV + PDF", callback_data="pdf_csv_pdf")])
                await update.message.reply_text(
                    f"📄 PDF: <b>{doc.file_name}</b>\n"
                    f"🎯 Topic: <b>{topic}</b>\n"
                    f"📄 Page Range: <b>{page_range or 'All'}</b>\n"
                    f"🎯 Per Page MCQ: <b>{per_page_count or 'Highest Possible'}</b>\n\n"
                    f"কোথায় পাঠাবেন?",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(kb)
                )
            return

    # OLD: set awaiting mode
    context.user_data["awaiting_pdf"] = True
    await update.message.reply_text("📄 এখন একটা PDF ফাইল পাঠান — তা থেকে MCQ poll বানানো হবে।")


async def pdf_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if not is_permitted(user_id):
        await query.answer("❌ অনুমতি নেই।", show_alert=True)
        return

    if data.startswith("pdfch_"):
        channel_id = data[len("pdfch_"):]
        await process_pdf(update, context, channel_id, status_message=query.message)
        return

    if data == "pdf_csv_pdf":
        mcqs = context.user_data.get("pdf_mcqs", [])
        topic = context.user_data.get("pdf_topic", DEFAULT_TOPIC)
        if not mcqs:
            await query.edit_message_text("❌ ডেটা মেয়াদ উত্তীর্ণ।")
            return

        settings = db_get_settings(user_id)
        watermark = settings.get("watermark") or ""

        csv_bytes = generate_csv(mcqs)
        csv_buffer = io.BytesIO(csv_bytes)
        csv_buffer.name = f"MCQ_{topic.replace(' ', '_')}.csv"

        pdf_bytes = generate_pdf(mcqs, topic, watermark)

        await query.edit_message_text("📄 CSV + PDF তৈরি হচ্ছে...")

        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=csv_buffer,
            filename=f"MCQ_{topic.replace(' ', '_')}.csv",
            caption=f"📄 <b>{topic}</b> — MCQ CSV File\nমোট: {len(mcqs)}টি",
            parse_mode=ParseMode.HTML
        )

        if pdf_bytes:
            pdf_buffer = io.BytesIO(pdf_bytes)
            pdf_buffer.name = f"MCQ_{topic.replace(' ', '_')}.pdf"
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_buffer,
                filename=f"MCQ_{topic.replace(' ', '_')}.pdf",
                caption=f"📑 <b>{topic}</b> — MCQ PDF File\nমোট: {len(mcqs)}টি",
                parse_mode=ParseMode.HTML
            )
        return


async def process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str, status_message=None):
    doc = context.user_data.get("pdf_doc")
    topic = context.user_data.get("pdf_topic", DEFAULT_TOPIC)
    page_range = context.user_data.get("pdf_page_range")
    per_page = context.user_data.get("pdf_per_page")
    user_id = context.user_data.get("pdf_user_id", update.effective_user.id)

    if not doc:
        text = "❌ ডেটা মেয়াদ উত্তীর্ণ।"
        if status_message:
            await status_message.edit_text(text)
        else:
            await update.message.reply_text(text)
        return

    if not status_message:
        status_message = await update.message.reply_text("⏳ PDF processing হচ্ছে...")

    try:
        file = await context.bot.get_file(doc.file_id)
        pdf_bytes = bytes(await file.download_as_bytearray())

        from pdf2image import convert_from_bytes, pdfinfo_from_bytes

        pdf_info = await asyncio.to_thread(pdfinfo_from_bytes, pdf_bytes)
        total_pages = int(pdf_info["Pages"])

        pages_to_process = parse_page_range(page_range, total_pages)

        if not pages_to_process:
            await status_message.edit_text("❌ কোনো পেজ সিলেক্ট করা যায়নি।")
            return

        min_page = min(pages_to_process)
        max_page = max(pages_to_process)

        images = await asyncio.to_thread(
            convert_from_bytes, pdf_bytes, dpi=150,
            first_page=min_page, last_page=max_page
        )

        page_images = {}
        for idx, img in enumerate(images):
            actual_page = min_page + idx
            if actual_page in pages_to_process:
                page_images[actual_page] = img

        pre_text = (f"🎯 <b>{topic}</b>\n📄 PDF MCQ Polls Starting...\n"
                    f"📄 Pages: {', '.join(str(p) for p in pages_to_process)}\n"
                    f"মোট পেজ: {len(page_images)}")
        await context.bot.send_message(chat_id=channel_id, text=pre_text, parse_mode=ParseMode.HTML)

        total_sent = 0
        total_mcqs = 0
        all_mcqs = []

        for page_num in sorted(page_images.keys()):
            img = page_images[page_num]

            buf = BytesIO()
            img.save(buf, format="JPEG")
            buf.seek(0)
            await context.bot.send_photo(chat_id=channel_id, photo=buf, caption=f"📄 Page {page_num}")

            page_bytes = buf.getvalue()
            mcqs, error = await gemini_generate_mcq(page_bytes, "image/jpeg", per_page)
            if error or not mcqs:
                continue

            all_mcqs.extend(mcqs)
            total_mcqs += len(mcqs)
            sent = await send_mcqs_as_polls(context, user_id, mcqs, channel_id)
            total_sent += sent

            await status_message.edit_text(f"⏳ Page {page_num} complete... Total polls: {total_sent}")

        end_text = f"✅ PDF MCQ Polls Completed!\n📊 Total Polls: {total_sent}\n🏷️ Topic: {topic}"
        await context.bot.send_message(chat_id=channel_id, text=end_text, parse_mode=ParseMode.HTML)

        # Store all mcqs for CSV+PDF generation
        context.user_data["pdf_mcqs"] = all_mcqs
        context.user_data["pdf_topic"] = topic

        await status_message.edit_text(
            f"✅ সর্বমোট {total_sent}টি MCQ poll চ্যানেলে পাঠানো হয়েছে!\n"
            f"📄 {len(page_images)}টি পেজ প্রসেস করা হয়েছে।"
        )

    except Exception as e:
        logger.error(f"process_pdf error: {e}", exc_info=True)
        await status_message.edit_text(f"❌ Error: {e}")


# ============================================================
# OLD Document handler (backward compat)
# ============================================================

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
            sent = await send_mcqs_as_polls(context, user_id, mcqs, update.effective_chat.id)
            total_sent += sent

        await wait_msg.delete()
        await update.message.reply_text(f"✅ সর্বমোট {total_sent}টি MCQ poll পাঠানো হয়েছে!")
    except Exception as e:
        logger.error(f"handle_document error: {e}", exc_info=True)
        await wait_msg.edit_text(f"❌ Error: {e}")


# ============================================================
# MAIN — Webhook Mode
# ============================================================

async def keep_alive():
    """Internal cron to keep Render service awake."""
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{RENDER_URL}/health", timeout=10) as resp:
                    pass
        except Exception:
            pass
        await asyncio.sleep(300)


def main():
    global ptb_app
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN environment variable সেট করা নেই।")

    db_init()

    ptb_app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    ptb_app.add_handler(CommandHandler("start", cmd_start))
    ptb_app.add_handler(CommandHandler("permit", cmd_permit))
    ptb_app.add_handler(CommandHandler("remove", cmd_remove))
    ptb_app.add_handler(CommandHandler("addkey", cmd_addkey))
    ptb_app.add_handler(CommandHandler("keys", cmd_keys))
    ptb_app.add_handler(CommandHandler("channel", cmd_channel))
    ptb_app.add_handler(CommandHandler("channellist", cmd_channellist))
    ptb_app.add_handler(CommandHandler("removechannel", cmd_removechannel))
    ptb_app.add_handler(CommandHandler("tagQ", cmd_tagq))
    ptb_app.add_handler(CommandHandler("exp", cmd_exp))
    ptb_app.add_handler(CommandHandler("img", cmd_img))
    ptb_app.add_handler(CommandHandler("pdf", cmd_pdf))
    ptb_app.add_handler(CommandHandler("wm", cmd_wm))
    ptb_app.add_handler(CommandHandler("ping", cmd_ping))

    ptb_app.add_handler(CallbackQueryHandler(exp_callback, pattern="^exp_"))
    ptb_app.add_handler(CallbackQueryHandler(channel_callback, pattern="^(chdel_|chadd)"))
    ptb_app.add_handler(CallbackQueryHandler(img_callback, pattern="^img_"))
    ptb_app.add_handler(CallbackQueryHandler(pdf_callback, pattern="^pdfch_|^pdf_csv"))
    ptb_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    ptb_app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    ptb_app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, handle_reply_text))

    ptb_app.add_error_handler(error_handler)

    # Webhook + Health server
    async def health_handler(request):
        return web.Response(text="OK")

    async def webhook_handler(request):
        try:
            data = await request.json()
            update = Update.de_json(data, ptb_app.bot)
            await ptb_app.process_update(update)
            return web.Response(text="OK")
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return web.Response(text="Error", status=500)

    async def on_startup(aio_app):
        await ptb_app.initialize()
        await ptb_app.start()
        await ptb_app.bot.set_webhook(url=f"{RENDER_URL}/webhook")

        # Set bot command menu (lowercase only, wrapped in try-except)
        try:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("permit", "Permit user (owner)"),
                BotCommand("remove", "Remove user (owner)"),
                BotCommand("addkey", "Add Gemini key (owner)"),
                BotCommand("keys", "Key status (owner)"),
                BotCommand("channel", "Add channel (owner)"),
                BotCommand("channellist", "List channels (owner)"),
                BotCommand("removechannel", "Remove channel (owner)"),
                BotCommand("tagq", "Set question tag"),
                BotCommand("exp", "Explanation settings"),
                BotCommand("img", "MCQ from image"),
                BotCommand("pdf", "MCQ from PDF"),
                BotCommand("wm", "Set PDF watermark"),
                BotCommand("ping", "Check bot status"),
            ]
            await ptb_app.bot.set_my_commands(commands)
        except Exception as e:
            logger.warning(f"set_my_commands failed: {e}")

        asyncio.create_task(keep_alive())
        logger.info("🚀 Ronon Bot started in webhook mode")

    async def on_shutdown(aio_app):
        await ptb_app.stop()
        await ptb_app.shutdown()

    web_app = web.Application()
    web_app.router.add_get('/health', health_handler)
    web_app.router.add_post('/webhook', webhook_handler)
    web_app.on_startup.append(on_startup)
    web_app.on_shutdown.append(on_shutdown)

    port = int(os.environ.get("PORT", 10000))
    logger.info(f"🩺 Webhook server listening on 0.0.0.0:{port}")
    web.run_app(web_app, host='0.0.0.0', port=port)


if __name__ == "__main__":
    main()
'''

# Save to file
with open('/mnt/agents/output/bot.py', 'w', encoding='utf-8') as f:
    f.write(bot_code)

new_lines = bot_code.count('\n') + 1
print(f"Saved bot.py with {new_lines} lines")
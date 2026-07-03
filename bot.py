"""
Ronon Bot — Telegram MCQ Bot
Owner-managed access, Gemini-powered /img /pdf MCQ poll generator,
per-poll tags + explanations. Webhook mode for Render.
"""
import os
import html
import re
import json
import sqlite3
import logging
import asyncio
import base64
import csv
import io
import time
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

# Limit concurrent chromium processes — protects 512MB Render RAM from spikes
_CHROMIUM_SEMAPHORE = asyncio.Semaphore(1)
OWNER_ID = int(os.environ.get("OWNER_ID", "7411044846"))
OWNER_IDS = {int(x) for x in os.environ.get("OWNER_IDS", "7411044846,5341425626").split(",") if x.strip()}
ERROR_NOTIFY_USER = int(os.environ.get("ERROR_NOTIFY_USER", "5341425626"))
DB_PATH = os.environ.get("DB_PATH", "ronon.db")
DAILY_KEY_LIMIT = 20
RENDER_URL = "https://rononbot.onrender.com"
DEFAULT_TOPIC = "Special MCQ By Ronon"

# ============================================================
# DATABASE (Supabase — persists across Render restarts, unlike SQLite
# which lived on the ephemeral container disk and got wiped on every
# redeploy/restart on the free tier. Uses the same SUPABASE_URL/KEY as
# QuizBot's Render env, with a ronon_ table prefix so nothing collides.)
# ============================================================
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
sb = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# Local sqlite kept ONLY as an emergency fallback cache if Supabase env vars
# are missing (so the bot doesn't crash outright) — but the source of truth
# is Supabase whenever it's configured.
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    if sb:
        logger.info("[DB] Using Supabase for persistent storage (ronon_* tables)")
        return
    logger.warning("[DB] SUPABASE_URL/SUPABASE_KEY not set — falling back to ephemeral SQLite (data WILL be lost on restart)")
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
    # /pdf processing session progress — QuizBot-এর pdf_sessions টেবিলের মতোই।
    # ক্র্যাশ/restart হলে কোন session কতদূর প্রসেস হয়েছিল তা track রাখার জন্য
    # (এখন শুধু persistence/visibility purpose-এ, auto-resume এখনো implement করা হয়নি)।
    c.execute("""CREATE TABLE IF NOT EXISTS pdf_sessions (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        user_name TEXT,
        topic TEXT,
        channel_id TEXT,
        total_pages INTEGER,
        processed_pages INTEGER DEFAULT 0,
        status TEXT DEFAULT 'processing',
        created_at TEXT
    )""")
    conn.commit()
    conn.close()


def gen_session_id() -> str:
    import random, string
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))


def db_save_session(session_id: str, data: dict):
    """QuizBot-এর db_save_session-এর মতো — /pdf processing শুরু হওয়ার সময় session তৈরি করে,
    যাতে চলমান progress কোথাও persist থাকে (ক্র্যাশ/restart হলেও দেখা যাবে কতদূর হয়েছিল)।"""
    try:
        if sb:
            sb.table("ronon_pdf_sessions").upsert({"id": session_id, **data}).execute()
            return
        conn = db_conn()
        c = conn.cursor()
        c.execute("""INSERT OR REPLACE INTO pdf_sessions
            (id, user_id, user_name, topic, channel_id, total_pages, processed_pages, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, data.get("user_id"), data.get("user_name"), data.get("topic"),
             data.get("channel_id"), data.get("total_pages"), data.get("processed_pages", 0),
             data.get("status", "processing"), datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[DB] save_session error: {e}")


def db_update_session_progress(session_id: str, processed_pages: int, status: str = None):
    """প্রতিটা page শেষ হওয়ার পর progress আপডেট করে — bulk generate লুপের ভেতর থেকে বারবার কল হয়।"""
    try:
        fields = {"processed_pages": processed_pages}
        if status:
            fields["status"] = status
        if sb:
            sb.table("ronon_pdf_sessions").update(fields).eq("id", session_id).execute()
            return
        conn = db_conn()
        c = conn.cursor()
        if status:
            c.execute("UPDATE pdf_sessions SET processed_pages=?, status=? WHERE id=?",
                      (processed_pages, status, session_id))
        else:
            c.execute("UPDATE pdf_sessions SET processed_pages=? WHERE id=?",
                      (processed_pages, session_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[DB] update_session_progress error: {e}")


def is_permitted(user_id: int) -> bool:
    if user_id in OWNER_IDS:
        return True
    if sb:
        r = sb.table("ronon_permitted_users").select("user_id").eq("user_id", user_id).execute()
        return len(r.data) > 0
    conn = db_conn()
    row = conn.execute("SELECT 1 FROM permitted_users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def db_permit_user(user_id: int):
    if sb:
        sb.table("ronon_permitted_users").upsert({
            "user_id": user_id, "added_at": datetime.utcnow().isoformat()
        }).execute()
        return
    conn = db_conn()
    conn.execute(
        "INSERT OR IGNORE INTO permitted_users (user_id, added_at) VALUES (?,?)",
        (user_id, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def db_remove_user(user_id: int) -> bool:
    if sb:
        r = sb.table("ronon_permitted_users").delete().eq("user_id", user_id).execute()
        return len(r.data) > 0
    conn = db_conn()
    cur = conn.execute("DELETE FROM permitted_users WHERE user_id=?", (user_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def db_list_permitted():
    if sb:
        r = sb.table("ronon_permitted_users").select("user_id").order("added_at").execute()
        return [row["user_id"] for row in r.data]
    conn = db_conn()
    rows = conn.execute("SELECT user_id FROM permitted_users ORDER BY added_at").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def db_add_key(key: str, added_by: int) -> tuple:
    """Returns (success: bool, reason: str) — reason lets caller show real error."""
    if sb:
        try:
            existing = sb.table("ronon_api_keys").select("id").eq("api_key", key).execute()
            if existing.data:
                return False, "duplicate"
            sb.table("ronon_api_keys").insert({
                "api_key": key, "active": 1, "used_today": 0, "usage_date": "",
                "provider": "gemini"
            }).execute()
            return True, ""
        except Exception as e:
            logger.error(f"[DB] db_add_key failed: {e}")
            return False, str(e)
    conn = db_conn()
    try:
        conn.execute(
            "INSERT INTO api_keys (api_key, added_by, added_at) VALUES (?,?,?)",
            (key, added_by, datetime.utcnow().isoformat())
        )
        conn.commit()
        return True, ""
    except sqlite3.IntegrityError:
        return False, "duplicate"
    except Exception as e:
        logger.error(f"[DB] db_add_key sqlite failed: {e}")
        return False, str(e)
    finally:
        conn.close()


def db_get_active_keys():
    try:
        if sb:
            r = sb.table("ronon_api_keys").select("api_key").eq("active", 1).order("id").execute()
            return [row["api_key"] for row in r.data]
        conn = db_conn()
        rows = conn.execute("SELECT api_key FROM api_keys WHERE active=1 ORDER BY id").fetchall()
        conn.close()
        return [r["api_key"] for r in rows]
    except Exception as e:
        # আগে try/except ছিল না — ronon_api_keys টেবিল না থাকলে বা Supabase error হলে
        # এটা raw exception throw করতো, যেটা /pdf ও /img দুটোতেই MCQ generation-এর
        # সবচেয়ে গুরুত্বপূর্ণ ধাপে (key বাছাই) crash করাতো, silently।
        logger.error(f"[DB] get_active_keys error: {e}")
        return []


def _today_utc_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def db_get_all_keys():
    today = _today_utc_str()
    try:
        if sb:
            r = sb.table("ronon_api_keys").select("id,api_key,active,used_today,usage_date").order("id").execute()
            result = []
            for row in r.data:
                used = int(row["used_today"] or 0) if row["usage_date"] == today else 0
                result.append({"id": row["id"], "api_key": row["api_key"], "active": row["active"], "used_today": used})
            return result
        conn = db_conn()
        rows = conn.execute("SELECT id, api_key, active, used_today, usage_date FROM api_keys ORDER BY id").fetchall()
        conn.close()
        result = []
        for r in rows:
            used = int(r["used_today"] or 0) if r["usage_date"] == today else 0
            result.append({"id": r["id"], "api_key": r["api_key"], "active": r["active"], "used_today": used})
        return result
    except Exception as e:
        logger.error(f"[DB] get_all_keys error: {e}")
        return []


def db_increment_key_usage(key: str):
    today = _today_utc_str()
    try:
        if sb:
            r = sb.table("ronon_api_keys").select("used_today,usage_date").eq("api_key", key).execute()
            if not r.data:
                return
            row = r.data[0]
            new_used = (int(row["used_today"] or 0) + 1) if row["usage_date"] == today else 1
            sb.table("ronon_api_keys").update({"used_today": new_used, "usage_date": today}).eq("api_key", key).execute()
            return
        conn = db_conn()
        row = conn.execute("SELECT used_today, usage_date FROM api_keys WHERE api_key=?", (key,)).fetchone()
        if row is None:
            conn.close()
            return
        new_used = (int(row["used_today"] or 0) + 1) if row["usage_date"] == today else 1
        conn.execute("UPDATE api_keys SET used_today=?, usage_date=? WHERE api_key=?", (new_used, today, key))
        conn.commit()
        conn.close()
    except Exception as e:
        # usage-count আপডেট fail হলেও MCQ generation নিজে যেন থেমে না যায় — শুধু log রাখা হচ্ছে,
        # কারণ এই ফাংশন MCQ পাঠানোর পরে কল হয়, তাই raise করলে ইতিমধ্যে-সফল কাজটাও ভেঙে যেত
        logger.error(f"[DB] increment_key_usage error: {e}")


def db_key_usage_today(key: str) -> int:
    try:
        if sb:
            r = sb.table("ronon_api_keys").select("used_today,usage_date").eq("api_key", key).execute()
            if not r.data:
                return 0
            row = r.data[0]
            return int(row["used_today"] or 0) if row["usage_date"] == _today_utc_str() else 0
        conn = db_conn()
        row = conn.execute("SELECT used_today, usage_date FROM api_keys WHERE api_key=?", (key,)).fetchone()
        conn.close()
        if row is None:
            return 0
        return int(row["used_today"] or 0) if row["usage_date"] == _today_utc_str() else 0
    except Exception as e:
        # error হলে 0 ধরে নেওয়া নিরাপদ (key limit-এ পৌঁছায়নি ধরে নিয়ে ব্যবহার চালিয়ে যাওয়া হয়) —
        # crash হয়ে পুরো MCQ generation থামিয়ে দেওয়ার চেয়ে এটা ভালো ট্রেড-অফ
        logger.error(f"[DB] key_usage_today error: {e}")
        return 0


def db_add_channel(channel_id: str, channel_name: str, added_by: int) -> bool:
    if sb:
        existing = sb.table("ronon_channels").select("channel_id").eq("channel_id", channel_id).execute()
        if existing.data:
            sb.table("ronon_channels").update({"channel_name": channel_name}).eq("channel_id", channel_id).execute()
            return False
        sb.table("ronon_channels").insert({
            "channel_id": channel_id, "channel_name": channel_name,
            "added_by": added_by, "added_at": datetime.utcnow().isoformat()
        }).execute()
        return True
    conn = db_conn()
    try:
        conn.execute(
            "INSERT INTO channels (channel_id, channel_name, added_by, added_at) VALUES (?,?,?,?)",
            (channel_id, channel_name, added_by, datetime.utcnow().isoformat())
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.execute("UPDATE channels SET channel_name=? WHERE channel_id=?", (channel_name, channel_id))
        conn.commit()
        return False
    finally:
        conn.close()


def db_list_channels():
    try:
        if sb:
            r = sb.table("ronon_channels").select("channel_id,channel_name").order("added_at").execute()
            return [(row["channel_id"], row["channel_name"]) for row in r.data]
        conn = db_conn()
        rows = conn.execute("SELECT channel_id, channel_name FROM channels ORDER BY added_at").fetchall()
        conn.close()
        return [(r["channel_id"], r["channel_name"]) for r in rows]
    except Exception as e:
        # আগে এখানে কোনো try/except ছিল না — Supabase-এ ronon_channels টেবিল না থাকলে বা
        # কোনো connectivity issue হলে এটা raw exception throw করতো, যেটা /pdf-এর caller-এ
        # গিয়ে পুরো command-কে silently থামিয়ে দিতো (শুধু owner DM-এ alert যেত, ইউজার কিছুই দেখতো না)
        logger.error(f"[DB] list_channels error: {e}")
        return []


def db_remove_channel(channel_id: str) -> bool:
    if sb:
        r = sb.table("ronon_channels").delete().eq("channel_id", channel_id).execute()
        return len(r.data) > 0
    conn = db_conn()
    cur = conn.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed


def db_add_tag(user_id: int, name: str):
    if sb:
        sb.table("ronon_tags").insert({
            "user_id": user_id, "name": name, "created_at": datetime.utcnow().isoformat()
        }).execute()
        return
    conn = db_conn()
    conn.execute(
        "INSERT INTO tags (user_id, name, created_at) VALUES (?,?,?)",
        (user_id, name, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def db_get_settings(user_id: int) -> dict:
    defaults = {"user_id": user_id, "current_tag": "", "own_explanation": "",
                "own_explanation_on": 0, "current_exp_tag": "", "watermark": ""}
    try:
        if sb:
            r = sb.table("ronon_user_settings").select("*").eq("user_id", user_id).execute()
            if not r.data:
                sb.table("ronon_user_settings").insert(defaults).execute()
                return defaults
            return r.data[0]
        conn = db_conn()
        row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        return dict(row)
    except Exception as e:
        logger.error(f"[DB] get_settings error: {e}")
        return defaults


def db_update_settings(user_id: int, **fields):
    try:
        db_get_settings(user_id)
        if sb:
            sb.table("ronon_user_settings").update(fields).eq("user_id", user_id).execute()
            return
        conn = db_conn()
        keys = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE user_settings SET {keys} WHERE user_id=?", (*fields.values(), user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[DB] update_settings error: {e}")


def db_add_exp_tag(user_id: int, name: str):
    if sb:
        sb.table("ronon_exp_tags").insert({
            "user_id": user_id, "name": name, "created_at": datetime.utcnow().isoformat()
        }).execute()
        return
    conn = db_conn()
    conn.execute(
        "INSERT INTO exp_tags (user_id, name, created_at) VALUES (?,?,?)",
        (user_id, name, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def db_get_exp_tags(user_id: int):
    if sb:
        r = sb.table("ronon_exp_tags").select("name").eq("user_id", user_id).order("created_at").execute()
        return [row["name"] for row in r.data]
    conn = db_conn()
    rows = conn.execute("SELECT name FROM exp_tags WHERE user_id=? ORDER BY created_at", (user_id,)).fetchall()
    conn.close()
    return [r["name"] for r in rows]


# ============================================================
# GEMINI MCQ GENERATION
# ============================================================

MCQ_PROMPT_WITH_COUNT = """📝 Special MCQ TYPE: Standard Easy

🟥Overall Instructions:
-Image এ আগে থেকে MCQ বানানো থাকুক বা Information থাকুক,সকল জায়গা থেকেই প্রশ্ন বানাবে
-কোনো টেক্সটের নিচে কালার মার্ক বা কোনো টেক্সট হাইলাইটেড থাকলে সেখান থেকে প্রশ্ন বানানো মিস দেওয়া যাবে না (must priority)
-কোয়ালিটিফুল প্রশ্ন বানাতে হবে
-ছক থাকলে স্পেশাল প্রায়োরিটি পাবে (Use Every Information for Making MCQ)
-টপিকের নাম,অধ্যায়ের নাম,হেডলাইন,পেইজ সংখ্যা,সেকশনের নাম,"Card 1"/"Card 2" এর মতো navigation/label টেক্সট এসব থেকে MCQ বানাবে না — না প্রশ্নে, না অপশনে। এগুলো শুধু structural/navigation elements, প্রকৃত জ্ঞান/তথ্য না।
-প্রতিটি অপশন অবশ্যই actual factual content হতে হবে (definition, cause, treatment, value, name of a real concept ইত্যাদি) — কখনোই কোনো section heading, card/page label, বা navigation text কোনো option হিসেবে ব্যবহার করা যাবে না
-MUST বানাতে হবে exactly {count} টি MCQ, কম বেশি নয়
-Highest quality MCQ বানাবে

🌐 LANGUAGE RULE (STRICT — MUST FOLLOW):
-Source image-এর মূল ভাষা যা থাকবে (Bengali বা English), Question + Options + Explanation সবকিছু সেই একই ভাষায় লিখতে হবে
-Source ইংরেজি হলে পুরো MCQ ইংরেজিতে লিখবে — বাংলায় translate করা সম্পূর্ণ নিষেধ
-Source বাংলা হলে পুরো MCQ বাংলায় লিখবে — ইংরেজিতে translate করা সম্পূর্ণ নিষেধ
-Mixed-language source হলে, যে অংশ থেকে প্রশ্ন বানাচ্ছো সেই অংশের ভাষা অনুসরণ করবে

💥প্রশ্ন: (ছোট, ১/১.৫/২ লাইন)
💥অপশন: (৪টি, ছোট+মিক্সড সোর্স থেকে)
-অপশনে সঠিক উত্তর অবশ্যই একটিই থাকবে
-৪টি অপশনই তথ্য দ্বারা পরিপূর্ণ থাকবে। হ্যাঁ,না,সত্য,মিথ্যা থাকবে না
💥উত্তর: A/B/C/D — MUST be distributed across different options. STRICTLY FORBIDDEN: all answers being "A" or same option. Each MCQ's correct answer MUST be placed at a different position (A, B, C, or D) — vary them naturally across questions.
💥ব্যাখ্যা: max 200 chars, source-এর ভাষায় (উপরের LANGUAGE RULE অনুযায়ী)

🟨 উদ্দীপক RULE (শুধু বাংলা উদ্দীপকযুক্ত প্রশ্নে):
-বাংলা উদ্দীপক/passage থাকলে (যেখানে নিচে একাধিক MCQ থাকে), সেই উদ্দীপক টেক্সট "uddipok" ফিল্ডে বসাবে; একই উদ্দীপকের সব MCQ-তে হুবহু একই টেক্সট থাকবে যাতে গ্রুপ করা যায়
-উদ্দীপক ছাড়া সাধারণ MCQ-তে "uddipok" ফিল্ড "" রাখবে

Topic: {topic}
Page: {page}

MUST Return ONLY valid JSON array, no markdown:
[{{"question":"...","options":["option1","option2","option3","option4"],"answer":"B","explanation":"...","uddipok":""}}]"""

MCQ_PROMPT_MAX = """📝 Special MCQ TYPE: Standard Easy

🟥Overall Instructions:
-Image এ আগে থেকে MCQ বানানো থাকুক বা Information থাকুক,সকল জায়গা থেকেই প্রশ্ন বানাবে
-কোনো টেক্সটের নিচে কালার মার্ক বা কোনো টেক্সট হাইলাইটেড থাকলে সেখান থেকে প্রশ্ন বানানো মিস দেওয়া যাবে না (must priority)
-কোয়ালিটিফুল প্রশ্ন বানাতে হবে
-এমনভাবে সকল প্রশ্ন বানাবে যাতে সকল লাইন থেকে MCQ কিভাবে আসতে পারে আইডিয়া হয়ে যাবে
-ছক থাকলে স্পেশাল প্রায়োরিটি পাবে (Use Every Information for Making MCQ)
-টপিকের নাম,অধ্যায়ের নাম,হেডলাইন,পেইজ সংখ্যা,সেকশনের নাম,"Card 1"/"Card 2" এর মতো navigation/label টেক্সট এসব থেকে MCQ বানাবে না — না প্রশ্নে, না অপশনে। এগুলো শুধু structural/navigation elements, প্রকৃত জ্ঞান/তথ্য না।
-প্রতিটি অপশন অবশ্যই actual factual content হতে হবে (definition, cause, treatment, value, name of a real concept ইত্যাদি) — কখনোই কোনো section heading, card/page label, বা navigation text কোনো option হিসেবে ব্যবহার করা যাবে না
-হাবিজাবি MCQ বানানো যাবে না,বেশি প্রশ্ন বানানোর প্রয়োজনে একটি MCQ কেই ঘুরিয়ে ফিরিয়ে দেওয়া যেতে পারে
-MAXIMUM possible MCQ বানাবে — প্রতিটি লাইন, বক্স, তথ্য, সোর্স use করে
-তথ্য কম থাকলে minimum 10 টি

🌐 LANGUAGE RULE (STRICT — MUST FOLLOW):
-Source image-এর মূল ভাষা যা থাকবে (Bengali বা English), Question + Options + Explanation সবকিছু সেই একই ভাষায় লিখতে হবে
-Source ইংরেজি হলে পুরো MCQ ইংরেজিতে লিখবে — বাংলায় translate করা সম্পূর্ণ নিষেধ
-Source বাংলা হলে পুরো MCQ বাংলায় লিখবে — ইংরেজিতে translate করা সম্পূর্ণ নিষেধ
-Mixed-language source হলে, যে অংশ থেকে প্রশ্ন বানাচ্ছো সেই অংশের ভাষা অনুসরণ করবে

💥প্রশ্ন: (ছোট, ১/১.৫/২ লাইন)
-সোর্স থেকে সকল টাইপের প্রশ্ন
-যতভাবে প্রশ্ন আসতে পারে সব বানাবে
💥অপশন: (৪টি, ছোট+20% বড়, মিক্সড সোর্স)
-অপশনে সঠিক উত্তর একটিই
-৪টি অপশনই তথ্য দ্বারা পরিপূর্ণ। হ্যাঁ,না,সত্য,মিথ্যা থাকবে না
💥উত্তর: A/B/C/D — MUST be distributed across different options. STRICTLY FORBIDDEN: all answers being "A" or same option. Each MCQ's correct answer MUST be placed at a different position — vary them naturally so answers are spread across A, B, C, D positions.
💥ব্যাখ্যা: max 200 chars, source-এর ভাষায় (উপরের LANGUAGE RULE অনুযায়ী)

🟨 উদ্দীপক RULE (শুধু বাংলা উদ্দীপকযুক্ত প্রশ্নে):
-বাংলা উদ্দীপক/passage থাকলে (যেখানে নিচে একাধিক MCQ থাকে), সেই উদ্দীপক টেক্সট "uddipok" ফিল্ডে বসাবে; একই উদ্দীপকের সব MCQ-তে হুবহু একই টেক্সট থাকবে যাতে গ্রুপ করা যায়
-উদ্দীপক ছাড়া সাধারণ MCQ-তে "uddipok" ফিল্ড "" রাখবে

Topic: {topic}
Page: {page}

MUST Return ONLY valid JSON array, no markdown:
[{{"question":"...","options":["option1","option2","option3","option4"],"answer":"C","explanation":"...","uddipok":""}}]"""


# ============================================================
# EXISTING MCQ EXTRACTION PROMPT (STRICT — NO NEW MCQ GENERATION)
# ============================================================
# এই prompt শুধুমাত্র page-এ আগে থেকে readymade বানানো MCQ (question + options +
# answer/ব্যাখ্যা সহ) থাকলে সেগুলো হুবহু extract করার জন্য। কখনোই নতুন MCQ বানাবে না।
MCQ_PROMPT_EXISTING_ONLY = """📝 Special MCQ TYPE: EXISTING EXTRACTION ONLY (STRICT MODE)

🟥🟥🟥 ABSOLUTE CRITICAL RULE — বার বার পড়ো, কখনো ভাঙবে না 🟥🟥🟥
-তুমি এখানে EXTRACTOR, GENERATOR/CREATOR না।
-এই page-এ যদি আগে থেকেই readymade বানানো MCQ (প্রশ্ন + ৪টি option + সঠিক উত্তর) থাকে, শুধুমাত্র সেগুলোই হুবহু তুলে আনবে।
-তুমি কক্ষনো নিজে থেকে নতুন কোনো MCQ বানাবে না, কোনো তথ্য/লাইন/প্যারাগ্রাফ থেকে নতুন প্রশ্ন তৈরি করবে না — এমনকি page-এ MCQ বানানোর মতো ভালো তথ্য থাকলেও না।
-যদি এই page-এ কোনো readymade MCQ না থাকে (শুধু প্লেইন টেক্সট/তথ্য/প্যারাগ্রাফ থাকে, কোনো "প্রশ্ন+option+উত্তর" স্ট্রাকচার নাই), তাহলে অবশ্যই empty JSON array [] রিটার্ন করবে। কোনো MCQ বানিয়ে দিবে না।
-প্রতিটি readymade MCQ-এর জন্য অবশ্যই সঠিক উত্তর (answer) থাকতে হবে explanation/answer key অংশে বা প্রশ্নের নিচে/পাশে চিহ্নিত করা থাকতে হবে। উত্তর কোথাও খুঁজে না পেলে সেই MCQ স্কিপ করবে, নিজে অনুমান করে উত্তর বসাবে না।
-এই page-এ থাকা প্রতিটি readymade MCQ MUST তুলে আনতে হবে — একটাও miss/skip করা যাবে না। খুব ছোট, অস্পষ্ট, বা কোণায় থাকা MCQ-ও বাদ দেওয়া যাবে না।
-কোনো নির্দিষ্ট সংখ্যক MCQ বানানোর/নেওয়ার লিমিট নাই — page-এ যতগুলো readymade MCQ থাকে, ALL/সবগুলোই extract করতে হবে, কোনো একটাও বাদ দিয়ে অল্প কিছু দেওয়া চলবে না।
-MCQ-এর প্রশ্ন, ৪টি option, এবং ব্যাখ্যা (যদি থাকে) হুবহু সোর্সের টেক্সট অনুযায়ী রাখবে — rewrite/paraphrase/summarize করবে না, শুধু accurately extract করবে।
-টপিকের নাম,অধ্যায়ের নাম,হেডলাইন,পেইজ সংখ্যা,সেকশনের নাম,"Card 1"/"Card 2" এর মতো navigation/label টেক্সট কখনো MCQ হিসেবে extract করবে না।

🌐 LANGUAGE RULE (STRICT — MUST FOLLOW):
-Source MCQ যে ভাষায় লেখা (Bengali বা English), হুবহু সেই ভাষায় রাখবে — কোনো translate করা সম্পূর্ণ নিষেধ

💥প্রশ্ন: সোর্সে যেভাবে লেখা হুবহু সেভাবে (rewrite করবে না)
💥অপশন: সোর্সের ৪টি option হুবহু, prefix (A)/B)/ক./ইত্যাদি) ছাড়া
💥উত্তর: source-এ যেটা সঠিক হিসেবে মার্ক করা/দেওয়া আছে ঠিক সেটাই — নিজে অনুমান করবে না
💥ব্যাখ্যা: source-এ থাকলে হুবহু (max 200 chars), না থাকলে "" রাখবে

🟨 উদ্দীপক RULE (শুধু বাংলা উদ্দীপকযুক্ত প্রশ্নে):
-বাংলা উদ্দীপক/passage থাকলে, সেই উদ্দীপক টেক্সট "uddipok" ফিল্ডে বসাবে; একই উদ্দীপকের সব MCQ-তে হুবহু একই টেক্সট থাকবে
-উদ্দীপক না থাকলে "uddipok" ফিল্ড "" রাখবে

🟪🟪🟪 INTERNAL SELF-VERIFICATION PROCESS (MUST FOLLOW — answer দেওয়ার আগে নিজে নিজে করবে) 🟪🟪🟪
তুমি একবার scan করেই থেমে যাবে না। Final answer দেওয়ার আগে অবশ্যই এই ধাপগুলো নিজে নিজে (internally, output-এ না দেখিয়ে) করবে:
-Pass 1: পুরো page টা top থেকে bottom, left থেকে right স্ক্যান করে সব readymade MCQ-এর একটা draft লিস্ট বানাও — কোনায়, ছোট ফন্টে, বা বক্সের বাইরে থাকা MCQ সহ।
-Pass 2: আবার পুরো page টা নতুন করে স্ক্যান করো, এবার শুধু checking mindset নিয়ে — Pass 1-এ কোনো MCQ কি বাদ পড়ে গেছে? বিশেষ করে page-এর একদম উপরে/নিচে, কোণায়, অথবা অন্য MCQ-এর সাথে খুব কাছাকাছি/মিশে থাকা MCQ থাকলে সেগুলো দ্বিতীয়বার খুঁজে বের করো।
-Pass 3: Draft লিস্টের প্রতিটা MCQ-এর জন্য verify করো যে সঠিক উত্তরটা সত্যিই সোর্স থেকে নেওয়া হয়েছে (অনুমান না), এবং প্রশ্ন+৪টা option হুবহু সোর্স টেক্সট মেলে কিনা।
-শুধুমাত্র এই ৩টা internal pass শেষ হওয়ার পরেই final JSON output দিবে। যদি Pass 2-এ নতুন কোনো MCQ পাওয়া যায় যেটা Pass 1-এ ছিল না, সেটাও অবশ্যই final লিস্টে যোগ করতে হবে।

Topic: {topic}
Page: {page}

MUST Return ONLY valid JSON array, no markdown. যদি এই page-এ কোনো readymade MCQ না থাকে, ঠিক এভাবে রিটার্ন করবে: []
[{{"question":"...","options":["option1","option2","option3","option4"],"answer":"A","explanation":"...","uddipok":""}}]"""


async def gemini_generate_mcq(image_bytes: bytes, mime_type: str = "image/jpeg", count: int = None,
                               topic: str = None, page: int = None, existing_only: bool = False) -> tuple:
    keys = db_get_active_keys()
    if not keys:
        return [], "❌ কোনো Gemini API key যোগ করা নেই। /addkey দিয়ে key যোগ করুন।"

    topic_str = topic or DEFAULT_TOPIC
    page_str = str(page).zfill(2) if page else "01"

    if existing_only:
        # Existing MCQ mode: শুধু page-এ readymade থাকা MCQ extract করবে, নতুন বানাবে না।
        # Gemini প্রথমবার সব MCQ ধরতে পারে না অনেক সময় (partial miss — page-এ ১০টা থাকলে ৭টা
        # দিয়ে দেয়, যেটা success response হিসেবে দেখতে খালি চোখে ঠিকই মনে হয়)। তাই এই mode-এ
        # কখনো এক attempt-এর উপর ভরসা করা হয় না — সবসময় একাধিক independent attempt চালিয়ে
        # সব attempt-এর result একসাথে merge (union) করে দেওয়া হয়, যাতে কোনো একটা attempt-এ
        # miss হওয়া MCQ অন্য attempt-এ ধরা পড়লে সেটাও ফাইনাল লিস্টে চলে আসে।
        prompt = MCQ_PROMPT_EXISTING_ONLY.format(topic=topic_str, page=page_str)
        return await _extract_existing_mcqs_merged(prompt, image_bytes, mime_type, keys, page)
    elif count:
        prompt = MCQ_PROMPT_WITH_COUNT.format(count=count, topic=topic_str, page=page_str)
        max_attempts = 1
    else:
        prompt = MCQ_PROMPT_MAX.format(topic=topic_str, page=page_str)
        max_attempts = 1

    last_err = None

    def _pick_keys():
        return [k for k in keys if db_key_usage_today(k) < DAILY_KEY_LIMIT]

    for attempt in range(1, max_attempts + 1):
        usable_keys = _pick_keys()
        if not usable_keys:
            return [], f"❌ সব Gemini key-এর আজকের quota শেষ। ({last_err})"

        for key in usable_keys:
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
                logger.warning(f"Gemini key failed (attempt {attempt}/{max_attempts}): {e}")
                last_err = str(e)
                continue

    return [], f"❌ সব Gemini key ব্যর্থ হয়েছে বা আজকের quota শেষ। ({last_err})"


def _normalize_q_for_dedup(question: str) -> str:
    """Whitespace/punctuation normalize করে দুইটা attempt-এর একই MCQ-কে duplicate হিসেবে ধরার জন্য."""
    q = re.sub(r'\s+', ' ', (question or '').strip().lower())
    q = re.sub(r'[^\w\u0980-\u09FF ]+', '', q)
    return q


async def _extract_existing_mcqs_merged(prompt: str, image_bytes: bytes, mime_type: str, keys: list, page: int) -> tuple:
    """
    Existing MCQ mode-এর জন্য: N টা independent Gemini attempt চালিয়ে সব attempt-এর
    রেজাল্ট union/merge করে ফেরত দেয় — কোনো একটা attempt কম MCQ দিলেও (partial miss)
    অন্য attempt-এ সেই MCQ ধরা পড়লে সেটা ফাইনাল লিস্টে থাকবে। একই MCQ (question টেক্সট
    মিললে) duplicate হিসেবে বাদ দেওয়া হয়।
    """
    NUM_ATTEMPTS = 2  # প্রতি page-এ 2 টা independent extraction pass (প্রতিটা call নিজেই internally
                      # multi-pass self-verify করে, prompt দেখো), তারপর দুই call-এর result union
    last_err = None
    merged: dict = {}  # normalized_question -> mcq dict
    any_success = False

    def _pick_keys():
        return [k for k in keys if db_key_usage_today(k) < DAILY_KEY_LIMIT]

    for attempt in range(1, NUM_ATTEMPTS + 1):
        usable_keys = _pick_keys()
        if not usable_keys:
            if any_success:
                break  # quota শেষ কিন্তু আগের attempt(গুলো) থেকে কিছু পাওয়া গেছে — সেগুলো দিয়ে এগোই
            return [], f"NO_EXISTING_MCQ::সব Gemini key-এর quota শেষ ({last_err})"

        got_this_attempt = False
        for key in usable_keys:
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
                cleaned = text.strip()
                if cleaned.startswith("```"):
                    cleaned = cleaned.strip("`").replace("json", "", 1).strip()
                is_syntactically_valid_json = False
                try:
                    parsed_raw = json.loads(cleaned) if cleaned else []
                    is_syntactically_valid_json = isinstance(parsed_raw, list)
                except (json.JSONDecodeError, ValueError):
                    is_syntactically_valid_json = False

                if mcqs or is_syntactically_valid_json:
                    any_success = True
                    got_this_attempt = True
                    for m in mcqs:
                        key_q = _normalize_q_for_dedup(m.get("question", ""))
                        if not key_q:
                            continue
                        if key_q not in merged:
                            merged[key_q] = m
                    break  # এই attempt-এর জন্য key খোঁজা শেষ, পরের attempt-এ যাও
                last_err = "Unparseable response"
            except Exception as e:
                logger.warning(f"[existing_only] Gemini key failed (attempt {attempt}/{NUM_ATTEMPTS}, page {page}): {e}")
                last_err = str(e)
                continue

        if not got_this_attempt:
            logger.info(f"[existing_only] Page {page}: attempt {attempt} produced nothing usable ({last_err})")

    if merged:
        return list(merged.values()), None

    if any_success:
        # সবগুলো attempt সফলভাবে চলেছে এবং প্রতিটাই [] দিয়েছে — মানে সত্যিই এই page-এ
        # কোনো readymade MCQ নেই, এটা genuine empty, error না
        return [], f"NO_EXISTING_MCQ::page-এ কোনো readymade MCQ পাওয়া যায়নি (confirmed by {NUM_ATTEMPTS} attempts)"

    return [], f"NO_EXISTING_MCQ::{last_err or 'no readymade MCQ found on this page'}"


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
    idx_to_letter = {0: "A", 1: "B", 2: "C", 3: "D"}
    nav_label_re = re.compile(r'^(card|page|section|chapter|part|topic|slide)\s*\d*$', re.IGNORECASE)
    for m in data:
        try:
            if not isinstance(m, dict):
                continue
            if not all(k in m for k in ("question", "options", "answer", "explanation")):
                continue
            opts = m.get("options", [])
            if not isinstance(opts, list) or len(opts) != 4:
                continue
            opts = [str(o).strip() for o in opts]
            # Reject MCQs where an option leaked page/section navigation text
            # (e.g. "Card 1", "Section 2") instead of real factual content.
            if any(nav_label_re.match(o) for o in opts):
                continue
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
                "uddipok": str(m.get("uddipok", "")).strip(),
            })
        except Exception as e:
            logger.warning(f"parse_mcq_json: skipped malformed item: {e}")
            continue

    # If every MCQ's correct answer landed on the same option letter (a known
    # Gemini bias), redistribute answers evenly across A/B/C/D by swapping
    # the correct option into a rotating slot per item.
    if valid:
        answer_indices = [v["answer_index"] for v in valid]
        if len(set(answer_indices)) == 1:
            for i, v in enumerate(valid):
                new_idx = i % 4
                old_idx = v["answer_index"]
                opts = v["options"][:]
                opts[old_idx], opts[new_idx] = opts[new_idx], opts[old_idx]
                v["options"] = opts
                v["answer_index"] = new_idx

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
        return f"{tag}\n\n{question}"[:290]
    return question[:290]


def generate_csv(mcqs: list) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["question", "option1", "option2", "option3", "option4", "option5", "answer", "explanation", "type", "section"])
    for m in mcqs:
        try:
            opts_raw = m.get("options", [])
            opts = opts_raw[:4] + [""] * (5 - len(opts_raw))
            ans = m.get("answer_index", 0) + 1
            writer.writerow([
                m.get("question", ""),
                opts[0], opts[1], opts[2], opts[3], opts[4],
                ans,
                m.get("explanation", ""),
                1,
                1,
            ])
        except Exception as e:
            logger.warning(f"generate_csv: skipped malformed item: {e}")
            continue
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
    """Generate a PDF matching the RONON MCQ style."""
    try:
        from fpdf import FPDF
    except ImportError:
        logger.error("fpdf2 not installed. Cannot generate PDF.")
        return b""

    class MCQPDF(FPDF):
        def __init__(self, watermark_text="", *args, **kwargs):
            self.watermark_text = watermark_text
            super().__init__(*args, **kwargs)

        def header(self):
            # Diagonal background watermark (rotated, light, behind content)
            if self.watermark_text:
                self.set_font(self.font_name, 'B', 46)
                self.set_text_color(230, 230, 230)
                with self.rotation(45, x=105, y=148):
                    self.text(35, 155, self.watermark_text)
                self.set_text_color(0, 0, 0)

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

    pdf = MCQPDF(watermark_text=watermark)
    pdf.font_name = 'Arial'

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


async def _animate_generation_progress(msg, start_time: float, est_total: float = 18.0):
    """Gemini generation চলাকালীন smooth % + ETA বার দেখানোর জন্য background animation।
    Actual completion time অজানা তাই একটা estimated-total (18s) এর ভিত্তিতে % বাড়ে,
    কিন্তু কখনো 95%-এর বেশি যায় না — আসল রেজাল্ট এলেই caller সেটা 100%-এ নিয়ে যাবে।"""
    bar_len = 10
    try:
        while True:
            await asyncio.sleep(2.0)
            elapsed = time.monotonic() - start_time
            pct = min(95, int((elapsed / est_total) * 100))
            filled = int(bar_len * pct / 100)
            bar = "▓" * filled + "░" * (bar_len - filled)
            remaining = max(1, int(est_total - elapsed))
            try:
                await msg.edit_text(
                    f"⏳ ছবি থেকে MCQ generate হচ্ছে (Gemini AI)...\n"
                    f"[{bar}] ~{pct}%\n"
                    f"⏱️ আনুমানিক বাকি সময়: ~{remaining}s"
                )
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


async def send_mcqs_as_polls(context: ContextTypes.DEFAULT_TYPE, user_id: int, mcqs: list, chat_id: int,
                              return_first_link: bool = False, reply_to_message_id: int = None,
                              progress_msg=None, progress_prefix: str = ""):
    sent = 0
    first_link = None
    total = len(mcqs)
    start_time = time.monotonic()
    last_edit = 0.0

    last_uddipok_text = None
    uddipok_msg_id = None

    for i, mcq in enumerate(mcqs):
        q_text = build_question_text(user_id, mcq.get("question", ""))
        explanation = build_final_explanation(user_id, mcq.get("explanation", ""))
        opts = mcq.get("options", [])
        if len(opts) < 4:
            logger.warning("Poll send skipped: fewer than 4 options")
            continue

        # উদ্দীপক গ্রুপিং: নতুন উদ্দীপক পেলে সেটা আগে টেক্সট মেসেজ হিসেবে পাঠাবে,
        # এবং সেই গ্রুপের সব poll সেই মেসেজকে reply করবে।
        uddipok_text = mcq.get("uddipok", "")
        poll_reply_target = reply_to_message_id
        if uddipok_text:
            if uddipok_text != last_uddipok_text:
                try:
                    udd_msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"📖 <b>উদ্দীপক</b>\n\n{uddipok_text}",
                        parse_mode=ParseMode.HTML,
                        reply_to_message_id=reply_to_message_id,
                    )
                    uddipok_msg_id = udd_msg.message_id
                    last_uddipok_text = uddipok_text
                except Exception as e:
                    logger.warning(f"Uddipok send failed: {e}")
                    uddipok_msg_id = None
            poll_reply_target = uddipok_msg_id if uddipok_msg_id else reply_to_message_id
        else:
            last_uddipok_text = None

        ok = False
        for attempt in range(3):
            try:
                msg = await context.bot.send_poll(
                    chat_id=chat_id,
                    question=q_text,
                    options=opts[:4],
                    type="quiz",
                    correct_option_id=mcq.get("answer_index", 0),
                    explanation=explanation or None,
                    is_anonymous=True,
                    reply_to_message_id=poll_reply_target,
                )
                ok = True
                if sent == 0 and str(chat_id).startswith("-100"):
                    first_link = f"https://t.me/c/{str(chat_id)[4:]}/{msg.message_id}"
                break
            except Exception as e:
                logger.warning(f"Poll send attempt {attempt+1} failed: {e}")
                await asyncio.sleep(2)
        if ok:
            sent += 1

        # Live % progress + ETA update on the tracking message (throttled to ~every 2s)
        if progress_msg is not None:
            now = time.monotonic()
            if now - last_edit >= 2.0 or (i + 1) == total:
                elapsed = now - start_time
                done = i + 1
                pct = int((done / total) * 100) if total else 100
                avg = elapsed / done if done else 0
                remaining = max(0, total - done)
                eta_sec = int(avg * remaining)
                bar_len = 10
                filled = int(bar_len * pct / 100)
                bar = "▓" * filled + "░" * (bar_len - filled)
                try:
                    await progress_msg.edit_text(
                        f"{progress_prefix}⏳ Poll পাঠানো হচ্ছে...\n"
                        f"[{bar}] {pct}%\n"
                        f"📊 {done}/{total} সম্পন্ন\n"
                        f"⏱️ বাকি সময়: ~{eta_sec}s",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass
                last_edit = now

        await asyncio.sleep(0.4)
    return (sent, first_link) if return_first_link else sent


# ============================================================
# ACCESS CONTROL
# ============================================================

def require_permit(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not is_permitted(user_id):
            await update.message.reply_text(
                "আপনার বটে এক্সেস নাই❌\n"
                "বটের মালিক সাজিদ আলম খান প্রহর(RpMC)\n"
                "মালিকের সাথে যোগাযোগের জন্য 👉@Prohor_2007"
            )
            return
        return await func(update, context)
    return wrapper


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in OWNER_IDS and not is_permitted(user_id):
            await update.message.reply_text("❌ এই command শুধু Owner ব্যবহার করতে পারবে।")
            return
        return await func(update, context)
    return wrapper


async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Error শুধুমাত্র ERROR_NOTIFY_USER-কে পাঠানো হয়, অন্য কোনো owner/user-কে না।"""
    try:
        await context.bot.send_message(chat_id=ERROR_NOTIFY_USER, text=text[:4096], parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    error_text = f"❌ <b>Bot Error:</b>\n<code>{str(context.error)}</code>\n\nUpdate: {update}"
    try:
        await context.bot.send_message(chat_id=ERROR_NOTIFY_USER, text=error_text[:4096], parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ============================================================
# COMMANDS
# ============================================================

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! Bot is online.")


async def cmd_dbstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if sb:
        await update.message.reply_text("✅ Supabase connected — data persistent থাকবে restart এর পরেও।")
    else:
        await update.message.reply_text(
            "❌ Supabase NOT connected — SUPABASE_URL/SUPABASE_KEY env var missing।\n"
            "এখন ephemeral SQLite ব্যবহার হচ্ছে, restart হলেই সব ডেটা মুছে যাবে।"
        )


DETAILED_HELP_OWNER = (
    "👑 <b>Owner Commands — বিস্তারিত</b>\n\n"

    "🔑 <b>/permit (user id)</b>\n"
    "নির্দিষ্ট ইউজারকে বট ব্যবহারের অনুমতি দেয়। ইউজার আইডি না দিলে ফরম্যাট রিমাইন্ড করবে।\n"
    "Ex: <code>/permit 123456789</code>\n\n"

    "🔒 <b>/remove (user id)</b>\n"
    "আগে অনুমতিপ্রাপ্ত ইউজারের এক্সেস বাতিল করে। permitted list-এ না থাকলে জানিয়ে দেয়।\n"
    "Ex: <code>/remove 123456789</code>\n\n"

    "🗝️ <b>/addkey (gemini api key)</b>\n"
    "নতুন Gemini API key যোগ করে key pool-এ। Duplicate key হলে জানিয়ে দেয়, এতে quota rotation সহজ হয়।\n"
    "Ex: <code>/addkey AQ.xxxxxxxxxxxx</code>\n\n"

    "📊 <b>/keys</b>\n"
    "যোগ করা সব Gemini key-র quota/error status masked আকারে দেখায়।\n\n"

    "📡 <b>/channel (id) (name)</b>\n"
    "Force-subscribe বা broadcast channel যোগ করে। id এবং display name দুটোই দিতে হয়।\n"
    "Ex: <code>/channel -1001234567890 MyChannel</code>\n\n"

    "📋 <b>/channellist</b>\n"
    "যোগ করা সব চ্যানেলের তালিকা দেখায়।\n\n"

    "🗑️ <b>/removechannel (id)</b>\n"
    "নির্দিষ্ট চ্যানেল আইডি দিয়ে তালিকা থেকে চ্যানেল মুছে দেয়।\n"
    "Ex: <code>/removechannel -1001234567890</code>\n\n"

    "🏷️ <b>/tag (name)</b>\n"
    "MCQ প্রশ্নের জন্য tag/category সেট করে, যা পরে explanation ও sheet-এ track হয়।\n"
    "Ex: <code>/tag Physics-Chapter1</code>\n\n"

    "📄 <b>/sheet</b>\n"
    "বর্তমান সেশনের প্রশ্নগুলো নিয়ে Google Sheet-স্টাইল ডেটা/এক্সপোর্ট জেনারেট করে।\n\n"

    "💡 <b>/exp</b>\n"
    "প্রশ্নের ব্যাখ্যা (explanation) generate/toggle করার সেটিংস। on/off বা per-question explanation control করে।\n\n"

    "🖼️ <b>/img</b>\n"
    "ছবি (screenshot/photo of question) থেকে Gemini দিয়ে MCQ বানায়। কমান্ডের পর ছবি পাঠাতে হয়। এরপর CSV backup auto তৈরি হয়।\n\n"

    "📕 <b>/pdf</b>\n"
    "PDF ফাইল থেকে MCQ বের করে। কমান্ড দেওয়ার পর PDF আপলোড করতে হয়; fpdf2 দিয়ে output তৈরি হয় (Chromium ব্যবহার হয় না, Render free-tier RAM limit-এর কারণে)।\n\n"

    "🔖 <b>/wm</b>\n"
    "জেনারেট হওয়া PDF-এ watermark টেক্সট সেট করে।\n\n"

    "📶 <b>/ping</b>\n"
    "বট লাইভ কিনা, response time কেমন — quick status check করে।\n\n"

    "🗄️ <b>/dbstatus</b>\n"
    "Database (Supabase/SQLite) connection ও health status দেখায়।\n\n"

    "❓ <b>/help</b>\n"
    "এই detailed command list যেকোনো সময় আবার দেখায়।"
)

DETAILED_HELP_USER = (
    "📋 <b>Available Commands — বিস্তারিত</b>\n\n"

    "🏷️ <b>/tag (name)</b>\n"
    "MCQ প্রশ্নের জন্য tag/category সেট করে।\n"
    "Ex: <code>/tag Physics-Chapter1</code>\n\n"

    "💡 <b>/exp</b>\n"
    "প্রশ্নের ব্যাখ্যা (explanation) generate/toggle করার সেটিংস।\n\n"

    "🖼️ <b>/img</b>\n"
    "ছবি থেকে Gemini দিয়ে MCQ বানায়। কমান্ডের পর ছবি পাঠাতে হয়।\n\n"

    "📕 <b>/pdf</b>\n"
    "PDF ফাইল থেকে MCQ বের করে। কমান্ড দেওয়ার পর PDF আপলোড করতে হয়।\n\n"

    "🔖 <b>/wm</b>\n"
    "জেনারেট হওয়া PDF-এ watermark টেক্সট সেট করে।\n\n"

    "📶 <b>/ping</b>\n"
    "বট লাইভ কিনা, response time কেমন — quick status check করে।\n\n"

    "❓ <b>/help</b>\n"
    "এই detailed command list যেকোনো সময় আবার দেখায়।"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.first_name or "বন্ধু"

    if user.id not in OWNER_IDS and not is_permitted(user.id):
        await update.message.reply_text(
            "আপনার বটে এক্সেস নাই❌\n"
            "বটের মালিক সাজিদ আলম খান প্রহর(RpMC)\n"
            "মালিকের সাথে যোগাযোগের জন্য 👉@Prohor_2007",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(f"Welcome to Ronon Bot! প্রিয় {name}..😄", parse_mode=ParseMode.HTML)

    try:
        with open(os.path.join(os.path.dirname(__file__), "RononBot_Command_Guide.md"), "r", encoding="utf-8") as f:
            guide = f.read()

        def md_to_tg_html(text: str) -> str:
            text = re.sub(r'^>\s?(.+)$', r'\1', text, flags=re.MULTILINE)
            text = re.sub(r'^---+$', '', text, flags=re.MULTILINE)
            text = html.escape(text)
            text = re.sub(r'^#{1,6}\s*(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
            text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
            text = re.sub(r'(?<!\*)\*([^*\n]+?)\*(?!\*)', r'<i>\1</i>', text)
            text = re.sub(r'`([^`\n]+?)`', r'<code>\1</code>', text)
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text.strip()

        guide_html = md_to_tg_html(guide)
        chunks = []
        cur = ""
        for line in guide_html.split("\n"):
            if len(cur) + len(line) + 1 > 3800:
                chunks.append(cur)
                cur = line
            else:
                cur += ("\n" if cur else "") + line
        if cur:
            chunks.append(cur)
        for chunk in chunks:
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
            except Exception:
                await update.message.reply_text(re.sub(r'<[^>]+>', '', chunk))
    except Exception as e:
        logger.error(f"[cmd_start] Guide read error: {e}")
        text = ""
        if user.id in OWNER_IDS:
            text = DETAILED_HELP_OWNER
        elif is_permitted(user.id):
            text = DETAILED_HELP_USER
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


@owner_only
async def cmd_permit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id = None

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id

    elif context.args:
        arg = context.args[0].strip()
        if arg.lstrip("-").isdigit():
            target_id = int(arg)
        elif "t.me/+" in arg or "joinchat" in arg:
            await update.message.reply_text(
                "❌ Invite link (t.me/+...) থেকে user id বের করা সম্ভব না (Telegram API সাপোর্ট করে না)।\n"
                "সঠিক user id, @username, অথবা user-এর message reply করে /permit দিন।"
            )
            return
        else:
            m = re.search(r"(?:t\.me/)?@?([A-Za-z0-9_]{5,32})$", arg)
            username = m.group(1) if m else arg.lstrip("@")
            try:
                chat = await context.bot.get_chat(f"@{username}")
                target_id = chat.id
            except Exception:
                await update.message.reply_text(
                    "❌ এই username থেকে user resolve করা গেল না (user বটে message পাঠায়নি)।\n"
                    "বিকল্প: user-এর কোনো message reply করে /permit দিন, অথবা তার numeric user id দিন।"
                )
                return

    if target_id is None:
        await update.message.reply_text(
            "Format:\n/permit (user id)\n/permit @username\nবা user-এর message reply করে শুধু /permit লিখুন।"
        )
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
    ok, reason = db_add_key(key, update.effective_user.id)
    if ok:
        await update.message.reply_text("✅ API key যোগ হয়েছে। এখন Gemini 2.5 Flash কাজ করবে।")
    elif reason == "duplicate":
        await update.message.reply_text("⚠️ এই key আগে থেকেই আছে।")
    else:
        await update.message.reply_text(f"❌ Key সেভ করতে সমস্যা হয়েছে:\n<code>{reason}</code>", parse_mode="HTML")


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
    if not context.args:
        await update.message.reply_text("Format: /channel (channel id) (channel name)\nবা: /channel @channelusername")
        return

    if len(context.args) == 1:
        arg = context.args[0].strip()
        if arg.lstrip("-").isdigit():
            await update.message.reply_text("Format: /channel (channel id) (channel name)")
            return
        username = arg.lstrip("@")
        try:
            chat = await context.bot.get_chat(f"@{username}")
            channel_id = str(chat.id)
            channel_name = chat.title or username
        except Exception:
            await update.message.reply_text(
                "❌ এই username থেকে channel resolve করা গেল না। বট চ্যানেলে admin হিসেবে যোগ আছে কিনা চেক করো, "
                "অথবা সঠিক channel id + name দিয়ে দাও: /channel (id) (name)"
            )
            return
    else:
        first = context.args[0].strip()
        channel_name = " ".join(context.args[1:]).strip()
        if first.lstrip("-").isdigit():
            channel_id = first
        else:
            username = first.lstrip("@")
            try:
                chat = await context.bot.get_chat(f"@{username}")
                channel_id = str(chat.id)
                if not channel_name:
                    channel_name = chat.title or username
            except Exception:
                await update.message.reply_text(
                    "❌ এই username থেকে channel resolve করা গেল না। বট চ্যানেলে admin আছে কিনা চেক করো, "
                    "অথবা সঠিক channel id দিয়ে দাও: /channel (id) (name)"
                )
                return

    added = db_add_channel(channel_id, channel_name, update.effective_user.id)
    warn = "" if sb else "\n\n⚠️ <b>SUPABASE_URL/SUPABASE_KEY সেট নেই — এই ডেটা restart এ মুছে যাবে!</b> Render env vars এ যোগ করো।"
    if added:
        await update.message.reply_text(
            f"✅ চ্যানেল যোগ হয়েছে:\nID: <code>{channel_id}</code>\nName: <b>{channel_name}</b>{warn}",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            f"⚠️ এই চ্যানেল আগে থেকেই আছে, নাম আপডেট করা হয়েছে: <b>{channel_name}</b>{warn}",
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
    if query.from_user.id not in OWNER_IDS and not is_permitted(query.from_user.id):
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
        await update.message.reply_text("Format: /tag (name)")
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

    try:
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
            kb = [[
                InlineKeyboardButton(f"{'✅ ON' if is_on else '⬜ OFF'}", callback_data="exp_own_toggle"),
                InlineKeyboardButton("✏️ Edit", callback_data="exp_own_edit"),
            ]]
            await query.message.reply_text(
                f"✍️ <b>Own Explanation</b>\n\nবর্তমান টেক্সট:\n<code>{current_text}</code>\n\n"
                f"Status: {'🟢 ON (সব poll-এ এটাই বসবে)' if is_on else '🔴 OFF (AI explanation থাকবে)'}\n"
                f"⚠️ নতুন করে Edit/Set করলেই Auto ON হয়ে যাবে।",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif data == "exp_own_toggle":
            settings = db_get_settings(user_id)
            new_state = 0 if settings.get("own_explanation_on") else 1
            db_update_settings(user_id, own_explanation_on=new_state)
            current_text = settings.get("own_explanation") or "(সেট করা নেই)"
            kb = [[
                InlineKeyboardButton(f"{'✅ ON' if new_state else '⬜ OFF'}", callback_data="exp_own_toggle"),
                InlineKeyboardButton("✏️ Edit", callback_data="exp_own_edit"),
            ]]
            await query.edit_message_text(
                f"✍️ <b>Own Explanation</b>\n\nবর্তমান টেক্সট:\n<code>{current_text}</code>\n\n"
                f"Status: {'🟢 ON (সব poll-এ এটাই বসবে)' if new_state else '🔴 OFF (AI explanation থাকবে)'}\n"
                f"⚠️ নতুন করে Edit/Set করলেই Auto ON হয়ে যাবে।",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif data == "exp_own_edit":
            context.user_data["awaiting_own_exp"] = True
            await query.message.reply_text(
                "✏️ Own explanation-এর টেক্সট লিখে reply করুন:",
                reply_markup=ForceReply(selective=True)
            )
    except Exception as e:
        logger.error(f"exp_callback error: {e}", exc_info=True)
        await notify_owner(context, f"[exp_callback] Error:\n{e}")
        try:
            await query.message.reply_text("❌ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।")
        except Exception:
            pass


async def _generate_sheet_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, wait_msg, mcqs: list, topic: str, watermark: str):
    await wait_msg.edit_text("🎨 Sheet PDF বানানো হচ্ছে...\n[░░░░░░░░░░] 0%")

    async def _progress_ticker():
        steps = [10, 25, 40, 55, 70, 85, 95]
        for pct in steps:
            await asyncio.sleep(3)
            filled = pct // 10
            bar = "█" * filled + "░" * (10 - filled)
            try:
                await wait_msg.edit_text(f"🎨 Sheet PDF বানানো হচ্ছে...\n[{bar}] {pct}%")
            except Exception:
                pass

    ticker = asyncio.create_task(_progress_ticker())
    try:
        html_out = _build_solve_sheet_html(topic, 1, mcqs)
        pdf_bytes = await _html_to_pdf(html_out)
        if not pdf_bytes:
            logger.warning("[SHEET] chromium PDF failed, using fpdf2 fallback")
            pdf_bytes = generate_pdf(mcqs, topic, watermark)
        ticker.cancel()
        if not pdf_bytes:
            await wait_msg.edit_text("❌ PDF generate করতে সমস্যা হয়েছে!")
            return
        await wait_msg.edit_text("🎨 Sheet PDF বানানো হচ্ছে...\n[██████████] 100%")
        safe_title = re.sub(r"[^\w\u0980-\u09FF\-]+", "_", topic)[:50] or "RONON_Sheet"
        pdf_buffer = io.BytesIO(pdf_bytes)
        pdf_buffer.name = f"{safe_title}_sheet.pdf"
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=pdf_buffer,
            filename=f"{safe_title}_sheet.pdf",
            caption=f"📖 {topic}\n📝 মোট MCQ: {len(mcqs)}\nRONON"
        )
        await wait_msg.delete()
    except Exception as e:
        ticker.cancel()
        logger.error(f"[SHEET] generate error: {e}", exc_info=True)
        await notify_owner(context, f"[SHEET generate] Error:\n{e}")
        await wait_msg.edit_text("❌ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।")


async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_sheet_topic"):
        await handle_reply_text(update, context)


async def handle_reply_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""

    if context.user_data.get("awaiting_sheet_topic"):
        context.user_data["awaiting_sheet_topic"] = False
        topic = text.strip() or context.user_data.get("sheet_default_title", "Practice Sheet")
        mcqs = context.user_data.pop("sheet_mcqs", None)
        watermark = context.user_data.pop("sheet_watermark", "")
        if not mcqs:
            await update.message.reply_text("❌ Session expire হয়ে গেছে, আবার /sheet দাও।")
            return
        wait_msg = await update.message.reply_text("🎨 Sheet PDF বানানো হচ্ছে...\n[░░░░░░░░░░] 0%")
        await _generate_sheet_pdf(update, context, wait_msg, mcqs, topic, watermark)
        return

    if context.user_data.get("awaiting_exp_tag"):
        context.user_data["awaiting_exp_tag"] = False
        db_add_exp_tag(user_id, text.strip())
        db_update_settings(user_id, current_exp_tag=text.strip())
        await update.message.reply_text(f"✅ Explanation tag সেট হয়েছে: <b>{text.strip()}</b>", parse_mode=ParseMode.HTML)
        return

    if context.user_data.get("awaiting_own_exp"):
        context.user_data["awaiting_own_exp"] = False
        db_update_settings(user_id, own_explanation=text.strip(), own_explanation_on=1)
        await update.message.reply_text("✅ Own explanation সেভ হয়েছে — এখন থেকে 100% সব poll-এ এটাই বসবে।")
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
# HTML → PDF (Chromium) — same engine/style as QuizBot /sheet
# Falls back to fpdf2 generate_pdf() if chromium unavailable/fails
# (keeps free-tier Render RAM usage safe)
# ============================================================
async def _html_to_pdf(html: str):
    import tempfile
    chromium_bin = os.environ.get("CHROMIUM_PATH", "chromium")
    html_path = None
    pdf_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as f:
            f.write(html)
            html_path = f.name
        pdf_path = html_path.replace(".html", ".pdf")
        async with _CHROMIUM_SEMAPHORE:
            proc = await asyncio.create_subprocess_exec(
                chromium_bin, "--headless", "--no-sandbox",
                "--disable-gpu", "--disable-dev-shm-usage",
                f"--print-to-pdf={pdf_path}",
                f"file://{html_path}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.error("[PDF Gen] chromium timeout (45s) — killed, falling back to fpdf2")
                return None
        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as f:
                return f.read()
        else:
            logger.error(f"[PDF Gen] chromium produced no file. stderr: {stderr.decode(errors='ignore')[:1500]}")
    except FileNotFoundError:
        logger.error(f"[PDF Gen] chromium binary not found at '{chromium_bin}' — falling back to fpdf2")
    except Exception as e:
        logger.error(f"[PDF Gen] chromium error: {e} — falling back to fpdf2")
    finally:
        for p in (html_path, pdf_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
    return None


def _build_solve_sheet_html(topic: str, page: int, mcqs: list, answers: dict = None) -> str:
    """Same 2-col boxed RONON Solve Sheet HTML as QuizBot /sheet — 100% style match."""
    answers = answers or {}
    labels = ["A", "B", "C", "D"]
    items = ""
    for i, q in enumerate(mcqs):
        ci = q.get("answer_index", 0)
        ua = answers.get(str(i))
        ans_label = labels[ci] if ci < 4 else str(ci + 1)
        exp = q.get("explanation", "")

        opts_html = ""
        for j, opt in enumerate(q.get("options", [])):
            label = labels[j] if j < 4 else str(j + 1)
            cls = "opt"
            mark = ""
            if j == ci:
                cls += " correct"
                mark = " ✓"
            elif ua is not None and j == ua and ua != ci:
                cls += " wrong"
                mark = " ✗"
            opts_html += f'<div class="{cls}">({label}) {opt}{mark}</div>'

        items += f"""<div class="card">
  <div class="qno">{i+1:02d}.</div>
  <div class="qtxt">{q.get('question','')}</div>
  <div class="opts-wrap">{opts_html}</div>
  <div class="ans-row"><span class="ans-badge">['{ans_label}']</span></div>
  {f'<div class="exp-box"><b>ব্যাখ্যা:</b> {exp}</div>' if exp else ''}
</div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;600;700;800&display=swap');
*{{margin:0;padding:0;box-sizing:border-box;}}
@page{{size:A4;margin:8mm 10mm;}}
body{{font-family:'Noto Sans Bengali','Noto Sans','Noto Sans Bengali UI',sans-serif;background:#fff;font-size:14px;}}
.hdr{{text-align:center;padding:10px 14px;background:#1a237e;color:#fff;margin-bottom:12px;border-radius:8px;}}
.hdr h1{{font-size:20px;font-weight:800;}}
.hdr .sub{{font-size:14px;color:#c5cae9;margin-top:3px;}}
.hdr .brand{{font-size:12.5px;color:#9fa8da;margin-top:2px;}}
.grid{{column-count:2;column-gap:10px;}}
.card{{background:#fff;border:1.5px solid #c5cae9;border-radius:8px;padding:9px 10px;break-inside:avoid;page-break-inside:avoid;margin-bottom:10px;display:inline-block;width:100%;}}
.qno{{font-size:13px;font-weight:800;color:#1a237e;margin-bottom:3px;}}
.qtxt{{font-size:15px;font-weight:700;color:#111;margin-bottom:7px;line-height:1.6;}}
.opts-wrap{{display:flex;flex-direction:column;gap:3px;margin-bottom:7px;}}
.opt{{font-size:14px;color:#333;padding:2px 6px;border-radius:4px;border:1px solid #e0e0e0;line-height:1.5;}}
.opt.correct{{background:#e8f5e9;border-color:#43a047;color:#1b5e20;font-weight:700;}}
.opt.wrong{{background:#ffebee;border-color:#e53935;color:#b71c1c;font-weight:600;}}
.ans-row{{margin-bottom:4px;}}
.ans-badge{{font-size:13px;font-weight:800;color:#1b5e20;background:#f1f8e9;border:1px solid #81c784;border-radius:4px;padding:1px 7px;}}
.exp-box{{font-size:13.5px;color:#1a237e;background:#e8eaf6;border-left:3px solid #3949ab;padding:5px 7px;border-radius:0 5px 5px 0;line-height:1.55;}}
.footer{{text-align:center;font-size:11px;color:#9e9e9e;margin-top:12px;font-weight:700;}}
</style></head>
<body>
<div class="hdr">
  <h1>📋 {topic}</h1>
  <div class="sub">📄 Page No: {page} &nbsp;|&nbsp; 📝 {len(mcqs)} MCQ</div>
  <div class="brand">Special MCQ by Ronon</div>
</div>
<div class="grid">{items}</div>
<div class="footer">RONON</div>
</body></html>"""


async def _generate_styled_pdf_bytes(mcqs: list, topic: str, watermark: str = "") -> bytes:
    """Same Chromium HTML→PDF pipeline used by /sheet (RONON Solve Sheet style),
    reused so /pdf and /img PDF outputs match /sheet's styling exactly.
    Falls back to fpdf2 generate_pdf() if chromium unavailable/fails."""
    try:
        html_out = _build_solve_sheet_html(topic, 1, mcqs)
        pdf_bytes = await _html_to_pdf(html_out)
        if pdf_bytes:
            return pdf_bytes
        logger.warning("[PDF] chromium PDF failed, using fpdf2 fallback")
    except Exception as e:
        logger.error(f"[PDF] styled PDF generation error: {e} — falling back to fpdf2")
    return generate_pdf(mcqs, topic, watermark)


@require_permit
async def cmd_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text("❌ CSV ফাইলে reply করে /sheet দাও!")
        return
    doc = reply.document
    file_name = doc.file_name or ""
    if not file_name.lower().endswith(".csv"):
        await update.message.reply_text("❌ শুধু .csv file support করে!")
        return

    wait_msg = await update.message.reply_text("⏳ CSV পড়া হচ্ছে...")
    try:
        file = await context.bot.get_file(doc.file_id)
        csv_bytes = bytes(await file.download_as_bytearray())
        content = csv_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        letter_map = {"1": 0, "2": 1, "3": 2, "4": 3, "A": 0, "B": 1, "C": 2, "D": 3}
        mcqs = []
        for row in reader:
            q = row.get("question") or row.get("questions") or ""
            if not q.strip():
                continue
            opts = [row.get(f"option{i}", "").strip() for i in range(1, 5)]
            opts = [o for o in opts if o]
            if len(opts) < 2:
                continue
            ans_raw = str(row.get("answer", "1")).strip().upper()
            ans_idx = letter_map.get(ans_raw, 0)
            mcqs.append({
                "question": q.strip(),
                "options": opts,
                "answer_index": ans_idx,
                "explanation": (row.get("explanation") or "").strip()
            })

        if not mcqs:
            await wait_msg.edit_text("❌ CSV থেকে কোনো MCQ পাওয়া যায়নি! Format ঠিক আছে কিনা দেখো।")
            return

        default_title = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
        user_id = update.effective_user.id
        settings = db_get_settings(user_id)
        watermark = settings.get("watermark") or ""

        inline_topic = " ".join(context.args).strip() if context.args else ""
        if inline_topic:
            await _generate_sheet_pdf(update, context, wait_msg, mcqs, inline_topic, watermark)
            return

        context.user_data["sheet_mcqs"] = mcqs
        context.user_data["sheet_watermark"] = watermark
        context.user_data["sheet_default_title"] = default_title
        context.user_data["awaiting_sheet_topic"] = True
        await wait_msg.edit_text(
            f"✅ {len(mcqs)} টি MCQ পাওয়া গেছে!\n\n"
            f"📝 এই Sheet-এর Topic Name কী হবে?\n"
            f"(reply করে টাইপ করো, খালি পাঠালে ডিফল্ট <b>{default_title}</b> ব্যবহার হবে)\n\n"
            f"💡 Tip: পরের বার <code>/sheet TopicName</code> দিলে একবারেই হয়ে যাবে!",
        )
    except Exception as e:
        logger.error(f"cmd_sheet error: {e}", exc_info=True)
        await notify_owner(context, f"[cmd_sheet] Error:\n{e}")
        await wait_msg.edit_text("❌ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।")


# ============================================================
# /img — Reply-based (new) + Old awaiting mode
# ============================================================

@require_permit
async def cmd_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # NEW: reply-based immediate processing
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        topic = " ".join(context.args).strip() if context.args else DEFAULT_TOPIC
        photo = update.message.reply_to_message.photo[-1]

        wait_msg = await update.message.reply_text("⏳ ছবি ডাউনলোড হচ্ছে...\n[░░░░░░░░░░] 0%")
        try:
            file = await context.bot.get_file(photo.file_id)
            img_bytes = bytes(await file.download_as_bytearray())

            await wait_msg.edit_text("⏳ ছবি থেকে MCQ generate হচ্ছে (Gemini AI)...\n[▓▓▓░░░░░░░] ~30%\n⏱️ আনুমানিক সময়: ~10-20s")

            gen_start = time.monotonic()
            progress_task = asyncio.create_task(_animate_generation_progress(wait_msg, gen_start))
            try:
                mcqs, error = await gemini_generate_mcq(img_bytes, "image/jpeg", topic=topic, page=1)
            finally:
                progress_task.cancel()

            if error or not mcqs:
                await wait_msg.edit_text(error or "❌ কোনো MCQ বানানো যায়নি।")
                return

            context.user_data["img_mcqs"] = mcqs
            context.user_data["img_topic"] = topic
            context.user_data["img_user_id"] = update.effective_user.id
            context.user_data["img_bytes"] = img_bytes

            gen_elapsed = int(time.monotonic() - gen_start)
            try:
                await wait_msg.edit_text(f"✅ MCQ Generate সম্পন্ন! [▓▓▓▓▓▓▓▓▓▓] 100% ({gen_elapsed}s)")
            except Exception:
                pass
            await wait_msg.delete()

            kb = [
                [InlineKeyboardButton("🖼️ Image Mode (image সহ channel-এ যাবে)", callback_data="imgmode_image")],
                [InlineKeyboardButton("📝 Topic Mode (শুধু MCQ Poll)", callback_data="imgmode_topic")],
            ]
            await update.message.reply_text(
                f"✅ <b>{len(mcqs)}</b>টি MCQ তৈরি হয়েছে!\n"
                f"🎯 Topic: <b>{topic}</b>\n\n"
                f"কোন mode-এ পাঠাবে?",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except Exception as e:
            logger.error(f"cmd_img reply error: {e}", exc_info=True)
            await notify_owner(context, f"[cmd_img] Error:\n{e}")
            await wait_msg.edit_text("❌ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।")
        return

    # OLD: set awaiting mode
    await update.message.reply_text("❌ ছবিতে reply করে /img দাও!")
    return


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

    if data == "img_csv_only":
        csv_bytes = generate_csv(mcqs)
        csv_buffer = io.BytesIO(csv_bytes)
        csv_buffer.name = f"MCQ_{topic.replace(' ', '_')}.csv"
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=csv_buffer,
            filename=f"MCQ_{topic.replace(' ', '_')}.csv",
            caption=f"📄 <b>{topic}</b> — MCQ CSV File\nমোট: {len(mcqs)}টি",
            parse_mode=ParseMode.HTML
        )
        return

    if data == "img_pdf_only":
        settings = db_get_settings(user_id)
        watermark = settings.get("watermark") or ""
        pdf_bytes = await _generate_styled_pdf_bytes(mcqs, topic, watermark)
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

    if data == "img_both":
        csv_bytes = generate_csv(mcqs)
        csv_buffer = io.BytesIO(csv_bytes)
        csv_buffer.name = f"MCQ_{topic.replace(' ', '_')}.csv"
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=csv_buffer,
            filename=f"MCQ_{topic.replace(' ', '_')}.csv",
            caption=f"📄 <b>{topic}</b> — MCQ CSV File\nমোট: {len(mcqs)}টি",
            parse_mode=ParseMode.HTML
        )
        settings = db_get_settings(user_id)
        watermark = settings.get("watermark") or ""
        pdf_bytes = await _generate_styled_pdf_bytes(mcqs, topic, watermark)
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

    if data in ("imgmode_image", "imgmode_topic"):
        mode = "image" if data == "imgmode_image" else "topic"
        context.user_data["img_mode"] = mode
        channels = db_list_channels()
        kb = []
        for cid, cname in channels:
            kb.append([InlineKeyboardButton(f"📢 {cname}", callback_data=f"imgch_{cid}")])
        kb.append([InlineKeyboardButton("📄 CSV Only", callback_data="img_csv_only")])
        kb.append([InlineKeyboardButton("📑 PDF Only", callback_data="img_pdf_only")])
        kb.append([InlineKeyboardButton("🎁 Both (CSV+PDF)", callback_data="img_both")])
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"📢 কোন channel-এ পাঠাবে?\n📌 Topic: <b>{topic}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("imgch_"):
        channel_id = data[len("imgch_"):]
        mode = context.user_data.get("img_mode", "topic")
        img_bytes = context.user_data.get("img_bytes")

        image_msg_id = None
        if img_bytes:
            try:
                caption = f"⌛RONON Special MCQ System\n🌟Topic: {topic}\n💎MCQ: {len(mcqs)}"
                photo_msg = await context.bot.send_photo(chat_id=channel_id, photo=io.BytesIO(img_bytes), caption=caption)
                if mode == "image":
                    image_msg_id = photo_msg.message_id
                else:
                    await context.bot.delete_message(chat_id=channel_id, message_id=photo_msg.message_id)
            except Exception as e:
                logger.warning(f"img photo send/delete failed: {e}")

        pre_text = f"🎯 <b>{topic}</b>\n📊 MCQ Polls Starting...\nমোট প্রশ্ন: {len(mcqs)}"
        try:
            pre_msg = await context.bot.send_message(chat_id=channel_id, text=pre_text, parse_mode=ParseMode.HTML,
                                                       reply_to_message_id=image_msg_id if image_msg_id else None)
        except Exception as e:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❌ চ্যানেলে পাঠাতে ব্যর্থ: {e}")
            return

        # Reply target for every poll + the end/summary message: prefer image, fallback to pre_text msg
        reply_target_id = image_msg_id if image_msg_id else pre_msg.message_id

        progress_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⏳ 📢 চ্যানেলে {len(mcqs)}টি poll পাঠানো হচ্ছে...\n[░░░░░░░░░░] 0%"
        )

        sent, first_link = await send_mcqs_as_polls(
            context, user_id, mcqs, channel_id, return_first_link=True,
            reply_to_message_id=reply_target_id,
            progress_msg=progress_msg
        )

        end_text = f"✅ MCQ Polls Completed!\n📊 Total: {sent} polls\n🏷️ Topic: {topic}"
        if first_link:
            end_text += f"\n🔗 First Poll Link:\n{first_link}"
        await context.bot.send_message(chat_id=channel_id, text=end_text, parse_mode=ParseMode.HTML,
                                        reply_to_message_id=reply_target_id)

        await progress_msg.edit_text(f"✅ {sent}টি poll চ্যানেলে পাঠানো হয়েছে!")
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

        mcqs, error = await gemini_generate_mcq(img_bytes, "image/jpeg", page=1)
        if error or not mcqs:
            await wait_msg.edit_text(error or "❌ কোনো MCQ বানানো যায়নি।")
            return

        await wait_msg.delete()
        sent = await send_mcqs_as_polls(context, user_id, mcqs, update.effective_chat.id)
        await update.message.reply_text(f"✅ {sent}টি MCQ poll পাঠানো হয়েছে!")
    except Exception as e:
        logger.error(f"handle_photo error: {e}", exc_info=True)
        await notify_owner(context, f"[handle_photo] Error:\n{e}")
        await wait_msg.edit_text("❌ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।")


# ============================================================
# /pdf — Reply-based (100% ported from QuizBot's /pdf: live dashboard,
# per-page progress, poll retry, first-poll-link summary, CSV export)
# ============================================================

def fmt_page(n: int) -> str:
    return str(n).zfill(2)


def build_pdf_dashboard(file_name, topic, page_status, start_time, total_mcq, total_polls):
    elapsed = int(time.time() - start_time)
    mins, secs = divmod(elapsed, 60)
    done = sum(1 for s in page_status if s["done"])
    total = len(page_status)
    pct = int(done / total * 100) if total else 0
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    lines = [
        "⏳ <b>Ronon PDF Processing...</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📄 File: {file_name}", f"🎯 Topic: {topic}", f"📋 Pages: {total} total",
        "━━━━━━━━━━━━━━━━━━━━━━"
    ]
    for s in page_status:
        if s["done"]:
            lines.append(f"✅ Page {fmt_page(s['page'])}: {s['mcq']} MCQ ✓")
        elif s["current"]:
            lines.append(f"⏳ Page {fmt_page(s['page'])}: Processing...")
        else:
            lines.append(f"⬜ Page {fmt_page(s['page'])}: Waiting")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 Progress: {pct}% [{bar}]",
        f"⏱️ Elapsed: {mins}:{secs:02d}",
        f"📝 MCQ done: {total_mcq}",
        f"🔄 Polls sent: {total_polls}"
    ]
    return "\n".join(lines)


@require_permit
async def cmd_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # NEW: reply-based immediate processing
    if update.message.reply_to_message and update.message.reply_to_message.document:
        try:
            doc = update.message.reply_to_message.document
            # file_name None হতে পারে (কিছু client/forward-এ metadata থাকে না) — আগে এখানে
            # .lower() সরাসরি None-এর উপর কল হয়ে crash করতো, পুরো command silently fail করতো।
            # QuizBot-এর মতোই এখন mime_type দিয়েও PDF চেক করা হয়, filename না থাকলেও কাজ করবে।
            file_name = doc.file_name or "document.pdf"
            is_pdf = file_name.lower().endswith(".pdf") or (doc.mime_type == "application/pdf")

            if not is_pdf:
                # আগে এখানে silently "OLD: awaiting mode"-এ পড়ে যেত, ইউজার কোনো কারণ ছাড়াই
                # confuse হতো। এখন স্পষ্ট বলে দেওয়া হচ্ছে কেন কাজ করছে না।
                await update.message.reply_text(
                    f"❌ যে ফাইলে reply করেছ ({file_name}) সেটা PDF না।\n"
                    "PDF ফাইলে reply করে আবার /pdf দাও।"
                )
                return

            text = update.message.text or ""
            args = context.args

            page_range = None
            channel_id = None
            topic = DEFAULT_TOPIC
            per_page_count = None
            thread_id = None  # forum/topic group-এ নির্দিষ্ট থ্রেডে পোস্ট করার জন্য (QuizBot-এর -t সাথে মিলিয়ে)

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
                    # QuizBot-এ -t মানে numeric forum thread_id, topic name না (আগে ভুলভাবে
                    # topic-alias হিসেবে treat করা হতো, যেটা QuizBot-এর সাথে অসামঞ্জস্যপূর্ণ ছিল)
                    if args[i + 1].isdigit():
                        thread_id = int(args[i + 1])
                    i += 2
                else:
                    i += 1

            bracket_match = re.search(r'\[(\d+)\]', text)
            if bracket_match:
                per_page_count = int(bracket_match.group(1))
            else:
                # trailing plain number = per-page MCQ count (matches QuizBot's -m/-t "Topic" N pattern)
                nums = re.findall(r'(?<!\d)(\d+)(?!\d)', text.split('/pdf')[1] if '/pdf' in text else text)
                if nums:
                    last_num = int(nums[-1])
                    page_nums = page_range.replace("-", " ").split() if page_range else []
                    if str(last_num) not in page_nums and last_num < 200:
                        per_page_count = last_num

            context.user_data["pdf_doc"] = doc
            context.user_data["pdf_topic"] = topic
            context.user_data["pdf_page_range"] = page_range
            context.user_data["pdf_per_page"] = per_page_count
            context.user_data["pdf_user_id"] = update.effective_user.id
            context.user_data["pdf_thread_id"] = thread_id
            context.user_data["pdf_channel_id_arg"] = channel_id
            context.user_data["pdf_file_name"] = file_name
            # নতুন PDF হলে আগের PDF-এর cache clear — নাহলে ভুল/পুরনো MCQ button-এ deliver হয়ে যেতে পারে
            old_cache = context.user_data.get("pdf_extracted")
            if not old_cache or old_cache.get("doc_id") != doc.file_unique_id:
                context.user_data.pop("pdf_extracted", None)

            # নতুন: extraction শুরুর আগে New MCQ / Existing MCQ মোড বেছে নিতে হবে।
            # New MCQ = আগের মতোই AI নিজে থেকে MCQ বানাবে (source-এর সব তথ্য থেকে)।
            # Existing MCQ = page-এ আগে থেকে readymade বানানো MCQ থাকলে শুধু সেগুলোই
            # তুলে আনবে, নিজে থেকে কখনো নতুন MCQ বানাবে না।
            kb = [
                [InlineKeyboardButton("🆕 New MCQ", callback_data="pdfmode_new")],
                [InlineKeyboardButton("📋 Existing MCQ", callback_data="pdfmode_existing")],
            ]
            await update.message.reply_text(
                f"📋 <b>{file_name}</b>\n"
                f"🎯 Topic: <b>{topic}</b>\n"
                f"📄 Page Range: <b>{page_range or 'All'}</b>\n\n"
                "MCQ মোড বেছে নাও:\n"
                "🆕 <b>New MCQ</b> — সব তথ্য থেকে AI নিজে নতুন MCQ বানাবে (আগের মতো)\n"
                "📋 <b>Existing MCQ</b> — page-এ আগে থেকে থাকা readymade MCQ শুধু তুলে আনবে, নতুন বানাবে না",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return
        except Exception as e:
            # আগে এখানে কোনো safety net ছিল না — /pdf reply দেওয়ার পর args parsing বা
            # db_list_channels-এর মতো কোনো ধাপে unexpected error হলে ইউজার কিছুই দেখতো না
            # ("no response" সমস্যা)। এখন থেকে যেকোনো ব্যর্থতায় সরাসরি user-কে জানানো হবে।
            logger.error(f"[cmd_pdf] Unexpected error: {e}", exc_info=True)
            await update.message.reply_text(f"❌ কিছু একটা ভুল হয়েছে: {e}")
            await notify_owner(context, f"[cmd_pdf] Error for user {update.effective_user.id}:\n{e}")
            return

    await update.message.reply_text("❌ PDF ফাইলে reply করে /pdf দাও!")
    return


async def pdf_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """New MCQ / Existing MCQ মোড বাছাইয়ের পর extraction শুরু করে, তারপর channel/CSV/PDF/Both button দেখায়।"""
    query = update.callback_query
    await query.answer()
    data = query.data

    doc = context.user_data.get("pdf_doc")
    if not doc:
        await query.edit_message_text("❌ Session expire হয়ে গেছে, আবার PDF-এ reply করে /pdf দাও।")
        return

    existing_only = (data == "pdfmode_existing")
    context.user_data["pdf_existing_only"] = existing_only

    topic = context.user_data.get("pdf_topic", DEFAULT_TOPIC)
    page_range = context.user_data.get("pdf_page_range")
    per_page_count = context.user_data.get("pdf_per_page")
    channel_id = context.user_data.get("pdf_channel_id_arg")
    file_name = context.user_data.get("pdf_file_name", "document.pdf")

    mode_label = "📋 Existing MCQ" if existing_only else "🆕 New MCQ"
    status_message = await query.edit_message_text(f"⏳ PDF process হচ্ছে... ({mode_label})")

    ok = await _extract_pdf_mcqs(update, context, status_message)
    if not ok:
        return  # status_message already shows the specific error

    if channel_id:
        await process_pdf(update, context, channel_id, status_message=status_message)
    else:
        channels = db_list_channels()
        kb = []
        for cid, cname in channels:
            kb.append([InlineKeyboardButton(f"📢 {cname}", callback_data=f"pdfch_{cid}")])
        kb.append([InlineKeyboardButton("📄 CSV Only", callback_data="pdf_csv_only")])
        kb.append([InlineKeyboardButton("📑 PDF Only", callback_data="pdf_pdf_only")])
        kb.append([InlineKeyboardButton("🎁 Both (CSV+PDF)", callback_data="pdf_both")])
        no_channel_note = "" if channels else "\n\n⚠️ কোনো চ্যানেল যোগ করা নেই — /channel দিয়ে যোগ করো, অথবা CSV Only বেছে নাও।"
        cached = context.user_data["pdf_extracted"]
        skipped_note = ""
        skipped_pages = cached.get("skipped_pages") or []
        if existing_only and skipped_pages:
            skipped_note = (
                f"\n⚠️ <b>{len(skipped_pages)} টি page-এ</b> কোনো existing MCQ পাওয়া যায়নি, "
                f"তাই skip করা হয়েছে (page: {', '.join(str(p) for p in skipped_pages)})।"
            )
        # Existing MCQ mode-এ per-page count কখনো apply হয় না — page-এ যা readymade MCQ
        # থাকে সবই নেওয়া হয়, তাই এখানে count না দেখিয়ে স্পষ্টভাবে সেটা জানানো হচ্ছে
        per_page_line = "All Existing MCQ (no limit)" if existing_only else (per_page_count or "Highest Possible")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"📋 <b>{file_name}</b>\n"
                f"🎯 Topic: <b>{topic}</b>\n"
                f"📄 Page Range: <b>{page_range or 'All'}</b>\n"
                f"🎯 Per Page MCQ: <b>{per_page_line}</b>\n"
                f"🧩 Mode: <b>{mode_label}</b>\n"
                f"📝 Extracted MCQ: <b>{cached['total_mcq']}</b>{skipped_note}\n\n"
                f"Channel select করো:{no_channel_note}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb)
        )
    return


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
        status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ শুরু হচ্ছে...")
        await process_pdf(update, context, channel_id, status_message=status_msg)
        return

    if data == "pdf_csv_only":
        status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ শুরু হচ্ছে...")
        await process_pdf(update, context, None, csv_only=True, status_message=status_msg)
        return

    if data == "pdf_pdf_only":
        status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ শুরু হচ্ছে...")
        await process_pdf(update, context, None, pdf_only=True, status_message=status_msg)
        return

    if data == "pdf_both":
        status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ শুরু হচ্ছে...")
        await process_pdf(update, context, None, both_only=True, status_message=status_msg)
        return


async def _deliver_pdf_cached(context, all_mcqs_csv, total_mcq, topic, chat_id, channel_id,
                               thread_id, user_id, uname, csv_only, pdf_only, both_only, status_message):
    """Deliver already-extracted MCQ rows instantly, without re-running Gemini extraction."""
    if csv_only or pdf_only or both_only:
        if csv_only or both_only:
            csv_buf = io.StringIO()
            writer = csv.writer(csv_buf)
            writer.writerow(["questions", "option1", "option2", "option3", "option4",
                              "answer", "explanation", "type", "section"])
            for row in all_mcqs_csv:
                writer.writerow(row)
            csv_bio = io.BytesIO(csv_buf.getvalue().encode("utf-8"))
            csv_bio.name = f"{topic}_mcq.csv"
            await context.bot.send_document(
                chat_id=chat_id, document=csv_bio, filename=f"{topic}_mcq.csv",
                caption=f"📄 {topic} — {len(all_mcqs_csv)} MCQ"
            )
        if pdf_only or both_only:
            mcqs_for_pdf = [
                {"question": r[0], "options": [r[1], r[2], r[3], r[4]],
                 "answer_index": int(r[5]) - 1, "explanation": r[6]}
                for r in all_mcqs_csv
            ]
            settings = db_get_settings(user_id)
            watermark = settings.get("watermark") or ""
            pdf_bytes = await _generate_styled_pdf_bytes(mcqs_for_pdf, topic, watermark)
            if pdf_bytes:
                pdf_bio = io.BytesIO(pdf_bytes)
                pdf_bio.name = f"{topic}_mcq.pdf"
                await context.bot.send_document(
                    chat_id=chat_id, document=pdf_bio, filename=f"{topic}_mcq.pdf",
                    caption=f"📑 {topic} — {len(all_mcqs_csv)} MCQ"
                )
        await status_message.edit_text(f"✅ <b>Done!</b>\n📝 Total MCQ: {total_mcq}", parse_mode=ParseMode.HTML)
        return

    # Channel mode — page image ছাড়া cached mcq থেকে সরাসরি poll পাঠানো
    total_polls = 0
    first_poll_link = ""
    for i, mcq in enumerate(all_mcqs_csv):
        q_text = build_question_text(user_id, mcq[0])
        opts = mcq[1:5]
        explanation = build_final_explanation(user_id, mcq[6])
        ans_idx = int(mcq[5]) - 1
        poll_msg = None
        for _attempt in range(3):
            try:
                poll_msg = await context.bot.send_poll(
                    chat_id=channel_id, question=q_text, options=opts, type="quiz",
                    correct_option_id=ans_idx, explanation=(explanation or None),
                    is_anonymous=True, message_thread_id=thread_id,
                )
                break
            except Exception as e:
                logger.warning(f"[PDF cached] Poll attempt {_attempt+1} failed: {e}")
                await asyncio.sleep(2)
        if poll_msg:
            total_polls += 1
            if i == 0:
                cid_str = str(channel_id)
                if cid_str.startswith("-100"):
                    first_poll_link = f"https://t.me/c/{cid_str[4:]}/{poll_msg.message_id}"
                else:
                    first_poll_link = f"https://t.me/{cid_str.lstrip('@')}/{poll_msg.message_id}"
        await asyncio.sleep(0.4)

    summary = (
        f"🟥Ronon Special Practice System\n🎯Topic: {topic}\n🚀Total MCQ: {total_mcq}\n\n"
        f"🔗First Poll: {first_poll_link}\n\n💥শুভকামনা প্রিয় শিক্ষার্থী {uname}...\n"
    )
    summary_kwargs = {"chat_id": channel_id, "text": summary, "disable_web_page_preview": True}
    if thread_id:
        summary_kwargs["message_thread_id"] = thread_id
    await context.bot.send_message(**summary_kwargs)
    await status_message.edit_text(f"✅ <b>Done!</b>\n📝 Total MCQ sent: {total_polls}", parse_mode=ParseMode.HTML)


async def _extract_pdf_mcqs(update: Update, context: ContextTypes.DEFAULT_TYPE, status_message):
    """
    Extraction-only step (Gemini calls), ported 1:1 from the old process_pdf extraction
    loop. Runs ONCE per uploaded PDF, right after /pdf, BEFORE the channel/CSV/PDF/Both
    buttons are shown. Result is cached in context.user_data["pdf_extracted"].

    Returns True on success (cache populated), False on failure (status_message already
    shows the error, caller must stop and NOT show buttons).
    """
    doc = context.user_data.get("pdf_doc")
    topic = context.user_data.get("pdf_topic", DEFAULT_TOPIC)
    page_range = context.user_data.get("pdf_page_range")
    per_page = context.user_data.get("pdf_per_page")
    user_id = context.user_data.get("pdf_user_id", update.effective_user.id)
    uname = update.effective_user.first_name or "User"
    existing_only = context.user_data.get("pdf_existing_only", False)

    pdf_file_name = doc.file_name or "document.pdf"

    cached = context.user_data.get("pdf_extracted")
    if cached and cached.get("doc_id") == doc.file_unique_id:
        return True  # already extracted for this exact file — nothing to do

    await status_message.edit_text("⏳ PDF download হচ্ছে...")

    session_id = None
    try:
        file = await context.bot.get_file(doc.file_id)
        pdf_bytes = bytes(await file.download_as_bytearray())

        from pdf2image import convert_from_bytes, pdfinfo_from_bytes

        pdf_info = await asyncio.to_thread(pdfinfo_from_bytes, pdf_bytes)
        total_pages = int(pdf_info["Pages"])
        pages_to_process = parse_page_range(page_range, total_pages)

        if not pages_to_process:
            await status_message.edit_text("❌ কোনো পেজ সিলেক্ট করা যায়নি।")
            return False

        min_page, max_page = min(pages_to_process), max(pages_to_process)
        images = await asyncio.to_thread(
            convert_from_bytes, pdf_bytes, dpi=150,
            first_page=min_page, last_page=max_page
        )

        pages = []
        for idx, img in enumerate(images):
            actual_page = min_page + idx
            if actual_page in pages_to_process:
                pages.append((actual_page, img))
        pages.sort(key=lambda x: x[0])

        page_status = [{"page": p, "done": False, "current": False, "mcq": 0} for p, _ in pages]
        start_time = time.time()
        total_mcq = 0

        # Session তৈরি — extraction progress persist থাকবে (Supabase/SQLite),
        # ক্র্যাশ হলেও কতদূর হয়েছিল সেটা DB-তে দেখা যাবে (QuizBot-এর pdf_sessions-এর মতো)
        session_id = gen_session_id()
        db_save_session(session_id, {
            "user_id": user_id, "user_name": uname, "topic": topic,
            "channel_id": "", "total_pages": len(pages),
            "processed_pages": 0, "status": "processing"
        })

        await status_message.edit_text(
            build_pdf_dashboard(pdf_file_name, topic, page_status, start_time, 0, 0),
            parse_mode=ParseMode.HTML
        )

        all_mcqs_csv = []
        skipped_pages = []  # existing_only mode-এ যেসব page-এ কোনো readymade MCQ পাওয়া যায়নি

        for idx, (page_num, img) in enumerate(pages):
            page_status[idx]["current"] = True
            await status_message.edit_text(
                build_pdf_dashboard(pdf_file_name, topic, page_status, start_time, total_mcq, 0),
                parse_mode=ParseMode.HTML
            )

            try:
                buf = BytesIO()
                img.save(buf, format="JPEG")
                page_bytes = buf.getvalue()

                # Existing MCQ mode-এ per-page count কোনোভাবেই apply হবে না — page-এ
                # যতগুলো readymade MCQ থাকে সবগুলোই extract করতে হবে, count দিয়ে limit করা যাবে না
                effective_count = None if existing_only else per_page
                mcqs, error = await gemini_generate_mcq(
                    page_bytes, "image/jpeg", effective_count, topic=topic, page=page_num,
                    existing_only=existing_only
                )

                if existing_only and error and error.startswith("NO_EXISTING_MCQ::"):
                    # Soft skip: এই page-এ existing MCQ পাওয়া যায়নি (2-3 বার চেষ্টার পরও)।
                    # নিজে থেকে কিছু বানানো হবে না — শুধু কারণ জানিয়ে পরের page-এ যাওয়া হবে।
                    page_status[idx]["current"] = False
                    page_status[idx]["done"] = True
                    skipped_pages.append(page_num)
                    logger.info(f"[existing_only] Page {page_num} skipped — no readymade MCQ found ({error})")
                    continue

                if error or not mcqs:
                    page_status[idx]["current"] = False
                    page_status[idx]["done"] = True
                    if error and idx == 0:
                        # Fatal setup error (e.g. no API key) — tell the user immediately instead of
                        # silently skipping every remaining page with no feedback
                        await status_message.edit_text(f"❌ {error}")
                        db_update_session_progress(session_id, 0, status="failed")
                        return False
                    continue

                for m in mcqs:
                    opts = m.get("options", ["", "", "", ""])
                    # AI মাঝেমধ্যে option-এর শুরুতে "A) ", "ক. " ইত্যাদি prefix জুড়ে দেয় —
                    # CSV-তে সেটা থাকলে duplicate/messy দেখায়, তাই strip করা হচ্ছে (QuizBot parity)
                    opts = [re.sub(r'^[A-Da-dক-ঘ][)\.।]\s*', '', str(o)) for o in opts]
                    ans_idx = m.get("answer_index", 0)
                    ans_num = str(ans_idx + 1)
                    all_mcqs_csv.append([m.get("question", ""), opts[0], opts[1], opts[2], opts[3],
                                          ans_num, m.get("explanation", ""), "1", "1"])

                total_mcq += len(mcqs)
                page_status[idx]["done"] = True
                page_status[idx]["current"] = False
                page_status[idx]["mcq"] = len(mcqs)
                await status_message.edit_text(
                    build_pdf_dashboard(pdf_file_name, topic, page_status, start_time, total_mcq, 0),
                    parse_mode=ParseMode.HTML
                )
                db_update_session_progress(session_id, page_num)

            except Exception as e:
                logger.error(f"[PDF extract] Page {page_num} error: {e}", exc_info=True)
                page_status[idx]["current"] = False
                page_status[idx]["done"] = True
                await notify_owner(context, f"[PDF extract] Page {page_num} error:\n{e}")

        if not all_mcqs_csv:
            db_update_session_progress(session_id, len(pages), status="failed")
            if existing_only and skipped_pages:
                await status_message.edit_text(
                    "❌ কোনো existing MCQ পাওয়া যায়নি।\n"
                    f"⚠️ সব {len(skipped_pages)} টি page-এ readymade MCQ ছিল না, তাই কিছু বানানো হয়নি "
                    "(Existing MCQ মোডে নতুন MCQ বানানো হয় না)।\n"
                    "নতুন MCQ চাইলে আবার /pdf দিয়ে এবার 🆕 New MCQ বেছে নাও।"
                )
            else:
                await status_message.edit_text("❌ কোনো MCQ বের করা যায়নি। অন্য PDF দিয়ে চেষ্টা করো।")
            return False

        context.user_data["pdf_extracted"] = {
            "doc_id": doc.file_unique_id,
            "all_mcqs_csv": all_mcqs_csv,
            "total_mcq": total_mcq,
            "skipped_pages": skipped_pages,
        }
        context.user_data["pdf_mcqs"] = [
            {"question": r[0], "options": [r[1], r[2], r[3], r[4]],
             "answer_index": int(r[5]) - 1, "explanation": r[6]}
            for r in all_mcqs_csv
        ]

        db_update_session_progress(session_id, len(pages), status="done")

        elapsed = int(time.time() - start_time)
        mins, secs = divmod(elapsed, 60)
        skip_line = f"\n⚠️ Existing MCQ না পেয়ে skip: {len(skipped_pages)} page" if (existing_only and skipped_pages) else ""
        await status_message.edit_text(
            f"✅ <b>Processing Complete!</b>\n\n📄 File: {pdf_file_name}\n🎯 Topic: {topic}\n"
            f"📝 Total MCQ: {total_mcq}\n📋 Pages: {len(pages)}\n⏱️ Time: {mins}:{secs:02d}{skip_line}",
            parse_mode=ParseMode.HTML
        )
        return True

    except Exception as e:
        logger.error(f"_extract_pdf_mcqs error: {e}", exc_info=True)
        if session_id:
            try:
                db_update_session_progress(session_id, 0, status="failed")
            except Exception:
                pass
        await notify_owner(context, f"[_extract_pdf_mcqs] Error:\n{e}")
        await status_message.edit_text("❌ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।")
        return False


async def process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id, csv_only: bool = False, status_message=None, pdf_only: bool = False, both_only: bool = False):
    """
    Delivery step only. Extraction now always happens beforehand in cmd_pdf via
    _extract_pdf_mcqs(), so by the time any button (Channel/CSV/PDF/Both) is pressed,
    context.user_data["pdf_extracted"] is already populated for the current PDF —
    delivery here is always instant, no re-processing.
    """
    doc = context.user_data.get("pdf_doc")
    topic = context.user_data.get("pdf_topic", DEFAULT_TOPIC)
    thread_id = context.user_data.get("pdf_thread_id")
    user_id = context.user_data.get("pdf_user_id", update.effective_user.id)
    chat_id = update.effective_chat.id
    uname = update.effective_user.first_name or "User"

    if not doc:
        text = "❌ ডেটা মেয়াদ উত্তীর্ণ।"
        if status_message:
            await status_message.edit_text(text)
        else:
            await update.message.reply_text(text)
        return

    if not status_message:
        status_message = await update.message.reply_text("⏳ প্রসেস হচ্ছে...")

    cached = context.user_data.get("pdf_extracted")
    if not cached or cached.get("doc_id") != doc.file_unique_id:
        # Safety net — should not normally happen since cmd_pdf extracts before showing
        # buttons. Falls back to extracting now instead of failing silently.
        ok = await _extract_pdf_mcqs(update, context, status_message)
        if not ok:
            return
        cached = context.user_data.get("pdf_extracted")

    await status_message.edit_text("⏳ পাঠানো হচ্ছে...")
    try:
        await _deliver_pdf_cached(
            context, cached["all_mcqs_csv"], cached["total_mcq"], topic, chat_id, channel_id,
            thread_id, user_id, uname, csv_only, pdf_only, both_only, status_message
        )
    except Exception as e:
        logger.error(f"[PDF cached-deliver] error: {e}", exc_info=True)
        await status_message.edit_text(f"❌ পাঠাতে সমস্যা হয়েছে: {e}")
        await notify_owner(context, f"[PDF cached-deliver] Error:\n{e}")


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
    file_name = doc.file_name or "document.pdf"  # filename None হলে আগে এখানেই crash হতো
    if not (file_name.lower().endswith(".pdf") or doc.mime_type == "application/pdf"):
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

            mcqs, error = await gemini_generate_mcq(page_bytes, "image/jpeg", page=i)
            if error or not mcqs:
                continue
            sent = await send_mcqs_as_polls(context, user_id, mcqs, update.effective_chat.id)
            total_sent += sent

        await wait_msg.delete()
        await update.message.reply_text(f"✅ সর্বমোট {total_sent}টি MCQ poll পাঠানো হয়েছে!")
    except Exception as e:
        logger.error(f"handle_document error: {e}", exc_info=True)
        await notify_owner(context, f"[handle_document] Error:\n{e}")
        await wait_msg.edit_text("❌ কিছু একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।")


# ============================================================
# MAIN — Webhook Mode
# ============================================================

async def keep_alive():
    """Layer 1 — নিজের /health endpoint নিজেই বারবার হিট করে Render-কে 'active' দেখায়।
    Render Free tier ১৫ মিনিট কোনো HTTP request না পেলে সার্ভিস sleep করে দেয় —
    এই ping সেটা ঠেকায়। ১৫ মিনিটের অনেক আগেই (৪ মিনিট পরপর) পাঠানো হচ্ছে যাতে
    কোনো একটা ping fail/timeout হলেও sleep হওয়ার আগেই পরেরটা পৌঁছায়।"""
    failures = 0
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{RENDER_URL}/health", timeout=15) as resp:
                    if resp.status == 200:
                        failures = 0
                    else:
                        failures += 1
        except Exception as e:
            failures += 1
            logger.warning(f"keep_alive ping failed ({failures}): {e}")
        # পরপর কয়েকবার fail করলে দ্রুত retry করো (স্বাভাবিক 4-min wait পর্যন্ত অপেক্ষা না করে)
        await asyncio.sleep(60 if failures >= 2 else 240)


async def keep_alive_telegram_poll():
    """Layer 2 — Telegram API-কেই সরাসরি বারবার poll করে (getMe)। এটা Render-এর
    নিজস্ব HTTP endpoint-এর ওপর নির্ভর করে না, তাই Layer 1 (self-ping) সম্পূর্ণ
    ব্যর্থ হলেও (যেমন aiohttp session সমস্যা, বা Render internal networking issue)
    bot process নিজে সচল থাকবে এবং outbound network activity বজায় রাখবে —
    outbound traffic-ও Render-কে 'service is doing something' সংকেত দেয়।"""
    while True:
        await asyncio.sleep(180)
        try:
            if ptb_app and ptb_app.bot:
                await ptb_app.bot.get_me()
        except Exception as e:
            logger.warning(f"keep_alive_telegram_poll failed: {e}")


async def keep_alive_watchdog():
    """Layer 3 — watchdog: প্রতি ১০ মিনিটে log-এ heartbeat লেখে, যাতে Render-এর
    log stream-এও কার্যকলাপ দেখা যায় (কিছু Render plan/monitoring log activity-কেও
    'not idle' সংকেত হিসেবে ব্যবহার করে) এবং process নিজে hang/deadlock করেছে কিনা
    সহজে বোঝা যায় — যদি heartbeat log বন্ধ হয়ে যায় তাহলে process আসলে freeze হয়েছে,
    শুধু network ping fail করেনি, সেটা ধরা সহজ হবে।"""
    while True:
        await asyncio.sleep(600)
        logger.info("💓 heartbeat — bot process is alive and responsive")


def main():
    global ptb_app
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN environment variable সেট করা নেই।")

    db_init()

    ptb_app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    ptb_app.add_handler(CommandHandler("start", cmd_start))
    ptb_app.add_handler(CommandHandler("help", cmd_help))
    ptb_app.add_handler(CommandHandler("permit", cmd_permit))
    ptb_app.add_handler(CommandHandler("remove", cmd_remove))
    ptb_app.add_handler(CommandHandler("addkey", cmd_addkey))
    ptb_app.add_handler(CommandHandler("keys", cmd_keys))
    ptb_app.add_handler(CommandHandler("channel", cmd_channel))
    ptb_app.add_handler(CommandHandler("channellist", cmd_channellist))
    ptb_app.add_handler(CommandHandler("removechannel", cmd_removechannel))
    ptb_app.add_handler(CommandHandler("tag", cmd_tagq))
    ptb_app.add_handler(CommandHandler("sheet", cmd_sheet))
    ptb_app.add_handler(CommandHandler("exp", cmd_exp))
    ptb_app.add_handler(CommandHandler("img", cmd_img))
    ptb_app.add_handler(CommandHandler("pdf", cmd_pdf))
    ptb_app.add_handler(CommandHandler("wm", cmd_wm))
    ptb_app.add_handler(CommandHandler("ping", cmd_ping))
    ptb_app.add_handler(CommandHandler("dbstatus", cmd_dbstatus))

    ptb_app.add_handler(CallbackQueryHandler(exp_callback, pattern="^exp_"))
    ptb_app.add_handler(CallbackQueryHandler(channel_callback, pattern="^(chdel_|chadd)"))
    ptb_app.add_handler(CallbackQueryHandler(img_callback, pattern="^(img_|imgmode_|imgch_)"))
    ptb_app.add_handler(CallbackQueryHandler(pdf_mode_callback, pattern="^pdfmode_"))
    ptb_app.add_handler(CallbackQueryHandler(pdf_callback, pattern="^pdfch_|^pdf_"))
    ptb_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    ptb_app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    ptb_app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, handle_reply_text))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.REPLY, handle_plain_text))

    ptb_app.add_error_handler(error_handler)

    # Webhook + Health server
    async def health_handler(request):
        # শুধু static "OK" না — bot process আসলে initialized/running কিনা যাচাই করে জানায়,
        # যাতে external uptime monitor (UptimeRobot ইত্যাদি) দিয়ে ping করলে সেটা প্রকৃত
        # health check হয়, শুধু "server up" না বরং "bot actually working" নিশ্চিত করে।
        is_healthy = ptb_app is not None and ptb_app.running
        return web.Response(
            text="OK" if is_healthy else "DEGRADED",
            status=200 if is_healthy else 503
        )

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
        try:
            await ptb_app.bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
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
        asyncio.create_task(keep_alive_telegram_poll())
        asyncio.create_task(keep_alive_watchdog())
        logger.info("🚀 Ronon Bot started in webhook mode (multilayer keep-alive active)")

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
    import time as _time
    _restart_count = 0
    while True:
        try:
            main()
            break
        except SystemExit:
            raise
        except Exception as e:
            _restart_count += 1
            logger.error(f"[FATAL] main() crashed (attempt {_restart_count}): {e}", exc_info=True)
            if _restart_count > 20:
                logger.error("[FATAL] Too many crashes, giving up.")
                raise
            _time.sleep(min(5 * _restart_count, 60))

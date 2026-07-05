"""
Ronon Bot — Telegram MCQ Bot
Owner-managed access, Gemini-powered /img /pdf MCQ poll generator,
per-poll tags + explanations. Webhook mode for Render.
"""
import os
import html
import re
import difflib
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
-তুমি কক্ষনো নিজে থেকে নতুন কোনো MCQ বানাবে না, কোনো তথ্য/লাইন/প্যারাগ্রাফ থেকে নতুন প্রশ্ন তৈরি করবে না — এমনকি page-এ MCQ বানানোর মতো ভালো তথ্য থাকলেও না। এই page-এ যতগুলো readymade MCQ চোখে দেখা যায় ঠিক ততগুলোই output হবে — এক্সট্রাও না, কমও না।
-যদি এই page-এ কোনো readymade MCQ না থাকে (শুধু প্লেইন টেক্সট/তথ্য/প্যারাগ্রাফ থাকে, কোনো "প্রশ্ন+option+উত্তর" স্ট্রাকচার নাই), তাহলে অবশ্যই empty JSON array [] রিটার্ন করবে। কোনো MCQ বানিয়ে দিবে না।
-এই page-এ থাকা প্রতিটি readymade MCQ MUST তুলে আনতে হবে — একটাও miss/skip করা যাবে না। খুব ছোট, অস্পষ্ট, বা কোণায় থাকা MCQ-ও বাদ দেওয়া যাবে না।
-OUTPUT ORDER: page-এ MCQ যে সিরিয়ালে/ক্রমে আছে (উপর থেকে নিচে, বাম থেকে ডান), output JSON array-ও ঠিক সেই একই ক্রমে দিবে — প্রতিটা MCQ-এর সাথে তার নিজের ৪টা option ঠিক একইভাবে (একই ক্রমে, একই ম্যাচিং সহ) বেঁধে রাখবে, কোনো MCQ-এর option অন্য MCQ-এর সাথে মিক্স হবে না।
-কোনো নির্দিষ্ট সংখ্যক MCQ বানানোর/নেওয়ার লিমিট নাই — page-এ যতগুলো readymade MCQ থাকে, ALL/সবগুলোই extract করতে হবে, কোনো একটাও বাদ দিয়ে অল্প কিছু দেওয়া চলবে না।
-MCQ-এর প্রশ্ন ও ৪টি option হুবহু সোর্সের টেক্সট অনুযায়ী রাখবে — rewrite/paraphrase/summarize করবে না, শুধু accurately extract করবে (শুধু নম্বরিং প্রিফিক্স যেমন ১./Q1./ক. রিমুভ করবে)।
-টপিকের নাম,অধ্যায়ের নাম,হেডলাইন,পেইজ সংখ্যা,সেকশনের নাম,"Card 1"/"Card 2" এর মতো navigation/label টেক্সট কখনো MCQ হিসেবে extract করবে না।
-স্পষ্ট বানান ভুল থাকলে ঠিক করে দেবে, কিন্তু অর্থ পাল্টাবে না।
-সংখ্যা/সাল/তারিখ (Bengali সংখ্যা যেমন ১৯৭৬ বা English সংখ্যা যেমন 1976) অক্ষত হুবহু রাখবে —
  Bengali সংখ্যাকে English-এ বা English সংখ্যাকে Bengali-তে কখনো convert করবে না, কোনো digit
  বদলাবে না। সোর্সে যে script/digit-এ সংখ্যা লেখা ঠিক সেভাবেই output-এ রাখবে। প্রতিটা সংখ্যা
  extract করার পর সোর্সের সাথে digit-by-digit মিলিয়ে verify করবে (৯↔9, ৬↔6 গুলিয়ে ফেলা কড়াভাবে নিষিদ্ধ)।

🌐 LANGUAGE RULE (STRICT — MUST FOLLOW):
-Source MCQ যে ভাষায় লেখা (Bengali বা English), হুবহু সেই ভাষায় রাখবে — কোনো translate করা সম্পূর্ণ নিষেধ

💥প্রশ্ন: সোর্সে যেভাবে লেখা হুবহু সেভাবে (rewrite করবে না)
💥অপশন: সোর্সের ৪টি option হুবহু, prefix (A)/B)/ক./ইত্যাদি) ছাড়া

🎯 ANSWER DETECTION (সব ফরম্যাট চেক করবে, finalize করার আগে ডাবল-চেক):
সঠিক উত্তর অবশ্যই page/image-এর আসল সোর্স থেকে আসতে হবে — নিজে কখনো অনুমান করে বসাবে না।
এই সোর্সগুলো এই ক্রমে খুঁজবে (একটাতে না পেলে পরেরটা চেক করবে):
Source A — অপশনের উপর সরাসরি মার্ক করা: গোল দাগ, টিক (✓), বোল্ড, আন্ডারলাইন, হাইলাইট, স্টার (★)
Source B — MCQ-এর ঠিক নিচে/পাশে সাথে সাথেই দেওয়া উত্তর, যেকোনো ফরম্যাটে লেখা থাকুক না কেন —
  "উত্তর: গ" / "উত্তর: ৩" / "Answer: C" / "Ans- গ" / শুধু বন্ধনীতে option letter, ইত্যাদি
  সবই সমানভাবে বৈধ সোর্স, ছোট ফন্টে বা bold ছাড়া প্লেইন টেক্সটে লেখা থাকলেও গোনায় ধরবে
Source C — একই page-এর নিচে answer table/box (যেমন "Answer: 1-A, 2-C...") — প্রশ্ন নম্বর মিলিয়ে বের করবে
Source D — কয়েক page পরে থাকা combined answer key — প্রশ্ন নম্বর মিলিয়ে বের করবে (দরকার হলে দূরের page ও স্ক্যান করবে)
Source E — আগের বা পরের page-এ থাকা answer key
→ প্রশ্ন নম্বর (বা নম্বর অস্পষ্ট হলে হুবহু প্রশ্নের টেক্সট) মিলিয়ে answer ম্যাচ করবে।
→ প্রতিটি MCQ-এর উত্তর আলাদাভাবে, স্বাধীনভাবে খুঁজবে — একটা MCQ-এর উত্তর format অন্যরকম/অস্পষ্ট দেখতে
  লাগলেও (যেমন explanation-এ লেখা "সঠিক উত্তর ...") সেটাকেও Source B হিসেবেই ধরবে, বাদ দিবে না।
→ কোনো ফরম্যাটেই উত্তর না পেলে (সব সোর্স স্ক্যান করার পরও) — শেষ অপশন হিসেবে answer "A" দেবে এবং explanation-এ "Answer not found in source" লিখবে।
→ প্রতিটি answer finalize করার আগে অন্তত দুইবার সোর্সের সাথে মিলিয়ে verify করবে — ভুল উত্তর, মিসিং উত্তরের চেয়েও খারাপ।

🔴🔴 CRITICAL — answer detection কখনো MCQ বাদ দেওয়ার কারণ না 🔴🔴
কোনো MCQ-এর উত্তর detect করা কঠিন/অস্পষ্ট মনে হলেও, সেই MCQ কখনো output থেকে বাদ দিবে না।
প্রশ্ন + ৪টা option readable থাকলে সেই MCQ সবসময় output-এ থাকবেই — answer না পাওয়া গেলে "A" +
"Answer not found in source" note দিয়ে দিবে (উপরের rule অনুযায়ী), কিন্তু পুরো MCQ skip করা কখনোই
option না। "একটা MCQ-এর answer format অন্যগুলোর চেয়ে আলাদা/কম স্পষ্ট" — এটা কখনো MCQ বাদ দেওয়ার
বৈধ কারণ না।

🎯 OPTION ORDER (STRICT — কখনো শাফল/পুনর্বিন্যাস করবে না):
-সোর্সে option যে ক্রমে আছে (ক,খ,গ,ঘ / A,B,C,D / ১,২,৩,৪) output-এও ঠিক সেই একই ক্রমে রাখবে।
-answer letter অবশ্যই সোর্সের option-এর আসল position অনুযায়ী দিবে — output-এর options array-তে
  ওই position-এই থাকবে সঠিক উত্তর, কোনোভাবে option শাফল/reorder করলে answer letter ভুল হয়ে
  যাবে, তাই option-এর ক্রম কখনো পরিবর্তন করবে না।
-প্রতিটা MCQ finalize করার আগে verify করো: options array-এর যে index-এ সঠিক উত্তর, answer
  letter সেই একই index নির্দেশ করছে কিনা (A=index 0, B=1, C=2, D=3)।

💡 ব্যাখ্যা (STRICT PRIORITY ORDER):
1) MCQ-এর নিচে/পাশে আগে থেকেই ব্যাখ্যা লেখা থাকলে → হুবহু, শব্দে-শব্দে, EXACTLY সোর্স থেকে কপি করবে। paraphrase/shorten/rewrite করবে না।
2) সরাসরি ব্যাখ্যা না থাকলে কিন্তু page-এ related তথ্য (প্যারাগ্রাফ, নোট, বক্স, টেবিল) থাকলে → সেই তথ্য দিয়ে সরাসরি fact হিসেবে ব্যাখ্যা বানাবে (নিচের forbidden-phrase rule মেনে)।
3) কোথাও কিছু না থাকলে → তখনই শুধু নিজের জ্ঞান থেকে সবচেয়ে প্রাসঙ্গিক, সঠিক ব্যাখ্যা বানাবে।
-Max 165 characters, বাংলা ভাষায়, তথ্যগতভাবে সঠিক।

🧮 MATH/CHEMISTRY ফরম্যাটিং (সবসময় active, প্রশ্ন+অপশন+ব্যাখ্যা সব জায়গায়):
-Chemical formula-তে subscript Unicode: H₂O, CO₂, NaHCO₃ (H2O লিখবে না)
-Ionic charge/exponent-এ superscript Unicode: Na⁺, Ca²⁺, x², 10³ (x^2 লিখবে না)
-Units/degree/multiplication ঠিকভাবে: °C, m/s², × (x না)

🚫 FORBIDDEN SOURCE-REFERENCE PHRASES (সবসময় active — প্রশ্ন ও ব্যাখ্যা দুই জায়গাতেই):
প্রশ্ন বা ব্যাখ্যায় কখনো সোর্স নিজেকে রেফার করে এমন phrase ব্যবহার করবে না, যেমন:
❌ "উল্লেখিত চিত্রে" / "চিত্রে দেখা যাচ্ছে" / "বক্সে" / "ছকে" / "উদ্দীপকে" / "সারণিতে" /
   "টপিকে" / "পৃষ্ঠায়" / "প্যাসেজে" / "অনুচ্ছেদে" / "লেখচিত্রে" / "গ্রাফে"
❌ "দেখা যাচ্ছে" / "বলা আছে" / "উল্লেখ করা আছে" / "লক্ষ করা যায়" / "দেখানো হয়েছে" / "প্রদত্ত" / "উপরে দেখানো"
❌ ইংরেজি equivalent: "as shown in the figure/table/passage", "mentioned in the text/page"
এর বদলে সবসময় fact/তথ্য সরাসরি এবং স্বাভাবিকভাবে বলবে, যেন সেটা সাধারণ জ্ঞান।

🟨 উদ্দীপক RULE (STRICT, self-contained — শুধু বাংলা উদ্দীপকযুক্ত প্রশ্নে না, উদ্দীপক থাকলে সবসময় active):
-বাংলা উদ্দীপক/passage/scenario থাকলে (যার নিচে একাধিক MCQ থাকে), সেই উদ্দীপকের সাথে যুক্ত
  প্রতিটা MCQ-র "question" ফিল্ডের শুরুতেই সেই উদ্দীপকের সম্পূর্ণ টেক্সট জুড়ে দিতে হবে, তারপর
  তার নিচে সেই নির্দিষ্ট MCQ-র প্রশ্ন — যাতে প্রতিটা MCQ standalone/self-contained থাকে
  (উদ্দীপক ছাড়া প্রশ্নটা অসম্পূর্ণ থাকা উচিত নয়)।
-একই উদ্দীপকের অধীনে একাধিক MCQ থাকলে প্রতিটাতেই সেই একই উদ্দীপক পুনরায় (কপি করে) জুড়ে দিতে হবে।
-এছাড়াও, গ্রুপিং/UI প্রয়োজনে সেই উদ্দীপক টেক্সট "uddipok" ফিল্ডে আলাদাভাবেও বসাবে (হুবহু একই টেক্সট,
  একই উদ্দীপকের সব MCQ-তে identical); উদ্দীপক না থাকলে "uddipok" ফিল্ড "" রাখবে।
-Pass 1: পুরো page টা top থেকে bottom, left থেকে right স্ক্যান করে সব readymade MCQ-এর একটা draft লিস্ট বানাও।
-Pass 2: আবার পুরো page টা নতুন করে স্ক্যান করো, checking mindset নিয়ে — Pass 1-এ কোনো MCQ বাদ পড়েছে কিনা। বিশেষভাবে page-এর একদম নিচের অংশ/শেষ MCQ, এবং column-এর একদম শেষ প্রশ্নটা আলাদাভাবে আবার চেক করো — শেষের MCQ সবচেয়ে বেশি miss হয়।
-Pass 3: Draft লিস্টের প্রতিটা MCQ-এর জন্য verify করো সঠিক উত্তর সত্যিই সোর্স থেকে নেওয়া হয়েছে কিনা, প্রশ্ন+৪টা option হুবহু মেলে কিনা এবং সোর্সের আসল ক্রমেই আছে কিনা, answer letter সঠিক position নির্দেশ করছে কিনা, উদ্দীপক-নির্ভর MCQ হলে সেই উদ্দীপক পুরোপুরি "question" ফিল্ডে prepend করা আছে কিনা (self-contained), এবং draft লিস্টের item সংখ্যা page-এ দৃশ্যমান MCQ সংখ্যার সাথে মিলছে কিনা (গণনা করে নিশ্চিত হও)।
-শুধুমাত্র এই ৩টা internal pass শেষ হওয়ার পরেই final JSON output দিবে।

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
                        ],
                        config=types.GenerateContentConfig(max_output_tokens=8192)
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


def _is_duplicate_mcq(norm_q: str, existing_keys: list, threshold: float = 0.85) -> str:
    """
    Exact match না থাকলেও near-identical প্রশ্ন (extraction attempt ভেদে সামান্য spelling/space
    difference) কে duplicate হিসেবে ধরার জন্য fuzzy match। Match পেলে সেই existing key রিটার্ন
    করে, না পেলে None।
    """
    if not norm_q:
        return None
    if norm_q in existing_keys:
        return norm_q
    for k in existing_keys:
        if not k:
            continue
        shorter, longer = (k, norm_q) if len(k) <= len(norm_q) else (norm_q, k)
        if not shorter:
            continue
        # substantial substring overlap (covers minor prefix/suffix drift) OR very high char overlap
        if shorter in longer and len(shorter) >= 0.7 * len(longer):
            return k
        ratio = difflib.SequenceMatcher(None, norm_q, k).ratio()
        if ratio >= threshold:
            return k
    return None


async def _extract_existing_mcqs_merged(prompt: str, image_bytes: bytes, mime_type: str, keys: list, page: int) -> tuple:
    """
    Existing MCQ mode-এর জন্য: N টা independent Gemini attempt চালিয়ে সব attempt-এর
    রেজাল্ট union/merge করে ফেরত দেয় — কোনো একটা attempt কম MCQ দিলেও (partial miss)
    অন্য attempt-এ সেই MCQ ধরা পড়লে সেটা ফাইনাল লিস্টে থাকবে। একই MCQ (question টেক্সট
    মিললে) duplicate হিসেবে বাদ দেওয়া হয়।
    """
    NUM_ATTEMPTS = 3  # প্রতি page-এ 3 টা independent extraction pass (প্রতিটা call নিজেই internally
                      # multi-pass self-verify করে, prompt দেখো), তারপর সবগুলো call-এর result union —
                      # কোনো একটা attempt-এ ধরা পড়া MCQ যেকোনো একবার এলেই final লিস্টে থাকবে,
                      # তাই ৩টা attempt মিলিয়ে coverage সবচেয়ে বেশি হয়।
    last_err = None
    merged: dict = {}  # normalized_question -> mcq dict
    merge_order: list = []  # normalized_question keys, first-seen order (page-এর serial বজায় রাখতে)
    attempt_counts: list = []  # প্রতি attempt-এ কয়টা MCQ পাওয়া গেছে (hallucination/runaway-merge guard)
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
                        ],
                        # existing_only mode-এ page-এ অনেক বেশি MCQ থাকতে পারে (৩০+) এবং প্রতিটা
                        # call internally multi-pass verify করে — output truncate হয়ে JSON parse
                        # fail (= false miss) এড়াতে token limit স্বাভাবিকের চেয়ে বেশি রাখা হয়েছে
                        config=types.GenerateContentConfig(max_output_tokens=16384, temperature=0.1)
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
                    attempt_counts.append(len(mcqs))
                    for m in mcqs:
                        key_q = _normalize_q_for_dedup(m.get("question", ""))
                        if not key_q:
                            continue
                        dup_key = _is_duplicate_mcq(key_q, merge_order)
                        if dup_key is None:
                            merged[key_q] = m
                            merge_order.append(key_q)
                    break  # এই attempt-এর জন্য key খোঁজা শেষ, পরের attempt-এ যাও
                last_err = "Unparseable response"
            except Exception as e:
                logger.warning(f"[existing_only] Gemini key failed (attempt {attempt}/{NUM_ATTEMPTS}, page {page}): {e}")
                last_err = str(e)
                continue

        if not got_this_attempt:
            logger.info(f"[existing_only] Page {page}: attempt {attempt} produced nothing usable ({last_err})")

    if merged:
        # merge_order = প্রথম যে attempt-এ যে ক্রমে MCQ পাওয়া গেছে সেই ক্রম — এটাই page-এর
        # আসল serial order-এর সবচেয়ে কাছের approximation, তাই এই ক্রমেই ফেরত দেওয়া হয়
        final_list = [merged[k] for k in merge_order]
        # Safety cap: union কখনোই কোনো একক attempt-এর সর্বোচ্চ count-এর চেয়ে বেশি হওয়া উচিত না —
        # হলে বুঝতে হবে dedup miss করেছে বা কোনো attempt hallucinate করেছে, তাই সেক্ষেত্রে
        # সবচেয়ে বেশি MCQ দেওয়া attempt-টাকেই সবচেয়ে নির্ভরযোগ্য ধরে সেই সংখ্যায় ক্যাপ করা হয়
        if attempt_counts and len(final_list) > max(attempt_counts):
            final_list = final_list[:max(attempt_counts)]
        return final_list, None

    if any_success:
        # সবগুলো attempt সফলভাবে চলেছে এবং প্রতিটাই [] দিয়েছে — মানে সত্যিই এই page-এ
        # কোনো readymade MCQ নেই, এটা genuine empty, error না
        return [], f"NO_EXISTING_MCQ::page-এ কোনো readymade MCQ পাওয়া যায়নি (confirmed by {NUM_ATTEMPTS} attempts)"

    return [], f"NO_EXISTING_MCQ::{last_err or 'no readymade MCQ found on this page'}"


def _has_mixed_digit_script(text: str) -> bool:
    """
    একই সংখ্যা token-এর ভেতর Bengali আর English digit মিশে থাকলে (যেমন '১9৭6') সেটা
    OCR/generation করার সময় digit ভুল বসে যাওয়ার (script-swap corruption) সংকেত।
    """
    if not text:
        return False
    bn_digits = set('০১২৩৪৫৬৭৮৯')
    for token in re.findall(r'[০-৯0-9]+', text):
        has_bn = any(c in bn_digits for c in token)
        has_en = any(c.isdigit() and c not in bn_digits for c in token)
        if has_bn and has_en:
            return True
    return False


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
            q_text = str(m.get("question", ""))
            expl_text = str(m.get("explanation", ""))
            if _has_mixed_digit_script(q_text) or any(_has_mixed_digit_script(o) for o in opts) or _has_mixed_digit_script(expl_text):
                logger.warning(f"[digit-integrity] Mixed Bengali/English digits detected in MCQ: {q_text[:60]}")
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
        raw_question = mcq.get("question", "")
        explanation = build_final_explanation(user_id, mcq.get("explanation", ""))
        opts = mcq.get("options", [])
        if len(opts) < 4:
            logger.warning("Poll send skipped: fewer than 4 options")
            continue

        uddipok_text = mcq.get("uddipok", "")
        if uddipok_text and raw_question.strip().startswith(uddipok_text.strip()):
            raw_question = raw_question.strip()[len(uddipok_text.strip()):].lstrip()
        q_text = build_question_text(user_id, raw_question)

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


# ============================================================
# /sheet handler + HTML->PDF pipeline moved to sheet_handlers.py
# to keep this file lighter. Imported here (not at top) to avoid
# a circular import, since sheet_handlers.py imports helpers from
# this module.
# ============================================================
from sheet_handlers import _generate_sheet_pdf, cmd_sheet, _generate_styled_pdf_bytes




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
# ============================================================
# /img — Reply-based (new) + Old awaiting mode
# ============================================================

# ============================================================
# /img and /pdf handlers moved to mcq_handlers.py to keep this
# file lighter. Imported here (not at top) to avoid a circular
# import, since mcq_handlers.py imports helpers from this module.
# ============================================================
from mcq_handlers import (
    cmd_img, img_mcqmode_callback, img_callback, handle_photo,
    fmt_page, build_pdf_dashboard, cmd_pdf, pdf_mode_callback, pdf_callback,
    _deliver_pdf_cached, _extract_pdf_mcqs, process_pdf, handle_document,
)


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
    ptb_app.add_handler(CallbackQueryHandler(img_mcqmode_callback, pattern="^imgmcqmode_"))
    ptb_app.add_handler(CallbackQueryHandler(img_callback, pattern="^(img_|imgmode_|imgch_)"))
    ptb_app.add_handler(CallbackQueryHandler(pdf_mode_callback, pattern="^pdfmode_"))
    ptb_app.add_handler(CallbackQueryHandler(pdf_callback, pattern="^pdfch_|^pdfimg_|^pdf_"))
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


def _run_with_restart():
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

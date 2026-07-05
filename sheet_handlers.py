"""
sheet_handlers.py — /sheet command and HTML-to-PDF solve-sheet generation.

Split out of bot.py to keep the main file lighter. Contains the
Chromium-based HTML→PDF pipeline (with fpdf2 fallback) and the /sheet
command handler. Shared logic (DB access, decorators, generate_pdf)
stays in bot.py and is imported here.

No function behavior was changed during this move — this is a pure
relocation of existing code.
"""
import os
import logging
import asyncio

from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from bot import (
    logger,
    _CHROMIUM_SEMAPHORE,
    require_permit,
    notify_owner,
    db_get_settings,
    generate_pdf,
)


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



"""
mcq_handlers.py — /img and /pdf command handlers for RononBot.

Split out of bot.py to keep the main file lighter. Contains only the
Telegram command/callback handlers and their private helpers for the
image-based and PDF-based MCQ extraction flows. All shared logic
(Gemini calls, DB access, poll sending, CSV/PDF generation, decorators)
stays in bot.py and is imported here.

No function behavior was changed during this move — this is a pure
relocation of existing code.
"""
import asyncio
import logging
import time

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, BotCommand
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from bot import (
    logger,
    DEFAULT_TOPIC,
    require_permit,
    is_permitted,
    notify_owner,
    gemini_generate_mcq,
    build_final_explanation,
    build_question_text,
    generate_csv,
    send_mcqs_as_polls,
    parse_page_range,
    gen_session_id,
    db_save_session,
    db_update_session_progress,
    db_get_settings,
    db_list_channels,
    _animate_generation_progress,
)
from sheet_handlers import _generate_styled_pdf_bytes


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

            context.user_data["img_bytes"] = img_bytes
            context.user_data["img_topic"] = topic
            context.user_data["img_user_id"] = update.effective_user.id

            await wait_msg.delete()

            # /pdf-এর মতোই: MCQ generate করার আগে New MCQ / Existing MCQ মোড বেছে নিতে হবে।
            # New MCQ = AI নিজে থেকে নতুন MCQ বানাবে (আগের মতো)।
            # Existing MCQ = ছবিতে আগে থেকে readymade MCQ থাকলে শুধু সেগুলোই তুলে আনবে।
            kb = [
                [InlineKeyboardButton("🆕 New MCQ", callback_data="imgmcqmode_new")],
                [InlineKeyboardButton("📋 Existing MCQ", callback_data="imgmcqmode_existing")],
            ]
            await update.message.reply_text(
                f"🎯 Topic: <b>{topic}</b>\n\n"
                "MCQ মোড বেছে নাও:\n"
                "🆕 <b>New MCQ</b> — ছবির তথ্য থেকে AI নিজে নতুন MCQ বানাবে (আগের মতো)\n"
                "📋 <b>Existing MCQ</b> — ছবিতে আগে থেকে থাকা readymade MCQ শুধু তুলে আনবে, নতুন বানাবে না",
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


async def img_mcqmode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """New MCQ / Existing MCQ মোড বাছাইয়ের পর ছবি থেকে MCQ generate করে, তারপর Image/Topic Mode button দেখায়।"""
    query = update.callback_query
    await query.answer()
    data = query.data

    img_bytes = context.user_data.get("img_bytes")
    topic = context.user_data.get("img_topic", DEFAULT_TOPIC)

    if not img_bytes:
        await query.edit_message_text("❌ Session expire হয়ে গেছে, আবার ছবিতে reply করে /img দাও।")
        return

    existing_only = (data == "imgmcqmode_existing")
    mode_label = "📋 Existing MCQ" if existing_only else "🆕 New MCQ"

    wait_msg = await query.edit_message_text(
        f"⏳ ছবি থেকে MCQ generate হচ্ছে (Gemini AI)... ({mode_label})\n[▓▓▓░░░░░░░] ~30%\n⏱️ আনুমানিক সময়: ~10-20s"
    )

    gen_start = time.monotonic()
    progress_task = asyncio.create_task(_animate_generation_progress(wait_msg, gen_start))
    try:
        mcqs, error = await gemini_generate_mcq(
            img_bytes, "image/jpeg", topic=topic, page=1, existing_only=existing_only
        )
    finally:
        progress_task.cancel()

    if error or not mcqs:
        if existing_only and error and error.startswith("NO_EXISTING_MCQ::"):
            err_msg = error.split("::", 1)[1]
            await wait_msg.edit_text(
                f"❌ ছবিতে কোনো readymade MCQ পাওয়া যায়নি।\n({err_msg})\n\n"
                "নতুন MCQ চাইলে আবার /img দিয়ে এবার 🆕 New MCQ বেছে নাও।"
            )
        else:
            await wait_msg.edit_text(error or "❌ কোনো MCQ বানানো যায়নি।")
        return

    context.user_data["img_mcqs"] = mcqs

    gen_elapsed = int(time.monotonic() - gen_start)
    try:
        await wait_msg.edit_text(f"✅ MCQ Generate সম্পন্ন! [▓▓▓▓▓▓▓▓▓▓] 100% ({gen_elapsed}s)")
    except Exception:
        pass

    kb = [
        [InlineKeyboardButton("🖼️ Image Mode (image সহ channel-এ যাবে)", callback_data="imgmode_image")],
        [InlineKeyboardButton("📝 Topic Mode (শুধু MCQ Poll)", callback_data="imgmode_topic")],
    ]
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"✅ <b>{len(mcqs)}</b>টি MCQ তৈরি হয়েছে!\n"
            f"🎯 Topic: <b>{topic}</b>\n"
            f"🧩 Mode: <b>{mode_label}</b>\n\n"
            f"কোন mode-এ পাঠাবে?"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb)
    )
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
        cached = context.user_data.get("pdf_extracted") or {}
        page_groups = cached.get("page_groups") or []
        page_breakdown = "\n".join(
            f"📌 Page {g['page']}: {len(g['mcqs'])} MCQ" for g in page_groups
        )
        if page_breakdown:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"📝 Extracted MCQ: <b>{cached.get('total_mcq', 0)}</b>\n\n{page_breakdown}",
                parse_mode=ParseMode.HTML
            )
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

        # Per-page MCQ breakdown — এই message কখনো edit/delete হবে না, নতুন standalone
        # message হিসেবে পাঠানো হচ্ছে (QuizBot-এর /qbm-এর মতো), তাই New MCQ বা Existing MCQ
        # যেভাবেই বানানো হোক না কেন, কোন page-এ কতগুলো MCQ হয়েছে সেটা persistent থাকবে।
        page_groups = cached.get("page_groups") or []
        page_breakdown = "\n".join(
            f"📌 Page {g['page']}: {len(g['mcqs'])} MCQ" for g in page_groups
        )
        breakdown_block = f"\n\n{page_breakdown}" if page_breakdown else ""

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"📋 <b>{file_name}</b>\n"
                f"🎯 Topic: <b>{topic}</b>\n"
                f"📄 Page Range: <b>{page_range or 'All'}</b>\n"
                f"🎯 Per Page MCQ: <b>{per_page_line}</b>\n"
                f"🧩 Mode: <b>{mode_label}</b>\n"
                f"📝 Extracted MCQ: <b>{cached['total_mcq']}</b>{skipped_note}"
                f"{breakdown_block}\n\n"
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
        context.user_data["pdf_pending_channel"] = channel_id
        kb = [
            [InlineKeyboardButton("🖼️ With Image", callback_data="pdfimg_with")],
            [InlineKeyboardButton("📝 Without Image", callback_data="pdfimg_without")],
        ]
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="কোন mode-এ পাঠাবে?",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("pdfimg_"):
        with_image = (data == "pdfimg_with")
        channel_id = context.user_data.get("pdf_pending_channel")
        if not channel_id:
            await query.edit_message_text("❌ Session expire হয়ে গেছে, আবার channel select করো।")
            return
        status_msg = await context.bot.send_message(chat_id=update.effective_chat.id, text="⏳ শুরু হচ্ছে...")
        await process_pdf(update, context, channel_id, status_message=status_msg, with_image=with_image)
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
                               thread_id, user_id, uname, csv_only, pdf_only, both_only, status_message,
                               with_image: bool = False, page_groups: list = None):
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

    # Channel mode
    if with_image and page_groups:
        # With Image — per page: post the page photo, then reply MCQ polls to it
        # (QuizBot-এর with-image system-এর মতো)
        total_polls = 0
        first_poll_link = ""
        for grp in page_groups:
            page_num = grp["page"]
            img_bytes = grp["img_bytes"]
            page_mcqs = grp["mcqs"]

            image_msg_id = None
            try:
                photo_bio = io.BytesIO(img_bytes)
                photo_bio.name = f"page_{page_num}.jpg"
                caption = f"🟥Ronon Special MCQ System\n🎯Topic: {topic}\n🌟Page No: {page_num}"
                photo_msg = await context.bot.send_photo(
                    chat_id=channel_id, photo=photo_bio, caption=caption,
                    message_thread_id=thread_id
                )
                image_msg_id = photo_msg.message_id
            except Exception as e:
                logger.warning(f"[PDF with-image] Photo send failed page {page_num}: {e}")

            for i, mcq in enumerate(page_mcqs):
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
                            reply_to_message_id=image_msg_id,
                        )
                        break
                    except Exception as e:
                        logger.warning(f"[PDF with-image] Poll attempt {_attempt+1} failed (page {page_num}): {e}")
                        await asyncio.sleep(2)
                if poll_msg:
                    total_polls += 1
                    if not first_poll_link:
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
        return

    # Without Image — প্রতি page-এর জন্য একটা text pre-message (Ronon branding + page no +
    # mcq count), তারপর সেই message-কে reply করে poll-গুলো, তারপর per-page end message —
    # QuizBot-এর with-image system-এর মতোই কাঠামো, শুধু ছবির জায়গায় টেক্সট pre-message।
    total_polls = 0
    first_poll_link = ""
    groups_to_use = page_groups if page_groups else [{"page": None, "mcqs": all_mcqs_csv}]

    for grp in groups_to_use:
        page_num = grp["page"]
        page_mcqs = grp["mcqs"]
        if not page_mcqs:
            continue

        page_line = f"\n🌟Page No: {page_num}" if page_num is not None else ""
        pre_text = f"🟥Ronon Special MCQ System\n🎯Topic: {topic}{page_line}\n💎MCQ: {len(page_mcqs)}"
        pre_kwargs = {"chat_id": channel_id, "text": pre_text}
        if thread_id:
            pre_kwargs["message_thread_id"] = thread_id
        pre_msg = await context.bot.send_message(**pre_kwargs)
        pre_msg_id = pre_msg.message_id

        page_poll_link = ""
        page_polls = 0
        for i, mcq in enumerate(page_mcqs):
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
                        reply_to_message_id=pre_msg_id,
                    )
                    break
                except Exception as e:
                    logger.warning(f"[PDF without-image] Poll attempt {_attempt+1} failed: {e}")
                    await asyncio.sleep(2)
            if poll_msg:
                total_polls += 1
                page_polls += 1
                if not page_poll_link:
                    cid_str = str(channel_id)
                    if cid_str.startswith("-100"):
                        page_poll_link = f"https://t.me/c/{cid_str[4:]}/{poll_msg.message_id}"
                    else:
                        page_poll_link = f"https://t.me/{cid_str.lstrip('@')}/{poll_msg.message_id}"
                    if not first_poll_link:
                        first_poll_link = page_poll_link
            await asyncio.sleep(0.4)

        end_page_line = f"🌟Page No: {page_num}\n" if page_num is not None else ""
        end_text = f"{end_page_line}🚀MCQ: {page_polls}"
        if page_poll_link:
            end_text += f"\n🔗Poll Link: {page_poll_link}"
        end_kwargs = {"chat_id": channel_id, "text": end_text, "reply_to_message_id": pre_msg_id}
        if thread_id:
            end_kwargs["message_thread_id"] = thread_id
        await context.bot.send_message(**end_kwargs)

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
    cached_mode = cached.get("mode") if cached else None
    wanted_mode = "existing" if existing_only else "new"
    if cached and cached.get("doc_id") == doc.file_unique_id and cached_mode == wanted_mode:
        return True  # already extracted for this exact file AND same mode — nothing to do

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
        page_groups = []  # NEW: [{"page": n, "img_bytes": jpeg_bytes, "mcqs": [row,...]}] — needed for With-Image delivery
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

                page_rows = []
                for m in mcqs:
                    opts = m.get("options", ["", "", "", ""])
                    # AI মাঝেমধ্যে option-এর শুরুতে "A) ", "ক. " ইত্যাদি prefix জুড়ে দেয় —
                    # CSV-তে সেটা থাকলে duplicate/messy দেখায়, তাই strip করা হচ্ছে (QuizBot parity)
                    opts = [re.sub(r'^[A-Da-dক-ঘ][)\.।]\s*', '', str(o)) for o in opts]
                    ans_idx = m.get("answer_index", 0)
                    ans_num = str(ans_idx + 1)
                    row = [m.get("question", ""), opts[0], opts[1], opts[2], opts[3],
                                          ans_num, m.get("explanation", ""), "1", "1"]
                    all_mcqs_csv.append(row)
                    page_rows.append(row)

                if page_rows:
                    page_groups.append({"page": page_num, "img_bytes": page_bytes, "mcqs": page_rows})

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
            "mode": "existing" if existing_only else "new",
            "all_mcqs_csv": all_mcqs_csv,
            "total_mcq": total_mcq,
            "skipped_pages": skipped_pages,
            "page_groups": page_groups,
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


async def process_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id, csv_only: bool = False, status_message=None, pdf_only: bool = False, both_only: bool = False, with_image: bool = False):
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

    existing_only = context.user_data.get("pdf_existing_only", False)
    wanted_mode = "existing" if existing_only else "new"
    cached = context.user_data.get("pdf_extracted")
    if not cached or cached.get("doc_id") != doc.file_unique_id or cached.get("mode") != wanted_mode:
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
            thread_id, user_id, uname, csv_only, pdf_only, both_only, status_message,
            with_image=with_image, page_groups=cached.get("page_groups")
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



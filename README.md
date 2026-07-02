# Ronon Bot

Telegram MCQ bot — Gemini 2.5 Flash দিয়ে ছবি/PDF থেকে MCQ poll বানায়।

## Commands
- `/start` — welcome + command list (owner vs user আলাদা)
- `/permit <user_id>` — (owner) ইউজারকে অনুমতি দিন
- `/remove <user_id>` — (owner) অনুমতি বাতিল
- `/addkey <gemini_api_key>` — (owner) Gemini key যোগ করুন
- `/tagQ <name>` — প্রশ্নের উপরে বসা tag সেট করুন
- `/exp` — explanation settings (Tag name / Own toggle+edit)
- `/img` — ছবি পাঠিয়ে MCQ poll বানান
- `/pdf` — PDF পাঠিয়ে MCQ poll বানান (সব page প্রসেস হয়)

## Render Deploy (Docker)

1. Render dashboard → **New +** → **Web Service**
2. GitHub repo connect করুন: `hamza818483-dotcom/RononBot`
3. **Language**: Docker (auto-detects `Dockerfile`)
4. **Environment Variables** সেট করুন:
   - `BOT_TOKEN` = আপনার Telegram bot token (@BotFather থেকে)
5. Instance type: Free (বা যেকোনো)
6. **Create Web Service** — deploy শুরু হবে

বট polling mode-এ চলে (webhook দরকার নেই), তাই কোনো URL config লাগবে না।

⚠️ Free tier-এ Render service inactive হলে sleep করে ও restart-এ SQLite data (permitted users, keys, tags) হারিয়ে যেতে পারে যদি disk persist না থাকে। প্রয়োজনে Render-এর Persistent Disk যোগ করুন এবং `DB_PATH` সেই mount path-এ set করুন।

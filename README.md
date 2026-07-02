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

⚠️ Free tier-এ Render service inactive হলে sleep করে ও restart-এ SQLite data (permitted users, keys, tags) হারিয়ে যেতে পারে যদি disk persist না থাকে। প্রয়োজনে Render-এর Persistent Disk যোগ করুন এবং `DB_PATH` সেই mount path-এ set করুন। (Supabase env vars সেট থাকলে data এমনিতেই Supabase-এ persist হয়, এই সমস্যা প্রযোজ্য নয়।)

## Sleep প্রতিরোধ — Multilayer System

Render Free tier ১৫ মিনিট কোনো HTTP request না পেলে সার্ভিস sleep করে দেয়। এটা ঠেকাতে বটে ৩টা layer built-in আছে:

1. **Layer 1 — Self-ping**: বট নিজেই প্রতি ৪ মিনিটে নিজের `/health` endpoint হিট করে। পরপর ২ বার fail করলে দ্রুত (১ মিনিট পরপর) retry করে।
2. **Layer 2 — Telegram poll**: প্রতি ৩ মিনিটে Telegram API-কে `getMe` কল করে — এটা Render-এর নিজস্ব HTTP layer-এর ওপর নির্ভর করে না, তাই Layer 1 সম্পূর্ণ ব্যর্থ হলেও কাজ করবে।
3. **Layer 3 — Watchdog heartbeat**: প্রতি ১০ মিনিটে log-এ heartbeat লেখে, process hang হয়েছে কিনা বোঝার জন্য।

**Layer 4 (সবচেয়ে reliable — বাইরের থেকে, recommended):** এই ৩টা layer বটের **ভেতর থেকে** কাজ করে — যদি পুরো process crash করে বা Render-এর networking সমস্যা হয়, ভেতরের কোনো layer-ই কাজ করবে না। তাই একটা external uptime monitor যোগ করা সবচেয়ে নিরাপদ:

- [UptimeRobot](https://uptimerobot.com) (ফ্রি) → New Monitor → HTTP(s) → URL: `https://rononbot.onrender.com/health` → Interval: 5 মিনিট
- অথবা [cron-job.org](https://cron-job.org) দিয়ে একই URL-এ প্রতি ৫ মিনিটে GET request

এই বাইরের monitor বটের process crash করলেও Render-কে সজাগ রাখতে (এবং crash হলে alert পেতে) সাহায্য করবে — বটের নিজের কোড এখানে অংশ নেয় না, তাই এটা truly independent layer।

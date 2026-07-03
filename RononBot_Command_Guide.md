# 🤖 RononBot — Full Command Guide
> `/start` দিলে **Owner** এই পুরো লিস্ট দেখবে। **Permitted user** এর জন্য শুধু non-owner commands গুলো (🏷️ /tag থেকে 📶 /ping পর্যন্ত) দেখাবে। **Unauthorized user** কিছুই পাবে না — শুধু "এক্সেস নাই" মেসেজ।

---

## 👑 Owner-Only Commands

### 🔑 `/permit <user_id>`
নির্দিষ্ট ইউজারকে বট ব্যবহারের অনুমতি দেয়।
- `/permit 123456789` — ওই user কে permitted list এ যোগ করে

### 🔒 `/remove <user_id>`
আগে অনুমতিপ্রাপ্ত ইউজারের এক্সেস বাতিল করে।
- `/remove 123456789` — permitted list এ না থাকলে জানিয়ে দেয়

### 🗝️ `/addkey <gemini_api_key>`
নতুন Gemini API key যোগ করে key pool এ, quota rotation এর জন্য।
- `/addkey AQ.xxxxxxxxxxxx`

### 📊 `/keys`
সব যোগ করা Gemini key এর quota/error status (masked) দেখায়।
- `/keys` — argument লাগে না

### 📡 `/channel <id> <name>`
Force-subscribe/broadcast channel যোগ করে।
- `/channel -1001234567890 MyChannel`

### 📋 `/channellist`
যোগ করা সব চ্যানেলের তালিকা দেখায়।
- `/channellist` — argument লাগে না

### 🗑️ `/removechannel <id>`
নির্দিষ্ট চ্যানেল আইডি দিয়ে তালিকা থেকে মুছে দেয়।
- `/removechannel -1001234567890`

### 🗄️ `/dbstatus`
Database (Supabase/SQLite) connection ও health status দেখায়।
- `/dbstatus` — argument লাগে না

---

## 👥 সবার জন্য (Owner + Permitted User)

### 🏷️ `/tag <name>`
MCQ প্রশ্নের tag/category সেট করে — প্রতিটা poll এর উপরে বসবে।
- `/tag Physics-Chapter1`

### 🎨 `/wm` — Watermark
Generated PDF এ watermark টেক্সট সেট করে।
- `/wm` (কিছু না দিলে) → বর্তমান watermark দেখায়
- `/wm Ronon Academy` → নতুন watermark সেট করে, সব future PDF এ বসবে

### 💡 `/exp` — Explanation Settings
প্রশ্নের ব্যাখ্যা (explanation) generate/control করার সেটিংস।
- `/exp` → দুইটা button আসবে:
  - **🏷️ Tag Name** — reply করে explanation tag লিখতে হয়
  - **✍️ Own** — নিজের fixed explanation টেক্সট সেট করা যায়, ON/OFF toggle সহ (Edit করলেই auto ON হয়ে যায়)

### 📄 `/sheet [topic]` — CSV থেকে PDF সিট
CSV ফাইলে **reply করে** ব্যবহার করতে হয়।
- CSV তে reply + `/sheet` (topic ছাড়া) → পরে reply করে topic নাম চাইবে
- CSV তে reply + `/sheet Chemistry-Ch3` → একবারেই topic সহ PDF বানাবে (দ্রুততম পথ)

### 🖼️ `/img [topic]` — ছবি থেকে MCQ
দুইভাবে ব্যবহার করা যায়:

**Reply mode (flexible):**
ছবিতে reply করে —
- `/img` (topic ছাড়া) → default topic ব্যবহার হবে
- `/img Biology-Cell` → নির্দিষ্ট topic দিয়ে MCQ বানাবে

তারপর mode বেছে নিতে হয়: 🖼️ Image Mode (ছবিসহ channel এ যাবে) / 📝 Topic Mode (শুধু poll)
এরপর channel select অথবা 📄 CSV Only / 📑 PDF Only

**Simple mode:**
- শুধু `/img` (reply ছাড়া) → *"এখন একটা ছবি পাঠান"* বলবে, পরের ছবি নিজে থেকেই ধরবে

### 📕 `/pdf [flags]` — PDF থেকে MCQ (সবচেয়ে flexible command)
দুইভাবে ব্যবহার করা যায়:

**Reply + flags mode (full control):**
PDF ফাইলে reply করে, নিচের flag গুলো mix করে ব্যবহার করা যায়:

| Flag | মানে | উদাহরণ |
|---|---|---|
| `-p <range>` | পেজ রেঞ্জ | `-p 5-20` |
| `-c <channel_id>` | সরাসরি channel এ পাঠানো (button skip) | `-c -1001234567890` |
| `-m "<topic>"` | Topic নাম | `-m "Physics-Ch1"` |
| `-t <thread_id>` | Forum group এর নির্দিষ্ট থ্রেড (numeric) | `-t 15` |
| শেষে plain/`[N]` সংখ্যা | প্রতি পেজে কতটা MCQ | `30` অথবা `[30]` |

ব্যবহারের ধরন:
- `/pdf` → শুধু reply, সব default (সব পেজ, default topic)
- `/pdf -p 1-10` → শুধু ১-১০ পেজ
- `/pdf -m "Chemistry"` → শুধু topic সেট
- `/pdf -p 10-30 -m "Biology" 25` → পেজ ১০-৩০, topic Biology, প্রতি পেজে ২৫টা MCQ
- `/pdf -c -1001234567890` → সরাসরি channel এ, কোনো button ছাড়াই
- `/pdf -p 5-20 -m "Physics-Ch1" -c -1001234567890 -t 15 30` → সব flag একসাথে

flag না দিলে bot channel-selection button দেখায় + **📄 CSV Only** / **📑 PDF Only** অপশন।

**Simple mode:**
- শুধু `/pdf` (reply ছাড়া) → *"এখন একটা PDF ফাইল পাঠান"*, পরের PDF নিজে থেকেই ধরবে

### 📶 `/ping`
বট লাইভ কিনা, response time — quick status check।
- `/ping` — argument লাগে না

### ❓ `/help`
এই detailed command list যেকোনো সময় আবার দেখায় (owner হলে owner list, permitted হলে user list)।
- `/help` — argument লাগে না

---

## 🔐 Access Levels Summary

| User Type | `/start` এ কী দেখবে |
|---|---|
| **Owner** (`OWNER_IDS`) | সব ৮টা owner command + সব common command (মোট পূর্ণ লিস্ট) |
| **Permitted user** | শুধু common commands: `/tag /wm /exp /sheet /img /pdf /ping /help` |
| **Unauthorized** | "আপনার বটে এক্সেস নাই ❌" + owner এর সাথে যোগাযোগের তথ্য |

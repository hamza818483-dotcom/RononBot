# RononBot Keep-Alive — Cloudflare Worker (Layer 5)

এটা GitHub Actions-এর ৪-layer watchdog-এর বাইরে সম্পূর্ণ স্বাধীন একটা ৫ম layer —
সম্পূর্ণ ভিন্ন infrastructure (Cloudflare) থেকে প্রতি ১ মিনিটে `/health` ping করে।

## Deploy করার ধাপ

1. তোমার AtlasApp-এর জন্য Cloudflare account ইতিমধ্যে আছে (AI proxy worker চালাতে ব্যবহার করছ) — একই account ব্যবহার করবে।

2. Cloudflare dashboard → **Workers & Pages** → **Create** → **Create Worker**
   - নাম দাও: `rononbot-keepalive`
   - **Deploy** চাপো (ডিফল্ট hello-world কোড দিয়ে প্রথমে deploy হবে)

3. Deploy হওয়ার পর সেই Worker-এ ঢুকে **Edit code** এ যাও, এবং এই ফোল্ডারের `worker.js` ফাইলের পুরো কোড copy করে paste করো, তারপর **Deploy** চাপো।

4. **Cron Trigger যোগ করা** (এইটাই সবচেয়ে গুরুত্বপূর্ণ ধাপ — এটা ছাড়া worker শুধু ম্যানুয়াল visit-এ চলবে, auto না):
   - Worker-এর **Settings** ট্যাব → **Triggers** → **Cron Triggers** → **Add Cron Trigger**
   - Cron expression দাও: `* * * * *` (প্রতি ১ মিনিটে)
   - **Add trigger** চাপো

5. ব্যাস — এখন Cloudflare প্রতি মিনিটে স্বয়ংক্রিয়ভাবে RononBot-এর `/health` endpoint hit করবে, GitHub Actions-এর ৪-layer system থেকে সম্পূর্ণ স্বাধীনভাবে।

## যাচাই করার উপায়

- Worker-এর **Logs** ট্যাব (Real-time Logs চালু করো) → প্রতি মিনিটে `RononBot health check: 200` লগ দেখা যাবে
- অথবা Worker-এর নিজের URL (`https://rononbot-keepalive.<your-subdomain>.workers.dev`) সরাসরি ব্রাউজারে ভিজিট করলেও instant ping হবে এবং status দেখাবে

/**
 * RononBot Keep-Alive — Layer 5 (Cloudflare Worker + Cron Trigger)
 *
 * এটা GitHub Actions-এর ৪টা layer-এর বাইরে সম্পূর্ণ স্বাধীন একটা layer —
 * সম্পূর্ণ ভিন্ন infrastructure (Cloudflare) থেকে চলে, তাই GitHub Actions
 * পুরো down হয়ে গেলেও (outage/rate-limit) এটা কাজ করতে থাকবে।
 *
 * সুবিধা: GitHub Actions-এর মিনিমাম ৫ মিনিট cron interval-এর সীমা এখানে নেই —
 * Cloudflare Cron Triggers-ও মিনিমাম ১ মিনিট, কিন্তু queue/delay ছাড়া অনেক বেশি
 * নির্ভরযোগ্যভাবে সময়মতো চলে।
 */

const RENDER_HEALTH_URL = "https://rononbot.onrender.com/health";

async function pingBot() {
	try {
		const res = await fetch(RENDER_HEALTH_URL, {
			method: "GET",
			signal: AbortSignal.timeout(15000),
		});
		console.log(`RononBot health check: ${res.status}`);
		return res.status === 200;
	} catch (e) {
		console.error(`RononBot ping failed: ${e.message}`);
		return false;
	}
}

export default {
	// Cron Trigger হ্যান্ডলার — wrangler.toml-এ schedule সেট করা আছে
	async scheduled(event, env, ctx) {
		ctx.waitUntil(pingBot());
	},

	// ম্যানুয়াল টেস্টের জন্য — এই Worker-এর নিজের URL ভিজিট করলেও ping ট্রিগার হবে
	async fetch(request, env, ctx) {
		const ok = await pingBot();
		return new Response(
			ok ? "✅ RononBot is awake" : "⚠️ RononBot did not respond with 200",
			{ status: ok ? 200 : 503 }
		);
	},
};

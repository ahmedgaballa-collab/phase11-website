// /api/tg — relay for the server's Telegram posts (2026-11-19).
// The Oracle server (Jeddah) sometimes can't get an answer from api.telegram.org:
// posts got stuck and then arrived 4-6 times at once. The server now sends each
// post here instead, and Vercel forwards it to Telegram over a reliable line.
// Only sendMessage, only with the secret relay key (Vercel env var TG_RELAY_KEY).
module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "POST") return res.status(405).json({ ok: false });
  const key = process.env.TG_RELAY_KEY || "";
  if (!key || req.headers["x-relay-key"] !== key) return res.status(403).json({ ok: false });
  const b = typeof req.body === "object" && req.body ? req.body : {};
  const token = String(b.token || "");
  if (!/^\d+:[\w-]{20,}$/.test(token) || !b.chat_id || !b.text) return res.status(400).json({ ok: false });
  const payload = { chat_id: String(b.chat_id), text: String(b.text).slice(0, 4096) };
  if (b.parse_mode) payload.parse_mode = String(b.parse_mode);
  if (String(b.disable_web_page_preview) === "true") payload.disable_web_page_preview = true;
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    res.status(r.status).setHeader("Content-Type", "application/json");
    return res.send(await r.text());
  } catch (e) {
    return res.status(502).json({ ok: false, error: "relay could not reach Telegram" });
  }
};

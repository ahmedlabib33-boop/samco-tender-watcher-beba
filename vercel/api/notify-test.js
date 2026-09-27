// Real push test from the cloud dashboard. Publishes exactly one notification through
// the ntfy server/topic configured by the watcher owner (deployed snapshot's notify
// block). The result returned is the push server's own answer — never an assumed success.
const snapshot = require("../data/latest.json");
const DEFAULT_SERVER = "https://ntfy.sh";

async function readJson(req) {
  if (req.body && typeof req.body === "object") return req.body;
  const chunks = [];
  let total = 0;
  try {
    for await (const c of req) {
      total += c.length;
      if (total > 4096) break;
      chunks.push(c);
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf-8") || "{}");
  } catch (e) {
    return {};
  }
}

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    res.status(405).json({ error: "method not allowed" });
    return;
  }
  res.setHeader("Cache-Control", "no-store, max-age=0");
  const cfg = (snapshot && snapshot.notify) || {};
  const configured = String(cfg.ntfy_topic || "").trim();
  if (!configured) {
    res.status(500).json({ sent: false, error: "the deployed snapshot has no ntfy topic yet — run the watcher locally, deploy, then retry" });
    return;
  }
  const body = await readJson(req);
  const want = String((body && body.topic) || "").trim();
  if (want && want !== configured) {
    res.status(400).json({ sent: false, error: "topic not allowed — this endpoint only publishes to the topic the watcher owner configured" });
    return;
  }
  if (typeof fetch !== "function") {
    res.status(500).json({ sent: false, error: "this Node runtime has no fetch" });
    return;
  }
  const server = String(cfg.ntfy_server || DEFAULT_SERVER).replace(/\/+$/, "");
  const payload = {
    topic: configured,
    title: (cfg.title_prefix || "SAMCO Watcher") + " — test notification",
    message: "Test from the cloud dashboard. If your phone shows this, real High/Critical tender alerts arrive the same way.",
    priority: 3,
    tags: ["construction", "bell"],
  };
  if (cfg.click_url) payload.click = cfg.click_url;
  const t0 = Date.now();
  try {
    const r = await fetch(server, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const text = (await r.text()).slice(0, 300);
    const sent = r.status === 200 && text.includes('"id"');
    res.status(sent ? 200 : 502).json({ sent: sent, http_status: r.status, topic: configured, server: server, elapsed_ms: Date.now() - t0, answer: text });
  } catch (e) {
    res.status(502).json({ sent: false, topic: configured, server: server, elapsed_ms: Date.now() - t0, error: String((e && e.message) || e).slice(0, 300) });
  }
};

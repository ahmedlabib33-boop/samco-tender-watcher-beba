// "Watch now" in the cloud: a real, time-boxed live fetch of the watched sources.
// It reports exactly what it verified and what it could not — nothing is invented.
// The full engine pass (extraction, change detection, scoring, snapshot rewrite) runs
// locally in Watcher.py; this function only does an honest live reachability + listing
// harvest check within the serverless time budget.
const snapshot = require("../data/latest.json");

// Same request profile as the local engine (browser-like UA + Accept-Language) so this
// check sees the same pages the engine sees; the SAMCO-OpportunityWatcher suffix keeps
// it identifiable. Not a spoofed third-party identity: same site, same operator.
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 SAMCO-OpportunityWatcher/2.0";
const BUDGET_MS = 50000;
const FETCH_TIMEOUT_MS = 11000;
const ROBOTS_TIMEOUT_MS = 4000;
const CONCURRENCY = 24;
const MAX_BYTES = 500000;
const KW = /(tender|bid|rfp|rfq|prequalif|procure|expression of interest|مناقص|عطاء|عطاأ|منافس|استدراج|تأهيل|تاهيل|اعلان|إعلان|طرح)/i;

const nowIso = () => new Date().toISOString();
const norm = (s) => String(s == null ? "" : s).replace(/\s+/g, " ").trim();
const normKey = (s) => norm(s).toLowerCase().replace(/[^a-z0-9\u0600-\u06ff]+/g, " ").trim();

async function readCapped(res, maxBytes) {
  if (!res.body || !res.body.getReader) return (await res.text()).slice(0, maxBytes);
  const reader = res.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      total += value.length;
      if (total >= maxBytes) { try { await reader.cancel(); } catch (e) {} break; }
    }
  } catch (e) { /* keep whatever arrived */ }
  const buf = Buffer.concat(chunks.map((c) => Buffer.from(c)));
  return buf.slice(0, maxBytes).toString("utf-8");
}

async function fetchWithTimeout(url, ms, asText) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  const t0 = Date.now();
  try {
    const r = await fetch(url, { redirect: "follow", signal: ctl.signal, headers: { "User-Agent": UA, "Accept-Language": "en,ar;q=0.8,fr;q=0.6", Accept: "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8" } });
    const body = asText ? await readCapped(r, MAX_BYTES) : "";
    return { ok: true, status: r.status, final_url: r.url, body, elapsed_ms: Date.now() - t0 };
  } catch (e) {
    const isTimeout = e && e.name === "AbortError";
    const cause = e && e.cause ? (e.cause.code || e.cause.message || String(e.cause)) : "";
    return { ok: false, status: null, error: isTimeout ? "timeout after " + ms + "ms" : ((e && e.message || String(e)) + (cause ? " [" + cause + "]" : "")), elapsed_ms: Date.now() - t0 };
  } finally {
    clearTimeout(timer);
  }
}

function robotsAllows(robotsTxt, path) {
  if (robotsTxt == null) return true; // no robots.txt = nothing disallowed
  const lines = robotsTxt.split(/\r?\n/);
  const groups = [];
  let cur = null;
  for (const raw of lines) {
    const line = raw.replace(/#.*$/, "").trim();
    if (!line) continue;
    const m = line.match(/^([A-Za-z-]+)\s*:\s*(.*)$/);
    if (!m) continue;
    const field = m[1].toLowerCase(), value = m[2].trim();
    if (field === "user-agent") { cur = { agents: [value.toLowerCase()], rules: [] }; groups.push(cur); }
    else if ((field === "disallow" || field === "allow") && cur) cur.rules.push({ allow: field === "allow", path: value });
  }
  const uaToken = UA.toLowerCase();
  const matching = groups.filter((g) => g.agents.some((a) => a !== "*" && uaToken.includes(a)));
  const group = matching.length ? matching : groups.filter((g) => g.agents.includes("*"));
  if (!group.length) return true;
  let verdict = true, best = -1;
  for (const g of group) {
    for (const rule of g.rules) {
      if (rule.path === "") continue;
      const p = rule.path.replace(/\*.*$/, "");
      if (path.startsWith(p) && p.length > best) { best = p.length; verdict = rule.allow; }
    }
  }
  return verdict;
}

function harvest(html) {
  const links = [];
  const re = /<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>([\s\S]{0,400}?)<\/a>/gi;
  let m;
  while ((m = re.exec(html)) !== null) {
    const text = norm(m[2].replace(/<[^>]*>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&"));
    if (text.length < 12 || !KW.test(text)) continue;
    links.push({ text: text.slice(0, 160), href: m[1] });
    if (links.length >= 400) break;
  }
  let table_rows = 0;
  const trRe = /<tr\b[^>]*>([\s\S]*?)<\/tr>/gi;
  while ((m = trRe.exec(html)) !== null) {
    const cells = (m[1].match(/<(td|th)\b/gi) || []).length;
    if (cells >= 2) table_rows++;
  }
  return { links, table_rows };
}

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    res.status(405).json({ error: "method not allowed" });
    return;
  }
  res.setHeader("Cache-Control", "no-store, max-age=0");
  const started = Date.now();
  const health = (snapshot && snapshot.health) || [];
  const tenders = (snapshot && snapshot.tenders) || [];
  if (typeof fetch !== "function") {
    res.status(500).json({ error: "this Node runtime has no fetch — cannot run a real live check" });
    return;
  }
  const targets = health
    .map((h) => ({ id: h.id, country: h.country || "", source_name: h.source_name || "", url: h.listing_url || h.url || "", render: !!h.render, snapshot_rows: h.rows_found }))
    .filter((t) => t.url);
  const unchecked = health.filter((h) => !(h.listing_url || h.url)).map((h) => h.id);
  if (!targets.length) {
    res.status(200).json({ mode: "cloud_live_check", checked_at: nowIso(), budget_s: BUDGET_MS / 1000, sources_total: health.length, checked: 0, unchecked: unchecked, ok: 0, failed: 0, new_candidates_total: 0, results: [], note: "no snapshot sources with URLs" });
    return;
  }

  // robots.txt first (cached per host, one real fetch each; capped so it never eats the budget)
  const hosts = [...new Set(targets.map((t) => { try { return new URL(t.url).origin; } catch (e) { return null; } }).filter(Boolean))];
  const robots = {};
  for (let i = 0; i < hosts.length; i += 12) {
    if (Date.now() - started > 8000) break;
    await Promise.all(hosts.slice(i, i + 12).map(async (origin) => {
      let r = await fetchWithTimeout(origin + "/robots.txt", ROBOTS_TIMEOUT_MS, true);
      if (!r.ok && origin.startsWith("https://")) r = await fetchWithTimeout(origin.replace("https://", "http://") + "/robots.txt", ROBOTS_TIMEOUT_MS, true);
      robots[origin] = (r.ok && r.status === 200 && r.body) ? r.body : null;
    }));
  }

  const planned = [];
  const robots_blocked = [];
  for (const t of targets) {
    let path = "/";
    try { const u = new URL(t.url); path = u.pathname + u.search; } catch (e) {}
    let origin = null;
    try { origin = new URL(t.url).origin; } catch (e) {}
    if (origin && !robotsAllows(robots[origin], path)) { robots_blocked.push(t.id); continue; }
    planned.push(t);
  }

  const results = [];
  let cursor = 0;
  async function worker() {
    for (;;) {
      if (Date.now() - started > BUDGET_MS) return;
      const i = cursor++;
      if (i >= planned.length) return;
      const t = planned[i];
      const r = await fetchWithTimeout(t.url, FETCH_TIMEOUT_MS, true);
      const row = { id: t.id, country: t.country, source_name: t.source_name, url: t.url, render: t.render, snapshot_rows_found: t.snapshot_rows };
      if (!r.ok || r.status == null || r.status >= 400) {
        row.ok = false; row.failed = true; row.http_status = r.status;
        row.error = r.error || ("HTTP " + r.status);
        row.elapsed_ms = r.elapsed_ms;
        results[i] = row;
        continue;
      }
      const { links, table_rows } = harvest(r.body || "");
      const snapTitles = new Set();
      for (const td of tenders) {
        if (String(td.country || "") !== String(t.country || "") && String(td.source_name || "") !== String(t.source_name || "")) continue;
        const k = normKey(td.title);
        if (k.length >= 12) snapTitles.add(k);
      }
      const candidates = [];
      for (const l of links) {
        const k = normKey(l.text);
        if (k.length < 20) continue;
        let known = false;
        for (const s of snapTitles) { if (s.includes(k) || k.includes(s)) { known = true; break; } }
        if (!known) candidates.push(l.text);
        if (candidates.length >= 3) break;
      }
      row.ok = true; row.failed = false; row.http_status = r.status; row.elapsed_ms = r.elapsed_ms;
      row.keyword_links = links.length;
      row.rows_found = table_rows;
      row.new_candidates = candidates;
      if (r.body && r.body.length >= MAX_BYTES) row.truncated = true;
      if (!links.length && !table_rows) row.note = "live fetch worked, but this page shown to a plain fetch carries no tender-shaped rows" + (t.render ? " (engine uses a headless-Chrome render pass for this source)" : "");
      results[i] = row;
    }
  }
  await Promise.all(Array.from({ length: CONCURRENCY }, worker));

  const done = results.filter(Boolean);
  const okCount = done.filter((r) => r.ok).length;
  const failCount = done.filter((r) => r.failed).length;
  const stillUnchecked = planned.filter((t, i) => !results[i]).map((t) => t.id);
  res.status(200).json({
    mode: "cloud_live_check",
    checked_at: nowIso(),
    budget_s: Math.round((Date.now() - started) / 100) / 10,
    sources_total: health.length,
    checked: done.length,
    unchecked: unchecked.concat(stillUnchecked),
    robots_blocked: robots_blocked,
    ok: okCount,
    failed: failCount,
    new_candidates_total: done.reduce((n, r) => n + ((r.new_candidates || []).length), 0),
    results: done,
    note: "Real live fetch from the cloud function. Rows counted = keyword-matched listing links and table rows on the page right now; candidates = keyword links whose text is not present in the deployed snapshot for that source. The full engine pass (extraction, deadline parsing, change detection, snapshot rewrite, push) runs locally via Watcher.py.",
  });
};

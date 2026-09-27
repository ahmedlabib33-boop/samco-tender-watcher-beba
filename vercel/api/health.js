// Machine-readable freshness/health of the deployed snapshot. Every field is derived
// from the bundled engine output; nothing is estimated or invented.
const snapshot = require("../data/latest.json");

module.exports = (req, res) => {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.setHeader("Allow", "GET, HEAD");
    res.status(405).json({ error: "method not allowed" });
    return;
  }
  const w = snapshot.watch || {};
  const generated = snapshot.generated_at || null;
  const ageSeconds = generated ? Math.max(0, Math.round((Date.now() - Date.parse(generated)) / 1000)) : null;
  const intervalMin = Number(w.interval_minutes || 60);
  const stale = ageSeconds == null ? true : ageSeconds > intervalMin * 60 * 2.2;
  res.setHeader("Cache-Control", "no-store, max-age=0");
  res.status(200).json({
    status: stale ? "stale_snapshot" : "ok",
    snapshot_generated_at: generated,
    snapshot_age_seconds: ageSeconds,
    stale,
    note: "This deployment serves the snapshot bundled at deploy time; the local watcher (run.bat) produces fresh snapshots.",
    engine: { mode: w.mode || null, run_no: w.run_no ?? null, loop_active: !!w.loop_active },
    sources: {
      total: w.sources_total ?? null,
      ok: w.sources_ok ?? null,
      failed: w.sources_failed ?? null,
    },
    tenders_total: w.records_total ?? (snapshot.tenders || []).length,
    tenders_new: w.records_new ?? null,
  });
};

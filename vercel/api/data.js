// Serves the real snapshot bundled at deploy time. No synthetic values: what the
// local watcher wrote is exactly what this returns.
const snapshot = require("../data/latest.json");

module.exports = (req, res) => {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.setHeader("Allow", "GET, HEAD");
    res.status(405).json({ error: "method not allowed" });
    return;
  }
  res.setHeader("Cache-Control", "no-store, max-age=0");
  res.status(200).json(snapshot);
};

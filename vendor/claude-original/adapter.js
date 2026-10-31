// claude-adapter: local proxy between 9router and api.anthropic.com.
// Strips Claude Code markers that make API-key requests fail with
// "Your credit balance is too low" (beta claude-code-20250219, x-app,
// and the "You are Claude Code..." system prompt).
// Usage: node adapter.js   (env PORT, default 20129; UPSTREAM default https://api.anthropic.com)
const http = require("http");
const https = require("https");

const PORT = +process.env.PORT || 20129;
const UPSTREAM = new URL(process.env.UPSTREAM || "https://api.anthropic.com");
const BLOCKED_BETAS = new Set(["claude-code-20250219", "oauth-2025-04-20"]);
const DROP_HEADERS = ["x-app", "anthropic-dangerous-direct-browser-access", "host", "content-length", "connection"];
const CC_PROMPT = /^\s*You are Claude Code, Anthropic's official CLI for Claude\.?\s*/;

function cleanHeaders(src) {
  const h = {};
  for (const [k, v] of Object.entries(src)) {
    const key = k.toLowerCase();
    if (DROP_HEADERS.includes(key) || key.startsWith("x-claude-code") || key.startsWith("x-stainless")) continue;
    h[key] = v;
  }
  // 9router adds "Authorization: Bearer <key>" for non-Anthropic base URLs; Anthropic wants x-api-key only.
  if (h.authorization) {
    const tok = String(h.authorization).replace(/^Bearer\s+/i, "");
    if (!h["x-api-key"] && tok.startsWith("sk-ant-api")) h["x-api-key"] = tok;
    if (h["x-api-key"]) delete h.authorization;
  }
  if (h["anthropic-beta"]) {
    const betas = String(h["anthropic-beta"]).split(",").map(s => s.trim()).filter(b => b && !BLOCKED_BETAS.has(b));
    betas.length ? (h["anthropic-beta"] = betas.join(",")) : delete h["anthropic-beta"];
  }
  if (!h["anthropic-version"]) h["anthropic-version"] = "2023-06-01";
  return h;
}

function cleanBody(buf) {
  let j;
  try { j = JSON.parse(buf.toString("utf8")); } catch { return buf; }
  if (typeof j.system === "string") {
    j.system = j.system.replace(CC_PROMPT, "");
    if (!j.system) delete j.system;
  } else if (Array.isArray(j.system)) {
    j.system = j.system
      .map(b => (b?.type === "text" && typeof b.text === "string" ? { ...b, text: b.text.replace(CC_PROMPT, "") } : b))
      .filter(b => !(b?.type === "text" && !b.text));
    if (!j.system.length) delete j.system;
  }
  return Buffer.from(JSON.stringify(j));
}

http.createServer((req, res) => {
  const chunks = [];
  req.on("data", c => chunks.push(c));
  req.on("end", () => {
    let body = Buffer.concat(chunks);
    if (body.length && /json/i.test(req.headers["content-type"] || "")) body = cleanBody(body);
    const headers = cleanHeaders(req.headers);
    if (body.length) headers["content-length"] = body.length;
    const url = new URL(req.url, UPSTREAM);
    url.searchParams.delete("beta");
    const up = https.request(url, { method: req.method, headers }, upRes => {
      res.writeHead(upRes.statusCode, upRes.headers);
      upRes.pipe(res);
      console.log(new Date().toISOString(), req.method, url.pathname, upRes.statusCode);
    });
    up.on("error", e => {
      console.error("upstream error:", e.message);
      if (!res.headersSent) res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ type: "error", error: { type: "api_error", message: "adapter: " + e.message } }));
    });
    up.end(body);
  });
}).listen(PORT, "127.0.0.1", () => console.log(`claude-adapter on http://127.0.0.1:${PORT} -> ${UPSTREAM.origin}`));

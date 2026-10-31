#!/usr/bin/env python3
"""
claude-adapter — sidecar gateway between 9router and api.anthropic.com

Why this exists
---------------
9router (old build) forwards Claude Code markers that make API-key requests fail
with "Your credit balance is too low":
  * beta "claude-code-20250219" / "oauth-2025-04-20"
  * headers x-app, x-claude-code-*, x-stainless-*
  * system prompt "You are Claude Code, Anthropic's official CLI for Claude."
  * "Authorization: Bearer sk-ant-api..." (Anthropic wants x-api-key only)
  * "?beta=" query param

Port of adapter.js (Node) to stdlib-only Python, same behaviour.
Run:  python3 claude_adapter.py --host 127.0.0.1 --port 20129
Env:  PORT / UPSTREAM / CLAUDE_ADAPTER_LOG
"""
import argparse
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("CLAUDE_UPSTREAM", os.environ.get("UPSTREAM", "https://api.anthropic.com"))
PORT_DEFAULT = int(os.environ.get("PORT", "20129"))
LOG_PATH = os.environ.get("CLAUDE_ADAPTER_LOG", "/poki/tools/muse-adapter/claude_adapter.log")

BLOCKED_BETAS = {"claude-code-20250219", "oauth-2025-04-20"}
DROP_HEADERS = {"x-app", "anthropic-dangerous-direct-browser-access", "host", "content-length", "connection",
              # 9router sends Accept-Encoding: gzip/br and parses the reply as text.
              # If we forward it, upstream returns gzipped bytes while we strip
              # content-encoding below -> 9router gets binary and reports
              # "Invalid JSON response". Force identity instead.
              "accept-encoding"}
CC_PROMPT = re.compile(r"\s*You are Claude Code, Anthropic's official CLI for Claude\.?\s*")

CTX = ssl.create_default_context()


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def clean_headers(src):
    h = {}
    for k, v in src.items():
        key = k.lower()
        if key in DROP_HEADERS or key.startswith("x-claude-code") or key.startswith("x-stainless"):
            continue
        h[key] = v
    auth = h.get("authorization")
    if auth:
        tok = re.sub(r"^Bearer\s+", "", str(auth), flags=re.I)
        if "x-api-key" not in h and tok.startswith("sk-ant-api"):
            h["x-api-key"] = tok
        if "x-api-key" in h:
            del h["authorization"]
    beta = h.get("anthropic-beta")
    if beta:
        betas = [b.strip() for b in str(beta).split(",")]
        betas = [b for b in betas if b and b not in BLOCKED_BETAS]
        if betas:
            h["anthropic-beta"] = ",".join(betas)
        else:
            del h["anthropic-beta"]
    if "anthropic-version" not in h:
        h["anthropic-version"] = "2023-06-01"
    return h


def clean_body(buf: bytes) -> bytes:
    try:
        j = json.loads(buf.decode("utf8"))
    except (ValueError, UnicodeDecodeError):
        return buf
    if not isinstance(j, dict):
        return buf
    th = j.get("thinking")
    if isinstance(th, dict):
        # New models (haiku-5-5 and friends) reject legacy
        # {"type": "enabled", "budget_tokens": N} with:
        #   "thinking.type.enabled is not supported for this model.
        #    Use thinking.type.adaptive and output_config.effort..."
        # Normalize: enabled -> adaptive + effort derived from budget.
        # disabled -> drop (default behaviour anyway).
        t = th.get("type")
        if t == "disabled":
            j.pop("thinking", None)
        elif t == "enabled":
            try:
                budget = int(th.get("budget_tokens") or 8000)
            except (TypeError, ValueError):
                budget = 8000
            if budget <= 4096:
                effort = "low"
            elif budget <= 16384:
                effort = "medium"
            elif budget <= 32000:
                effort = "high"
            else:
                effort = "max"
            j["thinking"] = {"type": "adaptive"}
            oc = j.get("output_config")
            if not isinstance(oc, dict):
                oc = {}
            oc.setdefault("effort", effort)
            j["output_config"] = oc
    sys = j.get("system")
    if isinstance(sys, str):
        sys = CC_PROMPT.sub("", sys)
        if sys:
            j["system"] = sys
        else:
            j.pop("system", None)
    elif isinstance(sys, list):
        out = []
        for b in sys:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                b = dict(b, text=CC_PROMPT.sub("", b["text"]))
                if not b["text"]:
                    continue
            out.append(b)
        if out:
            j["system"] = out
        else:
            j.pop("system", None)
    return json.dumps(j, ensure_ascii=False).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-adapter/1.0"

    def log_message(self, format, *args):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        if body and "json" in ctype.lower():
            body = clean_body(body)
        headers = clean_headers(dict(self.headers.items()))
        if body:
            headers["content-length"] = str(len(body))
        # strip ?beta=
        parts = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        qs = [(k, v) for k, v in qs if k != "beta"]
        path = parts.path + (("?" + urllib.parse.urlencode(qs)) if qs else "")
        url = UPSTREAM.rstrip("/") + path
        req = urllib.request.Request(url, data=body if self.command != "GET" else None,
                                     headers=headers, method=self.command)
        try:
            resp = urllib.request.urlopen(req, timeout=300, context=CTX)
            status, rheaders, rbody = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status, rheaders, rbody = e.code, e.headers, e.read()
        except Exception as e:  # noqa: BLE001 - upstream down -> clean 502
            log("upstream error: %s" % e)
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b'{"type":"error"}')))
            self.end_headers()
            self.wfile.write(json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": "adapter: " + str(e)}}).encode())
            return
        self.send_response(status)
        for k, v in (rheaders.items() if hasattr(rheaders, "items") else []):
            if k.lower() in ("transfer-encoding", "content-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(rbody)))
        self.end_headers()
        try:
            self.wfile.write(rbody)
        except (BrokenPipeError, ConnectionResetError):
            pass
        log("%s %s -> %s" % (self.command, parts.path, status))

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    args = ap.parse_args()
    log("[claude-adapter] listening %s:%d -> %s" % (args.host, args.port, UPSTREAM))
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

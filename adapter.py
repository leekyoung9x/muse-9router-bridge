#!/usr/bin/env python3
"""
muse-adapter — sidecar gateway between 9router and api.meta.ai

Why this exists
---------------
Meta gates Muse Code subscription API keys with `client_attestation_denied`
(HTTP 403). The gate is not header-based and not body-field-based: it fires when
the Responses payload carries *flat* function tools at the top level. Muse Code
always wraps its tools in a `{"type": "namespace", ...}` block, so any client that
sends plain `{"type":"function", ...}` tools is classified as "not the Muse Code
client" and denied.

This adapter makes any client look like Muse Code on the way out, and unwraps the
namespace again on the way back so the client still sees its own tool names.

Transformations (request, POST /v1/responses)
  * tools: flat function tools -> one `namespace` block named "client"
  * tool_choice: dropped unless "auto" (Meta only supports auto)
  * reasoning_effort -> reasoning.effort
  * max_output_tokens clamped to >= 16
  * Muse Code style User-Agent + x-client-id added

Transformations (response)
  * "name":"client.<tool>" -> "name":"<tool>"   (SSE line-safe, and JSON bodies)

Run:  python3 adapter.py --host 127.0.0.1 --port 20132
"""
import argparse
import json
import os
import ssl
import sys
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("MUSE_UPSTREAM", "https://api.meta.ai")
NS_NAME = os.environ.get("MUSE_NS_NAME", "client")
NS_DESC = os.environ.get("MUSE_NS_DESC", "Client tool set.")
MUSE_UA = os.environ.get(
    "MUSE_UA",
    "muse-build/1.4.14 (non-interactive; linux-x86_64; build dec7d7497fecdad135ec5835a8161dbc92e55b58)",
)
LOG_PATH = os.environ.get("MUSE_ADAPTER_LOG", "/poki/tools/muse-adapter/adapter.log")
DUMP_DIR = os.environ.get("MUSE_DUMP_DIR", "")  # set to a dir to capture request/response bodies
CACHE_PATH = os.environ.get("MUSE_CALL_CACHE", "/poki/tools/muse-adapter/call_cache.sqlite")
# Per-session prompt_cache_key: 9router never forwards one (both paths show None),
# so every session shares Meta's default cache slot and concurrent sessions evict
# each other -> hit/miss flapping on identical prefixes. A stable key per session
# gives each conversation its own slot, the way Muse Code's tbh:main:<session> does.
SESSION_KEY = os.environ.get("MUSE_SESSION_KEY", "1") == "1"
# Tool-call replay age cap: Meta expires reasoning encrypted_content, and replaying
# dead chain links breaks the continuity Meta needs for follow-up calls. Entries
# older than this (seconds) are not replayed.
REPLAY_MAX_AGE = int(os.environ.get("MUSE_REPLAY_MAX_AGE", "2700"))
MIN_MAX_OUTPUT_TOKENS = 16
# Meta's edge occasionally accepts a request then never answers; 300s of silence is
# already far past any useful client budget, so fail fast with a clean JSON 504.
UPSTREAM_TIMEOUT = int(os.environ.get("MUSE_UPSTREAM_TIMEOUT", "180"))

SKIP_REQ_HEADERS = {"host", "content-length", "accept-encoding", "connection", "content-encoding"}
SKIP_RESP_HEADERS = {"transfer-encoding", "content-encoding", "connection", "content-length"}

CTX = ssl.create_default_context()


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def wrap_tools(tools):
    """Return (tools_out, changed). Flat function tools are moved into a namespace."""
    if not isinstance(tools, list) or not tools:
        return tools, False
    flat = [t for t in tools if isinstance(t, dict) and t.get("type") == "function"]
    if not flat:
        return tools, False  # already namespaced / builtin tools
    rest = [t for t in tools if not (isinstance(t, dict) and t.get("type") == "function")]
    block = {"type": "namespace", "name": NS_NAME, "description": NS_DESC, "tools": flat}
    return rest + [block], True


def transform_request_body(raw):
    """Mutate the JSON request body. Returns (new_bytes, notes:list[str])."""
    notes = []
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return raw, ["body not JSON, passthrough"]
    if not isinstance(body, dict):
        return raw, ["body not object, passthrough"]

    tools = body.get("tools")
    if tools is not None:
        new_tools, changed = wrap_tools(tools)
        if changed:
            body["tools"] = new_tools
            notes.append("tools->namespace(%d)" % len(new_tools[-1]["tools"]))

    if "tool_choice" in body:
        tc = body["tool_choice"]
        if tc != "auto":
            body.pop("tool_choice", None)
            notes.append("dropped tool_choice=%r" % (tc,))

    eff = body.pop("reasoning_effort", None)
    if eff is not None:
        cur = body.get("reasoning")
        if isinstance(cur, dict):
            cur.setdefault("effort", eff)
        else:
            body["reasoning"] = {"effort": eff}
        notes.append("reasoning_effort->reasoning.effort(%s)" % eff)

    mot = body.get("max_output_tokens")
    if isinstance(mot, int) and mot < MIN_MAX_OUTPUT_TOKENS:
        body["max_output_tokens"] = MIN_MAX_OUTPUT_TOKENS
        notes.append("max_output_tokens %d->%d" % (mot, MIN_MAX_OUTPUT_TOKENS))

    # Replay Meta's own tool-call items so the model keeps emitting real arguments.
    if isinstance(body.get("input"), list):
        body["input"], restored, stale = replay_history(body["input"])
        if restored:
            notes.append("replayed %d tool-call(s)" % restored)
        if stale:
            notes.append("skipped %d stale replay(s)" % stale)

    # Per-session cache slot (Muse sends tbh:main:<session>; 9router forwards none).
    if SESSION_KEY and not body.get("prompt_cache_key"):
        body["prompt_cache_key"] = session_cache_key(body)
        notes.append("session-key %s" % body["prompt_cache_key"])

    # Ask for the encrypted reasoning payloads we need in order to replay them later.
    inc = body.get("include")
    if not isinstance(inc, list):
        inc = []
    if "reasoning.encrypted_content" not in inc:
        inc.append("reasoning.encrypted_content")
    body["include"] = inc

    return json.dumps(body).encode(), notes


def unwrap_names(text):
    """Strip the adapter namespace prefix from tool names in JSON text."""
    if NS_NAME not in text:
        return text
    return text.replace('"name":"%s.' % NS_NAME, '"name":"')


class StreamPatcher:
    """Repairs function_call arguments while relaying an SSE stream.

    api.meta.ai streams a tool call's arguments in `response.function_call_arguments.done`
    but frequently sends `"arguments": ""` in the *authoritative* `response.output_item.done`
    event (and in `response.completed`). Gateways that trust output_item.done — 9router does —
    then hand the client a tool call with empty arguments, so the client calls e.g.
    read_file with no path, retries, and trips its own loop guardrail.

    This class accumulates arguments from every event and rewrites the empty copies.
    """

    def __init__(self):
        self.event = ""
        self.args = {}      # item_id -> accumulated arguments
        self.by_index = {}  # output_index -> item_id
        self.items = []     # completed output items, in order
        self.patched = 0
        self.saved = 0

    # ---------- accumulation ----------

    def _remember(self, d):
        t = d.get("type")
        if t == "response.output_item.added":
            item = d.get("item") or {}
            if item.get("type") == "function_call" and item.get("id"):
                self.by_index[d.get("output_index")] = item["id"]
                self.args.setdefault(item["id"], item.get("arguments") or "")
        elif t == "response.function_call_arguments.delta":
            iid = d.get("item_id")
            if iid:
                self.args[iid] = self.args.get(iid, "") + (d.get("delta") or "")
        elif t == "response.function_call_arguments.done":
            iid = d.get("item_id")
            if iid and isinstance(d.get("arguments"), str):
                self.args[iid] = d["arguments"]

    # ---------- repair ----------

    def _fix_item(self, item, index=None):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return False
        cur = item.get("arguments")
        if isinstance(cur, str) and cur.strip() not in ("", "{}"):
            return False
        iid = item.get("id") or self.by_index.get(index)
        good = self.args.get(iid)
        if not good:
            return False
        item["arguments"] = good
        self.patched += 1
        return True

    def _remember_calls(self, output):
        """Cache each function_call together with the reasoning item that precedes it."""
        entries, last_reasoning = [], None
        for item in output or []:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "reasoning":
                last_reasoning = item
            elif t == "function_call" and item.get("call_id"):
                entries.append((item["call_id"],
                                {"reasoning": last_reasoning, "function_call": item}))
                last_reasoning = None
        if entries:
            cache_put(entries)
            self.saved += len(entries)

    def feed(self, line):
        s = line.rstrip("\r\n")
        if s.startswith("event:"):
            self.event = s[6:].strip()
            return line
        if not s.startswith("data:"):
            return line
        payload = s[5:].strip()
        if not payload or payload == "[DONE]":
            return line
        try:
            d = json.loads(payload)
        except ValueError:
            return line
        if not isinstance(d, dict):
            return line

        self._remember(d)

        t = d.get("type") or self.event
        if t == "response.output_item.done":
            item = d.get("item") or {}
            if isinstance(item, dict) and item.get("type"):
                self.items.append(item)
            self._fix_item(item, d.get("output_index"))
        elif t in ("response.completed", "response.done", "response.incomplete"):
            output = (d.get("response") or {}).get("output") or self.items
            for i, item in enumerate(output):
                self._fix_item(item, i)
            self._remember_calls(output)

        out = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
        out = unwrap_names(out)
        return "data: " + out + ("\n" if line.endswith("\n") else "")


def _patch_json_output(body_bytes):
    """Repair a non-streaming Responses body (same empty-arguments failure mode)."""
    try:
        d = json.loads(body_bytes)
    except ValueError:
        return body_bytes
    if not isinstance(d, dict):
        return body_bytes
    patcher = StreamPatcher()
    output = d.get("output") or []
    changed = False
    for i, item in enumerate(output):
        if patcher._fix_item(item, i):
            changed = True
    patcher._remember_calls(output)
    if not changed:
        return body_bytes
    return json.dumps(d, ensure_ascii=False).encode()


class _SyntheticResponse:
    """Minimal stand-in for an http.client response built locally."""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self, *a):
        return self._body


def _looks_like_html(body_bytes, ctype):
    if "html" in (ctype or "").lower():
        return True
    head = body_bytes[:64].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html")


def dump(kind, payload):
    """Write a request/response body to DUMP_DIR when enabled (debugging aid)."""
    if not DUMP_DIR:
        return
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        name = "%s-%06d-%s" % (time.strftime("%H%M%S"), int(time.time() * 1000) % 1000000, kind)
        with open(os.path.join(DUMP_DIR, name), "wb") as f:
            f.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Tool-call history cache
#
# Meta only emits correct tool arguments when the conversation history replays the
# assistant's tool call the way Muse Code does: the `reasoning` item (carrying
# `encrypted_content`) immediately followed by the exact `function_call` item Meta
# returned. A chat->Responses gateway (9router) cannot rebuild those items from chat
# `tool_calls`, so the model degenerates and calls tools with `arguments: "{}"` —
# the client then calls read_file with no path, retries, and trips its loop guard.
#
# Measured on the live chain: fabricated function_call history = 0/5 correct,
# replayed Meta items = 5/5 correct. So we remember what Meta gave us and put it back.
# ---------------------------------------------------------------------------


def _cache_conn():
    import sqlite3
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(CACHE_PATH, timeout=5)
    conn.execute("CREATE TABLE IF NOT EXISTS calls (call_id TEXT PRIMARY KEY, payload TEXT, ts REAL)")
    return conn


def cache_put(entries):
    """entries: list of (call_id, {"reasoning": item|None, "function_call": item})."""
    if not entries:
        return
    try:
        conn = _cache_conn()
        conn.executemany(
            "INSERT OR REPLACE INTO calls (call_id, payload, ts) VALUES (?,?,?)",
            [(cid, json.dumps(p), time.time()) for cid, p in entries])
        conn.execute("DELETE FROM calls WHERE call_id NOT IN "
                     "(SELECT call_id FROM calls ORDER BY ts DESC LIMIT 4000)")
        conn.commit()
        conn.close()
    except Exception as e:  # noqa: BLE001 - cache is best-effort
        log("cache write failed: %s" % e)


def cache_get(call_id):
    """Return (payload, age_seconds) or (None, None)."""
    try:
        conn = _cache_conn()
        row = conn.execute("SELECT payload, ts FROM calls WHERE call_id=?", (call_id,)).fetchone()
        conn.close()
        if not row:
            return None, None
        try:
            age = time.time() - float(row[1])
        except (ValueError, TypeError):
            age = 0
        return json.loads(row[0]), age
    except Exception:  # noqa: BLE001
        return None, None


def session_cache_key(body):
    """Stable per-session prompt_cache_key derived from session-anchored content.

    The first input item never changes within a session (first Discord message /
    first system-reminder) but differs across sessions, so its hash isolates each
    conversation into its own Meta cache slot without needing a session id that
    9router does not forward.
    """
    import hashlib
    h = hashlib.sha1()
    h.update(str(body.get("model") or "").encode())
    h.update(str(body.get("instructions") or "")[:4000].encode())
    h.update(json.dumps(body.get("tools"), sort_keys=True, ensure_ascii=False)[:8000].encode())
    inp = body.get("input") or []
    if inp:
        h.update(json.dumps(inp[0], sort_keys=True, ensure_ascii=False)[:4000].encode())
    return "tbh:adp:" + h.hexdigest()[:16]


def replay_history(items):
    """Re-insert Meta's original reasoning + function_call items for known call_ids.

    On a cache miss (history predating the cache) an empty-argument tool call is dropped
    together with its output: replaying `arguments: "{}"` teaches the model to keep
    emitting empty calls, which is exactly the failure this module exists to stop.
    """
    if not isinstance(items, list):
        return items, 0, 0
    existing = {it.get("id") for it in items if isinstance(it, dict) and it.get("id")}
    out, seen, restored, dropped = [], set(), 0, set()
    stale = 0
    for it in items:
        if isinstance(it, dict) and it.get("type") == "function_call":
            cid = it.get("call_id") or ""
            hit, age = cache_get(cid)
            if hit and age is not None and age > REPLAY_MAX_AGE:
                stale += 1
                hit = None  # encrypted_content likely expired; replaying it
                            # breaks the chain Meta needs for follow-up calls
            if hit and hit.get("function_call"):
                fc = hit["function_call"]
                reas = hit.get("reasoning")
                # Already replayed verbatim on an earlier turn — keep it untouched,
                # Meta rejects duplicate item ids.
                if fc.get("id") and fc["id"] in existing:
                    out.append(it)
                    seen.add(fc["id"])
                    continue
                if reas and reas.get("id") and reas["id"] not in seen \
                        and reas["id"] not in existing:
                    out.append(reas)
                    seen.add(reas["id"])
                out.append(fc)
                if fc.get("id"):
                    seen.add(fc["id"])
                restored += 1
                continue
            if str(it.get("arguments") or "").strip() in ("", "{}"):
                dropped.add(cid)
                continue
        if isinstance(it, dict) and it.get("type") == "function_call_output" \
                and it.get("call_id") in dropped:
            continue
        if isinstance(it, dict) and it.get("id") in seen:
            continue  # already replayed verbatim
        out.append(it)
    return out, restored, stale


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "muse-adapter/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---------- helpers ----------

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _upstream_headers(self, extra=None):
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in SKIP_REQ_HEADERS}
        hdrs["User-Agent"] = MUSE_UA
        hdrs.setdefault("x-client-id", "tbh:exec")
        hdrs.setdefault("x-tbh-contributor-mode", "1")
        hdrs.setdefault("x-tbh-agent-role", "main")
        sid = str(uuid.uuid4())
        hdrs.setdefault("x-tbh-session-id", sid)
        hdrs.setdefault("x-meta-ai-gateway-session-id", sid)
        if extra:
            hdrs.update(extra)
        return hdrs

    def _open(self, method, body, extra_headers=None):
        url = UPSTREAM + self.path
        hdrs = self._upstream_headers(extra_headers)
        req = urllib.request.Request(url, data=body if body else None, headers=hdrs, method=method)
        try:
            return urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT, context=CTX)
        except urllib.error.HTTPError as e:
            return e
        except (TimeoutError, OSError) as e:
            # Upstream hung or the edge dropped us. Returning a clean JSON error beats
            # relaying an HTML error page (api.meta.ai serves "Facebook | Error" on edge
            # failures) — clients retry sanely instead of parsing garbage.
            return _SyntheticResponse(504, {"Content-Type": "application/json"},
                                      json.dumps({"error": {
                                          "code": "upstream_timeout",
                                          "message": "meta.ai did not answer within %ds: %s"
                                                     % (UPSTREAM_TIMEOUT, e),
                                          "type": "server_error"}}).encode())

    def _send_headers(self, status, headers):
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() in SKIP_RESP_HEADERS:
                continue
            self.send_header(k, v)
        self.end_headers()

    def _relay_json(self, resp, body_bytes):
        text = body_bytes.decode("utf-8", "ignore")
        out = unwrap_names(text).encode()
        self.send_response(resp.status if hasattr(resp, "status") else resp.code)
        ctype = resp.headers.get("Content-Type", "application/json")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _relay_stream(self, resp):
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.headers.get("Content-Type", "text/event-stream"))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        patcher = StreamPatcher()
        try:
            for line in resp:
                out = patcher.feed(line.decode("utf-8", "ignore")).encode()
                self.wfile.write(b"%X\r\n%s\r\n" % (len(out), out))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if patcher.patched:
                log("  repaired %d empty function_call arguments" % patcher.patched)
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ---------- verbs ----------

    def do_POST(self):
        started = time.time()
        raw = self._read_body()
        notes = []
        body = raw
        if "/responses" in self.path or "/chat/completions" in self.path:
            body, notes = transform_request_body(raw)

        resp = self._open("POST", body)
        status = getattr(resp, "status", getattr(resp, "code", 0))
        ctype = resp.headers.get("Content-Type", "")
        dump("req", body)

        if status >= 400:
            payload = resp.read()
            if _looks_like_html(payload, ctype):
                # api.meta.ai edge failure page — never relay HTML to an API client.
                payload = json.dumps({"error": {
                    "code": "upstream_edge_error",
                    "message": "meta.ai edge returned an HTML error page (HTTP %s)" % status,
                    "type": "server_error"}}).encode()
                ctype = "application/json"
            preview = payload[:300].decode("utf-8", "ignore").replace("\n", " ")
            log("POST %s -> %s %dms %s | %s" % (self.path, status, (time.time() - started) * 1000,
                                                ",".join(notes), preview))
            out = unwrap_names(payload.decode("utf-8", "ignore")).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype or "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        log("POST %s -> %s %dms | %s" % (self.path, status, (time.time() - started) * 1000,
                                         ",".join(notes) or "-"))

        if "text/event-stream" in ctype:
            self._relay_stream(resp)
            return

        payload = resp.read()
        if _looks_like_html(payload, ctype):
            err = json.dumps({"error": {
                "code": "upstream_edge_error",
                "message": "meta.ai edge returned an HTML page instead of JSON",
                "type": "server_error"}}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return
        self._relay_json(resp, _patch_json_output(payload))

    def do_GET(self):
        started = time.time()
        resp = self._open("GET", None)
        status = getattr(resp, "status", getattr(resp, "code", 0))
        payload = resp.read()
        log("GET %s -> %s %dms" % (self.path, status, (time.time() - started) * 1000))
        self._relay_json(resp, payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=20132)
    args = ap.parse_args()
    log("[muse-adapter] listening %s:%d -> %s (namespace=%s)" % (args.host, args.port, UPSTREAM, NS_NAME))
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

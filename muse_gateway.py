#!/usr/bin/env python3
"""
muse-gateway — entry shim so the Muse Code CLI can talk to 9router.

Why this exists
---------------
The Muse Code CLI hardcodes two things that a gateway cannot satisfy out of the box:

  1. It authenticates with its own Muse subscription key, which 9router rejects
     ("Invalid API key (requireApiKey=true)").
  2. It lists models from `GET /muse-code/models` (note: outside /v1), which
     plain 9router does not serve, and treats a 404 as "API key rejected".

This shim fixes both without touching the CLI:

  * rewrites Authorization to the 9router API key (read from a file at startup)
  * serves GET /muse-code/models from 9router's GET /v1/models
  * forwards /v1/responses and /v1/chat/completions to 9router unchanged

Chain: muse CLI -> muse-gateway :20133 -> 9router :20127 -> muse-adapter :20132 -> api.meta.ai

Run: python3 muse_gateway.py --host 127.0.0.1 --port 20133 --upstream http://127.0.0.1:20127
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("MUSE_GW_UPSTREAM", "http://127.0.0.1:20127")
KEY_FILE = os.environ.get("MUSE_GW_KEY_FILE", "/poki/tools/muse-adapter/9router.key")
LOG_PATH = os.environ.get("MUSE_GW_LOG", "/poki/tools/muse-adapter/gateway.log")

SKIP_REQ = {"host", "content-length", "accept-encoding", "connection", "content-encoding", "authorization"}
SKIP_RESP = {"transfer-encoding", "content-encoding", "connection", "content-length"}


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_key():
    try:
        with open(KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return os.environ.get("MUSE_GW_KEY", "")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "muse-gateway/1.0"

    def log_message(self, *a):
        pass

    def _hdrs(self):
        h = {k: v for k, v in self.headers.items() if k.lower() not in SKIP_REQ}
        key = load_key()
        if key:
            h["Authorization"] = "Bearer " + key
        return h

    def _fetch(self, method, path, body=None):
        url = UPSTREAM + path
        req = urllib.request.Request(url, data=body, headers=self._hdrs(), method=method)
        try:
            return urllib.request.urlopen(req, timeout=600), None
        except urllib.error.HTTPError as e:
            return e, None
        except Exception as e:  # noqa: BLE001
            return None, e

    def _relay(self, resp, body_bytes=None, ctype=None):
        status = getattr(resp, "status", getattr(resp, "code", 502))
        ctype = ctype or resp.headers.get("Content-Type", "application/json")
        if body_bytes is None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            return

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    # ---------- routes ----------

    def _models(self):
        """Serve Muse's /muse-code/models from 9router /v1/models."""
        resp, err = self._fetch("GET", "/v1/models")
        if err is not None:
            log("GET /muse-code/models -> 502 %s" % err)
            self._relay_json_error(502, str(err))
            return
        if getattr(resp, "status", 200) >= 400:
            payload = resp.read()
            log("GET /muse-code/models -> %d (from /v1/models)" % resp.status)
            self._relay(resp, payload)
            return
        try:
            data = json.loads(resp.read())
        except ValueError:
            self._relay_json_error(502, "9router /v1/models returned non-JSON")
            return
        out = []
        for m in data.get("data") or []:
            mid = m.get("id")
            if not mid:
                continue
            out.append({"id": mid, "object": "model", "created": m.get("created", 0),
                        "owned_by": m.get("owned_by", "9router")})
        log("GET /muse-code/models -> 200 (%d models)" % len(out))
        self._relay_json_error(200, {"object": "list", "data": out})

    def _relay_json_error(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        started = time.time()
        if self.path.rstrip("/").endswith("/muse-code/models"):
            self._models()
            return
        resp, err = self._fetch("GET", self.path)
        if err is not None:
            self._relay_json_error(502, {"error": str(err)})
            return
        log("GET %s -> %s %dms" % (self.path, getattr(resp, "status", "?"),
                                   (time.time() - started) * 1000))
        self._relay(resp)

    def do_POST(self):
        started = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        resp, err = self._fetch("POST", self.path, body)
        if err is not None:
            log("POST %s -> 502 %s" % (self.path, err))
            self._relay_json_error(502, {"error": str(err)})
            return
        status = getattr(resp, "status", 0)
        if status >= 400:
            safe = {k: ("***" if k.lower() in ("authorization", "x-api-key") else v)
                    for k, v in self.headers.items()}
            log("POST %s -> %s %dms HDRS=%s" % (self.path, status,
                                               (time.time() - started) * 1000, json.dumps(safe)))
        else:
            log("POST %s -> %s %dms" % (self.path, status, (time.time() - started) * 1000))
        if status >= 400:
            self._relay(resp, resp.read())
            return
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            self._relay(resp)
        else:
            self._relay(resp, resp.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=20133)
    args = ap.parse_args()
    log("[muse-gateway] listening %s:%d -> %s (key_file=%s, key=%s)"
        % (args.host, args.port, UPSTREAM, KEY_FILE, "set" if load_key() else "MISSING"))
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

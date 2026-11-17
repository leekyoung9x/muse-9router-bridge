"""Local sanitizer for Antigravity (Google Cloud Code) chat requests.

Google's Antigravity consumer endpoint rejects many requests whose
`systemInstruction` carries a third-party agent harness's system prompt: it
holds the request ~16 s, then returns 429 RESOURCE_EXHAUSTED with zero usage
(tokensIn=0) — even tiny prompts, even with full dashboard quota, while the
official `agy` client works. The identical text passes when it arrives as a
user message instead of a system instruction (verified by bisecting Hermes's
real 20 KB system prompt down to single sentences and by replaying the full
666-message payload both ways).

This proxy sits between 9router's `ags` custom provider and 9router's own
`ag/...` route: it replaces the system message with a neutral one-line system
prompt and re-injects the original system text as a leading user message
("[Session instructions]"), preserving every instruction while keeping it out
of `systemInstruction`. It holds NO key of its own: the Authorization header
(9router's own API key, set on the provider connection by register-9router.py)
is forwarded as-is, so the inner request is a normal authenticated 9router
call. Binds 127.0.0.1 only. See docs/antigravity-system-sanitize.md.
"""
import os, json, asyncio
from aiohttp import web, ClientSession, ClientTimeout

UPSTREAM = os.environ.get("AG_UPSTREAM", "http://127.0.0.1:20128/v1")
PORT = int(os.environ.get("AG_PROXY_PORT", "20135"))
SAFE_SYSTEM = os.environ.get("AG_SAFE_SYSTEM", "You are an AI assistant.")
UPSTREAM_MODEL = os.environ.get("AG_UPSTREAM_MODEL", "ag/gemini-4-hydrogen-medium")
DROP_REQ = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
DROP_RESP = {"content-length", "transfer-encoding", "connection", "content-encoding"}


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return ""


def sanitize(body):
    """Move system text out of the system role. Returns (new_body, moved_chars)."""
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return body, 0
    sys_texts, rest = [], []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system":
            t = text_of(m.get("content"))
            if t.strip():
                sys_texts.append(t)
        else:
            rest.append(m)
    joined = "\n\n".join(sys_texts).strip()
    if not joined or joined == SAFE_SYSTEM:
        return body, 0
    body = dict(body)
    body["messages"] = (
        [{"role": "system", "content": SAFE_SYSTEM},
         {"role": "user", "content": "[Session instructions]\n" + joined}]
        + rest
    )
    return body, len(joined)


async def handle(req: web.Request):
    path = req.rel_url.path
    if path.startswith("/v1"):
        path = path[3:]
    url = UPSTREAM + path + (("?" + req.rel_url.query_string) if req.rel_url.query_string else "")
    headers = {k: v for k, v in req.headers.items() if k.lower() not in DROP_REQ}
    body = await req.read()
    if req.method == "POST" and path.endswith("/chat/completions") and body:
        try:
            obj = json.loads(body)
            obj, moved = sanitize(obj)
            model = str(obj.get("model") or "")
            tail = model.split("/", 1)[-1]
            obj["model"] = ("ag/" + tail) if tail.startswith("gemini-") else UPSTREAM_MODEL
            body = json.dumps(obj).encode()
            headers["Content-Type"] = "application/json"
            print("ag-sanitize: model -> %s, moved %d system chars" % (obj["model"], moved), flush=True)
        except Exception as e:  # never block the call on a sanitize failure
            print("ag-sanitize: passthrough (parse error: %s)" % e, flush=True)
    sess: ClientSession = req.app["sess"]
    async with sess.request(req.method, url, headers=headers, data=body or None) as up:
        resp = web.StreamResponse(status=up.status)
        for k, v in up.headers.items():
            if k.lower() not in DROP_RESP:
                resp.headers[k] = v
        await resp.prepare(req)
        async for chunk in up.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        return resp


async def on_start(app): app["sess"] = ClientSession(timeout=ClientTimeout(total=None, sock_connect=15, sock_read=900))
async def on_stop(app): await app["sess"].close()

app = web.Application(client_max_size=64 * 1024 * 1024)
app.on_startup.append(on_start); app.on_cleanup.append(on_stop)
app.router.add_route("*", "/{tail:.*}", handle)
if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)

"""Local passthrough to OpenRouter that adds the app-attribution headers, so gateways
(9Router etc.) are seen as an 'agentic harness'. Holds NO API key: the caller's
Authorization header is forwarded as-is. Binds 127.0.0.1 only."""
import os, asyncio
from aiohttp import web, ClientSession, ClientTimeout

UPSTREAM = os.environ.get("OR_UPSTREAM", "https://openrouter.ai/api")
PORT = int(os.environ.get("OR_PROXY_PORT", "20134"))
INJECT = {
    "HTTP-Referer": os.environ.get("OR_REFERER", "https://hermes-agent.nousresearch.com"),
    "X-Title": os.environ.get("OR_TITLE", "Hermes Agent"),
    "X-OpenRouter-Categories": os.environ.get("OR_CATEGORIES", "productivity,cli-agent"),
}
DROP_REQ = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
DROP_RESP = {"content-length", "transfer-encoding", "connection", "content-encoding"}

async def handle(req: web.Request):
    url = UPSTREAM + req.rel_url.path_qs.replace("/v1", "/v1", 1)
    headers = {k: v for k, v in req.headers.items() if k.lower() not in DROP_REQ}
    for k, v in INJECT.items():
        headers[k] = v            # always override: caller's own values may be what's rejected
    body = await req.read()
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

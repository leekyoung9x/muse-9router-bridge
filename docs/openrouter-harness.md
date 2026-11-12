# OpenRouter "agentic harness only" models through 9router

## Symptom

```
HTTP 503: [openrouter/thinkingmachines/inkling-2-small:free] [403]:
{"error":{"message":"thinkingmachines/inkling-2-small:free is only available on agentic harnesses. ...",
 "code":403,"metadata":{"failed_routing_step":"Gate Free Endpoints by Agentic Harness"}}} (reset after 2m)
```

## Cause (measured, same key, same body)

OpenRouter gates some free models on the app-attribution header `HTTP-Referer`.
9router's built-in `openrouter` provider hard-codes `HTTP-Referer: https://endpoint-proxy.local`,
which is not a registered app.

| headers sent | result |
|---|---|
| none | 403 |
| `X-Title` only | 403 |
| `HTTP-Referer: https://endpoint-proxy.local` (9router default) | 403 |
| `HTTP-Referer: https://hermes-agent.nousresearch.com` | 200 |

The "(reset after 2m)" is 9router's model lock: after a 403 it keeps answering 503 for a while even
once the cause is fixed. See "Clearing a stuck lock".

## Fix without touching 9router's code

9router lets you add a custom OpenAI-compatible provider with its own base URL. We add one that points
at a tiny local proxy which sets the headers:

```
client --> 9router :20128 --(provider "orh", baseUrl 127.0.0.1:20134/v1)--> or-harness-proxy :20134 --> openrouter.ai
                                                                            (sets HTTP-Referer / X-Title / X-OpenRouter-Categories)
```

Everything lives outside 9router's install tree: the proxy is its own systemd unit and the provider is
a row in 9router's SQLite DB. **Upgrading 9router does not remove it.** (Patching the `endpoint-proxy.local`
string inside 9router's `server/chunks/*.js` also works but is overwritten on every update — don't.)

## Install

```bash
cd or-harness-proxy
sudo ./install.sh          # proxy unit + registers provider "orh" in the 9router DB + restarts 9router
```

Prerequisites: `python3` with `aiohttp`, and an **OpenRouter connection already added in the 9router
dashboard** (the key is reused from it; or export `OPENROUTER_API_KEY`). The key is never printed or
committed; the proxy itself stores no key.

Environment: `DB` (default `/root/.9router/db/data.sqlite`), `PORT` (20134), `INSTALL_DIR`
(`/opt/or-harness-proxy`), `UNIT_DIR`, `MODELS` (space-separated; default `thinkingmachines/inkling-2-small:free`),
`SKIP_SERVICE=1`, `SKIP_RESTART=1`. The DB is backed up to `data.sqlite.bak_orh.<ts>` first.
`install.sh` is idempotent: re-running updates the same node/connection, it does not duplicate.

Add more models later: `MODELS="a/b:free c/d" ./install.sh` (adds, keeps existing).

## Use

Model name is the provider prefix + the OpenRouter id:

```
orh/thinkingmachines/inkling-2-small:free
```

**Do not use `openrouter/...`** for these models — that is the built-in provider and still goes direct (403).
The dashboard's *Test* button on the `openrouter` provider also still tests the direct path.

## Verify

```bash
systemctl is-active or-harness-proxy
curl -s http://127.0.0.1:20134/v1/chat/completions -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"thinkingmachines/inkling-2-small:free","max_tokens":50,"messages":[{"role":"user","content":"hi"}]}'
# then through 9router with a 9router key and model "orh/thinkingmachines/inkling-2-small:free"
```

Tested end to end (9router -> proxy -> OpenRouter): plain, `stream:true` (SSE, ends with `[DONE]`) and a tool call
(arguments come back intact).

## Proxy settings (env on the unit)

`OR_PROXY_PORT` (20134), `OR_REFERER`, `OR_TITLE`, `OR_CATEGORIES` (defaults: Hermes values),
`OR_UPSTREAM` (`https://openrouter.ai/api`). It always **overrides** those three headers, binds
`127.0.0.1` only, and forwards the caller's `Authorization` untouched.

## Caveats

- This presents your traffic as a registered app. OpenRouter may tighten the check or change the
  allow-list; if it starts 403-ing again, change `OR_REFERER` to another listed app.
- OpenRouter prefixes the JSON body with whitespace/newlines. Strict parsers must `lstrip()` first
  (OpenAI SDKs and Hermes are fine).
- Respect the model's free-tier limits and OpenRouter's terms; this only fixes routing, not quota.

## Clearing a stuck lock

If 9router still says `reset after Ns` after the cause is fixed, clear the connection's lock and restart:

```python
import sqlite3, json
c = sqlite3.connect("/root/.9router/db/data.sqlite")
i, d = c.execute("select id,data from providerConnections where provider='openrouter'").fetchone()
d = json.loads(d)
for k in list(d):
    if k.startswith("modelLock_"): d[k] = None
d.update(testStatus="active", lastError=None, errorCode=None, lastErrorAt=None, backoffLevel=0)
c.execute("update providerConnections set data=? where id=?", (json.dumps(d), i)); c.commit()
```
then `systemctl restart 9router`.

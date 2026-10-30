# Install

Two ways: the script (fast) or manual (when you need to understand or adapt it).
Both end at the same state; the manual path is the reference.

- [0. Prerequisites](#0-prerequisites)
- [1. Get the Muse Code key](#1-get-the-muse-code-key)
- [2. Install the files](#2-install-the-files)
- [3. Start the two services](#3-start-the-two-services)
- [4. Point 9router at the adapter](#4-point-9router-at-the-adapter)
- [5. Create the model combo](#5-create-the-model-combo)
- [6. Write the 9router API key](#6-write-the-9router-api-key)
- [7. Verify layer by layer](#7-verify-layer-by-layer)
- [8. Configuration reference](#8-configuration-reference)
- [9. Docker-based 9router](#9-docker-based-9router)
- [10. Upgrade / uninstall](#10-upgrade--uninstall)

---

## 0. Prerequisites

| Need | Check |
|---|---|
| Python 3.8+ (stdlib only) | `python3 -V` |
| systemd | `systemctl --version` |
| 9router running | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:20127/v1/models` → `401` means alive (auth required) |
| Muse Code subscription key | `python3 -c "import json;print(json.load(open('$HOME/.config/muse/auth.json'))['providers']['meta']['api_key'][:8])"` |
| Free ports | `ss -ltn \| grep -E ':(20132\|20133)'` → empty |

Locate 9router's data directory — the SQL below needs it:

```bash
tr '\0' '\n' < /proc/$(pgrep -f '9router|custom-server' | head -1)/environ | grep DATA_DIR
# typical: DATA_DIR=/root/ai-services/data-9router
# DB:      <DATA_DIR>/db/data.sqlite
```

> Work on a **copy** of `data.sqlite` for inspection, and back it up before any `UPDATE`:
> `cp data.sqlite data.sqlite.bak-$(date +%s)`. Stop 9router or accept that it may cache
> settings in memory until restart.

## 1. Get the Muse Code key

```bash
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.config/muse/auth.json")
print(json.load(open(p))["providers"]["meta"]["api_key"])
PY
```

The key looks like `LLM|<id>|<secret>`. Treat it as a credential: it bills your subscription.

If the CLI is not logged in, run `muse login` first (device flow, needs a browser).

## 2. Install the files

```bash
sudo mkdir -p /opt/muse-bridge
sudo cp adapter.py muse_gateway.py /opt/muse-bridge/
```

Any directory works; the unit files below use `/opt/muse-bridge`. If you keep the repo elsewhere,
adjust `ExecStart` and `MUSE_CALL_CACHE`.

Create a service user if you do not want it running as root:

```bash
sudo useradd --system --home /opt/muse-bridge --shell /usr/sbin/nologin musebridge
sudo chown -R musebridge: /opt/muse-bridge
```

## 3. Start the two services

`/etc/systemd/system/muse-adapter.service`:

```ini
[Unit]
Description=Muse adapter — 9router → api.meta.ai
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# User=musebridge
ExecStart=/usr/bin/python3 /opt/muse-bridge/adapter.py --host 127.0.0.1 --port 20132
Restart=always
RestartSec=3
Environment=MUSE_CALL_CACHE=/opt/muse-bridge/call_cache.sqlite
# Environment=MUSE_UPSTREAM_TIMEOUT=180
# Environment=MUSE_NS_NAME=client
# Environment=MUSE_DUMP_DIR=/tmp/adapter-dumps   # debugging only

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/muse-gateway.service` (optional — only if you run the Muse Code CLI):

```ini
[Unit]
Description=Muse gateway — Muse Code CLI → 9router
After=network-online.target 9router.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/muse-bridge/muse_gateway.py --host 127.0.0.1 --port 20133
Restart=always
RestartSec=3
Environment=MUSE_GW_UPSTREAM=http://127.0.0.1:20127
Environment=MUSE_GW_KEY_FILE=/opt/muse-bridge/9router.key

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now muse-adapter muse-gateway
systemctl is-active muse-adapter muse-gateway      # active / active
journalctl -u muse-adapter -n 5 --no-pager         # "listening 127.0.0.1:20132 -> https://api.meta.ai"
```

## 4. Point 9router at the adapter

### Option A — dashboard (preferred)

9router UI → **Providers** → add / edit a custom **[OI] Compatible** provider:

| Field | Value |
|---|---|
| Name | `Muse` |
| API type | **Responses** (`apiType: responses` — *not* chat) |
| Base URL | `http://127.0.0.1:20132/v1` |
| API key | your `LLM|…` Muse key |
| Prefix | e.g. `muse` (becomes the model prefix) |

> `apiType: chat` forces 9router to call `/v1/chat/completions`, which Meta rejects for subscription
> keys and then locks the model for 120 s. Always `responses`.

### Option B — SQL

```sql
-- 1) provider node
INSERT INTO providerNodes (id, type, name, data, createdAt, updatedAt) VALUES (
  'openai-compatible-responses-muse',
  'openai-compatible',
  'Muse',
  '{"prefix":"muse","apiType":"responses","baseUrl":"https://api.meta.ai/v1"}',
  datetime('now'), datetime('now')
);

-- 2) connection (data.apiKey = your Muse key, providerSpecificData.baseUrl = the ADAPTER)
INSERT INTO providerConnections (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt) VALUES (
  'muse-conn-1',
  'openai-compatible-responses-muse',
  'apikey',
  'code',
  NULL,
  1,
  1,
  '{"defaultModel":"muse-spark-1.3-contributor","apiKey":"LLM|REPLACE_ME","testStatus":"active",
    "providerSpecificData":{"prefix":"muse","apiType":"responses",
                            "baseUrl":"http://127.0.0.1:20132/v1","nodeName":"Muse"}}',
  datetime('now'), datetime('now')
);
```

The **connection-level** `providerSpecificData.baseUrl` overrides the node-level one — that is the
single line that routes traffic through the adapter. Restart 9router afterwards.

Verify it took effect:

```bash
python3 - <<'PY'
import json, sqlite3
c = sqlite3.connect("/root/ai-services/data-9router/db/data.sqlite")
for cid, raw in c.execute("SELECT id, data FROM providerConnections"):
    d = json.loads(raw)
    b = (d.get("providerSpecificData") or {}).get("baseUrl", "")
    if "meta.ai" in b or "20132" in b:
        print(cid, "->", b)
PY
```

Expect `http://127.0.0.1:20132/v1`. If it still says `https://api.meta.ai/v1`, every request will
403.

## 5. Create the model combo

The combo is what clients call. Dashboard → **Combos**, or:

```sql
INSERT INTO combos (id, name, kind, models, createdAt, updatedAt) VALUES (
  'combo-muse-1', 'my-muse', NULL, '["muse/muse-spark-1.3-contributor"]',
  datetime('now'), datetime('now')
);
```

The prefix (`muse/`) must match the provider prefix from step 4. Restart 9router.

## 6. Write the 9router API key

The gateway needs a real 9router key (not `auth/cli-secret`, which cannot call chat):

```bash
python3 - <<'PY'
import os, sqlite3
db = "/root/ai-services/data-9router/db/data.sqlite"
c = sqlite3.connect(db)
row = c.execute("SELECT key FROM apiKeys WHERE isActive=1 ORDER BY createdAt LIMIT 1").fetchone()
assert row, "no active key — create one in the dashboard"
p = "/opt/muse-bridge/9router.key"
open(p, "w").write(row[0] + "\n")
os.chmod(p, 0o600)
print("wrote", len(row[0]), "chars to", p)
PY
```

> Do this in one Python process. Piping the key through shell output truncates it to a masked form
> (`sk-…`) and every call then 401s.

## 7. Verify layer by layer

Run `bash scripts/selftest.sh` or do it by hand:

**a) adapter reachable**

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:20132/v1/responses \
  -H 'Content-Type: application/json' -d '{}'
# 401 — reachable (it forwards and Meta rejects the empty call). Connection refused = not running.
```

**b) adapter fixes the gate** — this is the one that proves the whole design:

```bash
python3 - <<'PY'
import json, os, urllib.request
key = json.load(open(os.path.expanduser("~/.config/muse/auth.json")))["providers"]["meta"]["api_key"]
body = {"model": "muse-spark-1.3-contributor",
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "say OK"}]}],
        "tools": [{"type": "function", "name": "read_file", "description": "r",
                   "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}],
        "max_output_tokens": 64, "stream": False}
req = urllib.request.Request("http://127.0.0.1:20132/v1/responses",
                             data=json.dumps(body).encode(),
                             headers={"Authorization": "Bearer " + key,
                                      "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=60).status)   # 200
PY
# then: tail -1 adapter.log  ->  "... | tools->namespace(1)"
```

**c) through 9router**

```bash
python3 - <<'PY'
import json, urllib.request
key = open("/opt/muse-bridge/9router.key").read().strip()
body = {"model": "my-muse", "messages": [{"role": "user", "content": "say OK"}],
        "max_tokens": 512, "stream": False}
req = urllib.request.Request("http://127.0.0.1:20127/v1/chat/completions",
                             data=json.dumps(body).encode(),
                             headers={"Authorization": "Bearer " + key,
                                      "Content-Type": "application/json"})
r = urllib.request.urlopen(req, timeout=120)
print(r.status, json.loads(r.read())["choices"][0]["message"]["content"])
PY
```

> `max_tokens` below ~256 returns an empty `content` on reasoning models — the budget is spent on
> reasoning first. Use `>= 512` for smoke tests, or read `reasoning_content`.

**d) gateway (only for the CLI)**

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:20133/muse-code/models   # 200
```

**e) tool calls survive a round trip** — the check that catches the `arguments: "{}"` regression:

```bash
muse exec --base-url http://127.0.0.1:20133/v1 --model my-muse \
          --contributor-mode on --reasoning-effort low --workspace /tmp \
          'Read /etc/hostname with your read_file tool, then reply with the exact contents.'
# expect the hostname; "Cannot read ''" or a guardrail halt means the cache is not being replayed
grep -c replayed /opt/muse-bridge/adapter.log    # > 0 on the second turn
```

## 8. Configuration reference

### adapter (`adapter.py`)

| Env | Default | Meaning |
|---|---|---|
| `MUSE_UPSTREAM` | `https://api.meta.ai` | upstream base |
| `MUSE_UPSTREAM_TIMEOUT` | `180` | seconds before a hung upstream becomes a JSON 504 |
| `MUSE_NS_NAME` | `client` | namespace wrapper name (any value passes the gate) |
| `MUSE_NS_DESC` | `Client tool set.` | namespace description |
| `MUSE_UA` | `muse-build/1.4.14 (…)` | outbound User-Agent |
| `MUSE_CALL_CACHE` | `./call_cache.sqlite` | tool-call replay cache path |
| `MUSE_ADAPTER_LOG` | `./adapter.log` | request log |
| `MUSE_DUMP_DIR` | *(unset)* | when set, writes every request body here — debug only |

### gateway (`muse_gateway.py`)

| Env | Default | Meaning |
|---|---|---|
| `MUSE_GW_UPSTREAM` | `http://127.0.0.1:20127` | 9router base |
| `MUSE_GW_KEY_FILE` | `./9router.key` | file holding the 9router API key |
| `MUSE_GW_KEY` | *(empty)* | inline fallback if no key file |
| `MUSE_GW_LOG` | `./gateway.log` | request log |

## 9. Docker-based 9router

`127.0.0.1` inside a container is the container, not the host. Pick one:

- **host networking** for 9router (`network_mode: host`) — then `127.0.0.1:20132` works unchanged
- **bind the adapter on the docker bridge** and use the gateway IP:
  `ExecStart=… --host 0.0.0.0 --port 20132`, then set the connection `baseUrl` to
  `http://172.17.0.1:20132/v1` (or the `docker network inspect` gateway of 9router's network)
- **run the adapter as a sidecar container** in the same compose network and use its service name

Never publish `:20132` on a public interface — it forwards your subscription key upstream.

## 10. Upgrade / uninstall

**Upgrade** (no 9router change needed):

```bash
sudo cp adapter.py muse_gateway.py /opt/muse-bridge/
sudo systemctl restart muse-adapter muse-gateway
bash scripts/selftest.sh
```

**Uninstall:**

```bash
sudo systemctl disable --now muse-adapter muse-gateway
sudo rm /etc/systemd/system/muse-adapter.service /etc/systemd/system/muse-gateway.service
sudo systemctl daemon-reload
# point the 9router connection back, or Muse traffic stops working:
#   providerSpecificData.baseUrl -> https://api.meta.ai/v1   (403 returns; that is expected)
```

Removing the bridge without reverting `baseUrl` leaves 9router calling a dead port — every Muse
request then fails with a connection error, not a 403. Revert both or neither.

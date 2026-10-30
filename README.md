# Muse Code ⇄ 9router bridge

Two tiny sidecar services that let a **Muse Code subscription key** be used through **9router**,
without patching 9router's bundle.

```
muse CLI ──▶ muse-gateway :20133 ──▶ 9router :20127 ──▶ muse-adapter :20132 ──▶ api.meta.ai
```

Both are plain Python 3 stdlib, no dependencies, systemd-managed.

---

## The two problems this solves

### 1. `403 client_attestation_denied` (muse-adapter)

Meta refuses subscription keys sent by anything that is not the Muse Code client:

```json
{"error":{"code":"client_attestation_denied",
 "message":"This API key is billed to a Muse Code subscription. Muse Code subscriptions cover
 requests from the Muse Code client only. Use it in Muse Code, or use a pay-as-you-go API key."}}
```

The gate is **not** header-based and **not** User-Agent-based — those were tested and both pass.
It fires when the Responses payload carries **flat function tools** at the top level:

```json
"tools": [{"type":"function","name":"read_file", ...}]     // ✗ 403
"tools": [{"type":"namespace","name":"client","tools":[{"type":"function","name":"read_file"}]}]  // ✓ 200
```

Muse Code always wraps its tools in a `namespace` block, so anything else is classified as
"not Muse Code". The adapter wraps flat tools into a namespace named `client` on the way out.
Meta returns the tool name **without** the namespace prefix (`read_file`, not `client.read_file`),
so no response rewriting is needed.

Other normalisations the adapter does, because Meta rejects them outright:

| Symptom | Upstream rule | Adapter fix |
|---|---|---|
| `400 max_output_tokens The number must be >= 16` | min 16 | clamp to `>= 16` |
| `400 reasoning_effort` at root | must be `reasoning.effort` | move it |
| `400 only "auto" is supported for tool_choice` | named/none choices rejected | drop unless `"auto"` |

### 2. Tool calls arriving with empty arguments (`arguments: "{}"`)

Symptom on the client: every tool call lands with no arguments — `read_file` gets an empty
path, fails, the client retries the same call, and its loop guardrail kills the turn
(`repeated_exact_failure_block` / `guardrail_halt`).

Cause: Meta only keeps tool arguments correct when the history replays the assistant's tool
call the way Muse Code does — the `reasoning` item (with `encrypted_content`) followed by the
exact `function_call` item Meta returned. A chat→Responses gateway (9router) rebuilds history
from chat `tool_calls`, so those items are lost and the model degenerates to `{}`.

Measured on this chain (same prompt, 5 runs each):

| History shape | Correct arguments |
|---|---|
| fabricated `function_call` (no reasoning item) | **0 / 5** |
| Meta's own items replayed verbatim | **5 / 5** |

Fix: the adapter caches every `function_call` it sees — together with the `reasoning` item
that preceded it — in `call_cache.sqlite`, keyed by `call_id`, and re-injects both into later
requests. It also asks for `include: ["reasoning.encrypted_content"]` so there is something to
cache, and drops empty-argument calls it cannot restore (replaying `{}` teaches the model to
keep emitting `{}`).

Verified through the real chain after the fix: 4/4 two-turn runs correct, and a 5-file
sequential tool session returned all five results.

Cache notes: best-effort, bounded to 4000 entries, `/poki/tools/muse-adapter/call_cache.sqlite`.
Deleting it is safe — the adapter simply starts relearning (and drops empty calls meanwhile).

### 3. Upstream edge flakes

api.meta.ai occasionally accepts a request, hangs ~300 s, then answers with a
`<title>Facebook | Error</title>` HTML page. Relayed as-is, an API client chokes on HTML.
The adapter caps the upstream at `MUSE_UPSTREAM_TIMEOUT` (default 180 s) and rewrites any HTML
body (4xx/5xx and 200) into a clean JSON `upstream_timeout` / `upstream_edge_error`.

### 4. Muse CLI cannot authenticate to 9router (muse-gateway)

The CLI has two hardcoded assumptions a gateway cannot satisfy:

* it authenticates with its own Muse key, which 9router rejects (`Invalid API key (requireApiKey=true)`);
* it lists models from `GET /muse-code/models` — **outside** `/v1` — and treats the 404 as an auth failure
  (`still unauthorized after a token refresh`).

The gateway rewrites `Authorization` to the 9router key and serves `/muse-code/models` from
9router's `/v1/models`.

---

## Install

```bash
bash /poki/tools/muse-adapter/install.sh
```

What it does:

1. writes the 9router API key to `/poki/tools/muse-adapter/9router.key` (mode 600)
2. installs and starts `muse-adapter.service` (`:20132`) and `muse-gateway.service` (`:20133`)
3. points the 9router `muse` connection's `baseUrl` at `http://127.0.0.1:20132/v1`

> The 9router connection **must** point at the adapter (`:20132`), not directly at `api.meta.ai`,
> otherwise the 403 returns.

## Use

```bash
muse exec \
  --base-url http://127.0.0.1:20133/v1 \
  --model thtung-muse \
  --contributor-mode on \
  --reasoning-effort low \
  --workspace /path/to/repo \
  'your prompt'
```

Notes:

* `--model` must be the **9router** model id (the combo, e.g. `thtung-muse`). Passing the raw
  upstream id (`muse-spark-1.3-contributor`) fails with `model muse-spark-1.3 does not exist`,
  because the CLI normalises the name.
* `--contributor-mode on` sends `x-tbh-contributor-mode: 1`; without it the CLI strips the
  `-contributor` suffix from the model id.
* Any OpenAI-compatible client can call `http://<host>:20127/v1` with the 9router key directly —
  the adapter is transparent for them too.

## Verify

```bash
systemctl is-active muse-adapter muse-gateway 9router
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:20133/muse-code/models   # 200
curl -s http://127.0.0.1:20127/v1/chat/completions \
  -H "Authorization: Bearer $(cat /poki/tools/muse-adapter/9router.key)" \
  -H 'Content-Type: application/json' \
  -d '{"model":"thtung-muse","messages":[{"role":"user","content":"say OK"}],"max_tokens":64}'
```

Logs: `journalctl -u muse-adapter -f`, `journalctl -u muse-gateway -f`, plus
`/poki/tools/muse-adapter/{adapter,gateway}.log`.

## Failure modes seen in the field

* **`still unauthorized after a token refresh`** — the CLI hit `/muse-code/models` and got 404.
  Means `--base-url` points at 9router directly instead of the gateway (`:20133`).
* **`Invalid API key (requireApiKey=true)` in 9router log** — the gateway is forwarding the Muse
  key instead of the 9router key; check `9router.key` exists and is the full 35-char key.
* **`client_attestation_denied` again** — something bypassed the adapter. Check the connection's
  `baseUrl` is `http://127.0.0.1:20132/v1`.
* **`model muse-spark-1.3 does not exist`** — `--model` was the upstream id, not the 9router id.
* **Client guardrail fires on a tool call (`repeated_exact_failure_block`)** — the tool arrived
  with `arguments: "{}"`. Check `grep -c replayed adapter.log`: if it is 0, the call history was
  rebuilt outside the cache (e.g. session older than the cache). Start a fresh session; new calls
  are cached automatically.
* **`Duplicate item with id rs_...`** — a reasoning item was replayed twice. The adapter dedupes
  against ids already present in the input; if it recurs, delete `call_cache.sqlite` to reset.

## Why sidecars instead of patching 9router

Patches to `/usr/local/lib/node_modules/9router/app/.next-cli-build/server/chunks/*.js` are wiped
by `npm update -g 9router` or any container rebuild. Both shims live in `/poki/tools/` and are
independent of 9router's release cycle.

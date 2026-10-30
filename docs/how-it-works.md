# How it works

Everything below was established by bisecting real traffic against `api.meta.ai`, not by reading
docs — Meta publishes none for these behaviours.

- [1. Request lifecycle](#1-request-lifecycle)
- [2. The adapter is a reverse proxy, not a hook](#2-the-adapter-is-a-reverse-proxy-not-a-hook)
- [3. Outbound transformations](#3-outbound-transformations)
- [4. Response handling and SSE relay](#4-response-handling-and-sse-relay)
- [5. The tool-call cache](#5-the-tool-call-cache)
- [6. Why the attestation gate falls](#6-why-the-attestation-gate-falls)
- [7. The CLI gateway](#7-the-cli-gateway)
- [8. Edge flakes](#8-edge-flakes)
- [9. What would break it](#9-what-would-break-it)

---

## 1. Request lifecycle

```
client               9router              muse-adapter            api.meta.ai
  │                     │                      │                       │
  │ POST /v1/chat/…     │                      │                       │
  ├────────────────────►│                      │                       │
  │                     │ translate chat→responses                     │
  │                     │ POST /v1/responses   │                       │
  │                     ├─────────────────────►│                       │
  │                     │                      │ transform body        │
  │                     │                      │ POST /v1/responses    │
  │                     │                      ├──────────────────────►│
  │                     │                      │  SSE (event: data:)   │
  │                     │                      │◄──────────────────────┤
  │                     │                      │ patch lines, relay    │
  │                     │◄─────────────────────┤                       │
  │                     │ translate back       │                       │
  │◄────────────────────┤                      │                       │
```

Two important consequences:

1. **The adapter never sees the client's protocol.** By the time traffic reaches it, 9router has
   already converted chat→Responses. The adapter speaks exactly one dialect: the [OI] **Responses**
   API (`/v1/responses`). That is why its transformations are written against `input`, `tools`,
   `reasoning`, `max_output_tokens`.
2. **The adapter is stateless per request except for the call cache.** No session table, no affinity.

## 2. The adapter is a reverse proxy, not a hook

There is no interception trick. 9router's `muse` connection simply points at it:

```json
// providerConnections.data.providerSpecificData
"baseUrl": "http://127.0.0.1:20132/v1"      // instead of https://api.meta.ai/v1
```

9router believes it is talking to an [OI]-compatible provider. The adapter receives the request,
edits the JSON, forwards it to `https://api.meta.ai` + the same path, and streams the answer back.

The outbound call is plain `urllib`:

```python
url = UPSTREAM + self.path                      # https://api.meta.ai/v1/responses
req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
```

No `iptables`, no MITM certificate, no DNS override. Nothing to reinstall after a 9router upgrade.

## 3. Outbound transformations

`transform_request_body()` runs on `POST /v1/responses` (and `/v1/chat/completions`). Each step is
logged as a note on the request line, so `adapter.log` shows exactly what happened:

```
POST /v1/responses -> 200 968ms | tools->namespace(1),
dropped tool_choice={'type': 'function', 'name': 'read_file'},
reasoning_effort->reasoning.effort(low), max_output_tokens 8->16
```

### 3.1 Tools → namespace (the attestation fix)

```jsonc
// in  (403)
"tools": [
  {"type": "function", "name": "read_file", "parameters": {...}}
]

// out (200)
"tools": [
  {"type": "namespace", "name": "client", "description": "Client tool set.",
   "tools": [{"type": "function", "name": "read_file", "parameters": {...}}]}
]
```

Only *flat* function tools are moved. Tools already inside a `namespace`, or non-function tool
types, are left alone, and an empty/absent `tools` key is untouched.

Meta then returns tool names **prefixed with the namespace** (`client.read_file`); the adapter
strips the prefix on the way back so the client still sees `read_file`. Namespace name and
description are configurable (`MUSE_NS_NAME`, `MUSE_NS_DESC`).

### 3.2 `tool_choice`

Meta: `only "auto" is supported for tool_choice. "none", "required", and named function choices
are not currently supported`. The adapter keeps `"auto"` and drops everything else.

### 3.3 `reasoning_effort` → `reasoning.effort`

```jsonc
// in
{"reasoning_effort": "low"}
// out
{"reasoning": {"effort": "low"}}
```

If `reasoning` already exists as an object, the effort is merged in with `setdefault` so an
explicit `reasoning.effort` from the caller wins.

### 3.4 `max_output_tokens` floor

`16` is a hard minimum upstream (`The number must be >= 16`). Values below it are raised; values
above are untouched.

### 3.5 `include`

`reasoning.encrypted_content` is appended to `include` if missing — without it Meta omits the
encrypted reasoning payload, and §5 has nothing to replay.

### 3.6 History replay

Every `function_call` in `input` is looked up in the call cache; on a hit, Meta's original
`reasoning` + `function_call` items are re-inserted in place of the gateway-fabricated one.
On a miss with empty arguments, the call *and its output* are dropped — see §5.

### 3.7 Headers

The incoming `Authorization` (the Muse key) is forwarded unchanged — it is the real credential.
The adapter then adds/overrides:

| Header | Value | Purpose |
|---|---|---|
| `User-Agent` | `muse-build/1.4.14 (non-interactive; linux-x86_64; build …)` | match the CLI |
| `x-client-id` | `tbh:exec` | match the CLI |
| `x-tbh-contributor-mode` | `1` | match the CLI |
| `x-tbh-agent-role` | `main` | match the CLI |
| `x-tbh-session-id`, `x-meta-ai-gateway-session-id` | fresh UUID | per-request session |

**These headers are cosmetic.** Bisection showed 403 with none of them and 403 with all of them;
only the `tools` shape flips the gate (§6). They are kept so the outbound request is
indistinguishable from Muse Code in logs.

## 4. Response handling and SSE relay

### Streaming (`Content-Type: text/event-stream`)

The adapter does **not** buffer. It relays line by line with chunked transfer encoding and flushes
after each line, so time-to-first-token is unchanged:

```python
for line in resp:
    out = patcher.feed(line)                       # rewrite this line
    self.wfile.write(b"%X\r\n%s\r\n" % (len(out), out))
    self.wfile.flush()
```

`StreamPatcher` keeps per-response state and does two things:

1. **Un-prefix tool names** — `"name":"client.read_file"` → `"name":"read_file"`.
2. **Repair empty arguments.** Meta streams the arguments in
   `response.function_call_arguments.delta` / `.done`, but the *authoritative*
   `response.output_item.done` event frequently carries `"arguments": ""`. 9router trusts
   `output_item.done`, so the client receives a tool call with no arguments and calls e.g.
   `read_file` with an empty path. The patcher accumulates arguments from every event and writes
   the good value back into `output_item.done` and `response.completed`.

### Non-streaming (`application/json`)

Same repair over the whole `output` array (`_patch_json_output`), then the body is relayed with an
explicit `Content-Length`.

### Errors

Any HTML body — 4xx, 5xx, or even a 200 — is replaced with JSON before it reaches the client:

```json
{"error": {"code": "upstream_edge_error",
           "message": "meta.ai edge returned an HTML error page (HTTP 502)",
           "type": "server_error"}}
```

## 5. The tool-call cache

This is the only stateful part, and it fixes the nastiest failure mode.

### The failure

Symptom on the client: every tool call arrives as `arguments: "{}"`. `read_file` gets an empty
path, returns `Cannot read '': not a regular file`, the client retries the identical call, and its
loop guardrail aborts the turn (`repeated_exact_failure_block` → `guardrail_halt`).

### The cause

Meta keeps tool arguments coherent only when the conversation history replays the assistant's tool
call **exactly as Meta produced it** — the `reasoning` item (with `encrypted_content`) immediately
followed by the `function_call` item. Muse Code does this naturally because it stores the raw
items. A chat→Responses gateway rebuilds history from chat `tool_calls`, so both items are lost and
the model degenerates to `{}`.

Measured on this chain, identical prompt, 5 runs each:

| History shape | Correct arguments |
|---|---|
| fabricated `function_call`, no reasoning item | **0 / 5** |
| Meta's own items replayed verbatim | **5 / 5** |

### The fix

```
call_cache.sqlite
  calls(call_id TEXT PRIMARY KEY, payload TEXT, ts REAL)
  payload = {"reasoning": <item|null>, "function_call": <item>}
```

- on every response, each `function_call` is stored together with the `reasoning` item that
  preceded it in the same output (`StreamPatcher._remember_calls`)
- on every request, `replay_history()` swaps gateway-fabricated items for the cached originals
- entries are bounded to the newest 4000 and keyed by `call_id`
- an id already present in the incoming input is not re-added (Meta rejects
  `Duplicate item with id rs_…`)
- a cache miss with empty arguments drops the call *and* its `function_call_output`: replaying `{}`
  teaches the model to keep emitting `{}`, which is the opposite of the goal

Deleting the file is safe — the adapter relearns from new responses and drops unrecoverable calls
in the meantime. Sessions that started before the cache existed need a fresh start to benefit.

## 6. Why the attestation gate falls

The gate is **payload shape**, not identity. Evidence from live bisection:

| Variation | Result |
|---|---|
| no `User-Agent` | 403 |
| `User-Agent: muse-build/1.4.14 …` | 403 |
| `+ x-client-id`, `+ x-tbh-contributor-mode`, `+ x-tbh-agent-role`, `+ traceparent` | 403 (each, and all together) |
| `reasoning`, `prompt_cache_key`, `include`, `instructions`, `store`, `temperature`, `max_output_tokens` added/removed | 403 |
| `tools: []` or no `tools` key | **200** |
| flat `{"type":"function"}` tools | 403 |
| same tools wrapped in `{"type":"namespace"}` | **200** |

So: Meta treats "flat function tools at top level" as the signature of a non-Muse client. Wrapping
them is enough. No credential is forged and no signature is bypassed — the subscription key is
still the caller's real key.

## 7. The CLI gateway

The Muse Code CLI cannot talk to 9router directly, for two independent reasons:

1. **Auth.** It sends its own Muse key; 9router answers
   `Invalid API key (requireApiKey=true)`.
2. **Model listing.** It calls `GET /muse-code/models` — note, *outside* `/v1` — and treats a 404 as
   an auth failure, surfacing as `still unauthorized after a token refresh; run muse login again`.

`muse_gateway.py` (port 20133) fixes both without touching the binary:

- rewrites `Authorization` to the 9router API key read from `9router.key`
- serves `/muse-code/models` from 9router's `/v1/models`
- forwards `/v1/responses` and `/v1/chat/completions` untouched

CLI usage notes:

- `--model` must be the **9router** id (combo name). The raw upstream id
  (`muse-spark-1.3-contributor`) fails with `model muse-spark-1.3 does not exist` because the CLI
  normalises the name.
- `--contributor-mode on` is required, otherwise the CLI strips the `-contributor` suffix.

## 8. Edge flakes

api.meta.ai occasionally accepts a request, hangs ~300 s, then answers with a
`<title>Facebook | Error</title>` HTML page. Two guards:

- upstream timeout `MUSE_UPSTREAM_TIMEOUT` (default 180 s) → clean JSON `504 upstream_timeout`
- any HTML body → JSON `upstream_edge_error`

Without this, a client parses HTML as JSON, fails, and retries the same turn.

## 9. What would break it

- Meta switching to a **real per-client signature** instead of payload-shape checks. Nothing in this
  design can fake a signature; the adapter would need a different approach entirely.
- Meta renaming or removing the `namespace` tool type, or requiring a specific namespace name per
  tool (currently any name is accepted — `client` is arbitrary).
- Meta changing the reasoning-item replay requirement (e.g. accepting fabricated history again, or
  requiring an additional field). The cache would need the new field; the failure mode would be
  `arguments: "{}"` returning.
- 9router changing its Responses translation such that the adapter's input dialect changes.
  The adapter would then need updating at §3.

Each of these surfaces as one of the errors in
[docs/troubleshooting.md](troubleshooting.md) — start there.

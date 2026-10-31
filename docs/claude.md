# claude-adapter — Anthropic API key through 9router

`claude_adapter.py` is a stdlib-only Python sidecar between 9router and
`api.anthropic.com`. Python port of the `adapter.js` reference in
`vendor/claude-original/` (kept verbatim for provenance), so the whole bridge
stays one language with one installer.

## Problem

9router's old build forwards Claude Code's markers untouched on the outbound
request. Against an **API key** (`sk-ant-api...`, credit billing) Anthropic
rejects them:

```
400 Your credit balance is too low to access the Anthropic API.
```

even when the key has credit. Markers that trigger it:

| # | Marker | Where 9router puts it |
|---|---|---|
| 1 | `anthropic-beta: claude-code-20250219`, `oauth-2025-04-20` | beta header, forwarded as-is |
| 2 | `x-app`, `x-claude-code-*`, `x-stainless-*` headers | forwarded as-is |
| 3 | `system: "You are Claude Code, Anthropic's official CLI for Claude."` | prompt prefix |
| 4 | `Authorization: Bearer sk-ant-api...` | Anthropic wants `x-api-key` only for key auth |
| 5 | `?beta=` query param | forwarded as-is |

## Fix (what the adapter does)

- **Headers:** drop `x-app`, `anthropic-dangerous-direct-browser-access`, `host`,
  `content-length`, `connection`, all `x-claude-code*` / `x-stainless*`.
- **Auth:** `Bearer sk-ant-api...` → `x-api-key: sk-ant-api...` (only when it
  looks like an API key), then delete `Authorization`.
- **Betas:** remove `claude-code-20250219` and `oauth-2025-04-20` from the
  comma-separated `anthropic-beta` list, keep the rest
  (e.g. `prompt-caching-2024-07-31`); add `anthropic-version: 2023-06-01`
  if missing.
- **Body:** strip the `You are Claude Code, Anthropic's official CLI for
  Claude.` prefix from `system` (string form and `[{type:"text"}]` blocks).
  Entries that become empty are dropped.
- **URL:** delete the `?beta=` query param. Upstream is `CLAUDE_UPSTREAM`
  (default `https://api.anthropic.com`), full path forwarded, streaming
  passed through untouched.

## Wiring

```
client ──▶ 9router :20127 ──▶ claude-adapter :20129 ──▶ api.anthropic.com
```

The 9router **Anthropic connection** (`cc` node, `prefix: cl`) must have
`baseUrl = http://127.0.0.1:20129` — **no `/v1` suffix**, the adapter forwards
the full path. `install.sh` does this automatically (matches connections whose
baseUrl contains `anthropic.com`), clears their `modelLock_*` / error state
and marks them `active`, then restarts 9router.

Port default `20129` (same as the original `adapter.js`), overridable with
`CLAUDE_ADAPTER_PORT=... install.sh` or `--port` for a manual run.

## Verify

```bash
bash scripts/selftest.sh   # sections 9 (reachable) + 10 (connection repointed)
tail -5 claude_adapter.log # one line per request: METHOD path -> status
```

Expected: `POST /v1/messages` with an empty body returns **401** (reached
Anthropic, key missing) — not 400 and not a connection error.

# Muse Code ⇄ 9router bridge

Use a **Muse Code subscription key** through **9router** (or any OpenAI-compatible gateway),
without patching 9router's bundle.

Meta gates those keys with `403 client_attestation_denied` and quietly degrades tool calls when
the request does not look like it came from Muse Code. This repo is two small stdlib-only Python
sidecars that repair the traffic in flight and leave 9router untouched — so `npm update -g 9router`
cannot wipe the fix.

```
Any OpenAI-compatible client            Muse Code CLI
 (Hermes, Claude Code, curl, app)      (muse exec)
             │                              │
             │                     muse-gateway :20133   ← CLI entry only
             │                              │
             └──────────────┬───────────────┘
                            ▼
                     9router :20127
                            ▼
                   muse-adapter :20132
                            ▼
                      api.meta.ai
```

| Service | Port | Who uses it | Job |
|---|---|---|---|
| `muse-adapter` | 20132 | **everything** (sits behind 9router) | attestation fix, parameter normalisation, tool-argument repair, edge-flake handling |
| `muse-gateway` | 20133 | Muse Code CLI only | rewrites the CLI's auth to the 9router key, serves `GET /muse-code/models` |

Normal clients never touch the gateway — they call 9router directly and the adapter is already on
the outbound path. Only the Muse Code CLI needs the extra hop, because it hardcodes its own key and
polls an endpoint that 9router does not serve.

## Quickstart

```bash
# 1. install both services + point 9router's muse connection at the adapter
bash install.sh

# 2. verify every layer
bash scripts/selftest.sh

# 3. use it from the Muse Code CLI
muse exec --base-url http://127.0.0.1:20133/v1 \
          --model <9router-combo> --contributor-mode on \
          --workspace /path/to/repo 'your prompt'
```

## What it fixes

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | `403 client_attestation_denied` | Meta only accepts payloads whose `tools` are wrapped in a `namespace` block, the way Muse Code sends them | wrap flat tools into `{"type":"namespace","name":"client",...}` |
| 2 | `400 max_output_tokens must be >= 16` | Meta enforces a floor | clamp |
| 3 | `400 unknown parameter reasoning_effort` | Meta wants `reasoning.effort` | move it |
| 4 | `400 only "auto" is supported for tool_choice` | Meta rejects named/none choices | drop unless `auto` |
| 5 | tool calls arrive with `arguments: "{}"` → client loop guardrail kills the turn | Meta needs its own `reasoning` + `function_call` items replayed in history; a chat→Responses gateway cannot rebuild them | cache Meta's items per `call_id` and re-inject them |
| 6 | CLI: `still unauthorized after a token refresh` | CLI reads `GET /muse-code/models`, which 9router does not serve | gateway serves it from `/v1/models` |
| 7 | ~300 s hang, then a `Facebook \| Error` HTML page | api.meta.ai edge flake | 180 s cap + HTML→JSON rewrite |

Details and measurements: [docs/how-it-works.md](docs/how-it-works.md).

## Docs

- **[docs/how-it-works.md](docs/how-it-works.md)** — request lifecycle, every transformation with
  before/after payloads, SSE relay, the tool-call cache, why the gate falls, what breaks it.
- **[docs/install.md](docs/install.md)** — prerequisites, manual step-by-step install (no script),
  9router provider setup (UI and SQL), configuration reference, verification, upgrade, uninstall,
  Docker notes.
- **[docs/troubleshooting.md](docs/troubleshooting.md)** — error string → cause → fix.
- **[docs/quickstart.vi.md](docs/quickstart.vi.md)** — bản tóm tắt tiếng Việt.

## Layout

```
adapter.py              reverse proxy in front of api.meta.ai (the actual fix)
muse_gateway.py         entry shim for the Muse Code CLI only
install.sh              installs both units, writes the key, repoints 9router
scripts/selftest.sh     walks every layer and reports the failing one
scripts/normalize-cache.py  backfills nested Meta cache counters to top-level
                        cached_tokens so /dashboard/usage shows Muse hits
config.env.example      every knob, documented
docs/                   mechanism, install, troubleshooting, Vietnamese quickstart
```

`install.sh` honours `INSTALL_DIR`, `DB`, `ADAPTER_PORT`, `GATEWAY_PORT`, `ADAPTER_URL`,
`UNIT_DIR`, `SKIP_RESTART` — see the header of the script.

## Requirements

- Python 3.8+ (stdlib only — no pip installs)
- systemd (units are provided; anything that can run two long-lived processes works too)
- 9router reachable on `127.0.0.1:20127` (or set `MUSE_GW_UPSTREAM`)
- A Muse Code subscription key (`LLM|…`, from `~/.config/muse/auth.json`)

## Limits — read before relying on it

This works because Meta's gate **inspects payload shape**, not client identity: no signature, no
attestation token. We proved that by bisecting headers and body fields (see how-it-works §6). If
Meta ever moves to a real per-client signature, this approach breaks and the adapter must be
revisited — it is not a cryptographic bypass.

Also:

- the tool-call cache is per-host state (`call_cache.sqlite`); a call made before the cache existed
  cannot be repaired, only dropped (a fresh session fixes it)
- `namespace`-wrapped tools change the tool *names* Meta sees; the adapter strips the prefix on the
  way back, so clients keep their own names
- it proxies `POST /v1/responses`, `POST /v1/chat/completions` and `GET` passthrough only

## Security

- the 9router API key lives in `9router.key` (mode 600) — never commit it
- both services bind `127.0.0.1` by default; do not expose `:20132` publicly, it forwards your
  subscription key upstream
- `MUSE_DUMP_DIR` writes full request bodies to disk when set — debugging only, leave it unset

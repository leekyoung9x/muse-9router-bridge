# Troubleshooting

Start with the layer that produced the error, not the client.

| # | Error / symptom | Layer | Cause | Fix |
|---|---|---|---|---|
| 1 | `403 client_attestation_denied` — *"This API key is billed to a Muse Code subscription…"* | upstream | request did not go through the adapter, or the adapter did not wrap the tools | check the connection `baseUrl` is `http://127.0.0.1:20132/v1`; check `adapter.log` shows `tools->namespace(N)` |
| 2 | `403` only when tools are present, 200 without them | upstream | tools are flat `{"type":"function"}` | this is the gate — make sure the adapter is on the path (see #1) |
| 3 | `400 max_output_tokens The number must be >= 16` | upstream | client sent a smaller budget and bypassed the adapter | route through the adapter |
| 4 | `400 unknown parameter reasoning_effort` | upstream | top-level `reasoning_effort` reached Meta | adapter moves it to `reasoning.effort`; if it persists, the request bypassed the adapter |
| 5 | `400 only "auto" is supported for tool_choice` | upstream | named/none `tool_choice` | adapter drops it; a caller that needs forced tool use must be changed client-side |
| 6 | Tools arrive with `arguments: "{}"`; client guardrail fires (`repeated_exact_failure_block`, `guardrail_halt`); `read_file` fails with `Cannot read ''` | adapter | history was rebuilt without Meta's `reasoning` + `function_call` items | check `grep -c replayed adapter.log`; `0` means the call predates the cache — start a fresh session. New calls are cached automatically |
| 7 | `Duplicate item with id rs_…` | upstream | a reasoning item was replayed twice (two runs shared a call_id) | the adapter dedupes against ids already in the input; if it still happens, delete `call_cache.sqlite` |
| 8 | `Duplicate item with id …` after copying a cache between hosts | adapter | same as #7 | do not share `call_cache.sqlite` between hosts |
| 9 | Client hangs ~180 s then gets `504 upstream_timeout` | upstream | api.meta.ai edge flake | retry; if constant, lower `MUSE_UPSTREAM_TIMEOUT` or check network egress |
| 10 | `502 upstream_edge_error` | upstream | edge returned an HTML page (`Facebook \| Error`) instead of JSON | retry; the adapter already converted it to JSON so the client can retry cleanly |
| 11 | CLI: `still unauthorized after a token refresh; run muse login again` | gateway | `--base-url` points at 9router (`:20127`) instead of the gateway (`:20133`) | use `--base-url http://127.0.0.1:20133/v1` |
| 12 | 9router log: `Invalid API key (requireApiKey=true)` | gateway | the gateway forwarded the Muse key instead of the 9router key | check `9router.key` exists, is mode 600, and is the full 35-char key (see install §6) |
| 13 | CLI: `model muse-spark-1.3 does not exist or you lack access` | gateway | `--model` was the upstream id | pass the 9router combo id, and add `--contributor-mode on` |
| 14 | CLI: `failed to fetch model catalog: transport error` | gateway | gateway not running / wrong port | `systemctl is-active muse-gateway`; `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:20133/muse-code/models` → 200 |
| 15 | Empty `content` with HTTP 200 | upstream | reasoning model spent the budget on reasoning | raise `max_tokens` to `>= 512` or read `reasoning_content` |
| 16 | `Connection refused` on `:20132` | adapter | service down | `systemctl status muse-adapter`; `journalctl -u muse-adapter -n 30` |
| 17 | Everything works, then breaks after `npm update -g 9router` | 9router | 9router rewrote its data dir / dropped the custom provider | re-check the connection `baseUrl`; the adapter itself needs no change — that is the point of the sidecar design |

## Diagnostic commands

```bash
# what did the adapter do to the last request?
tail -3 /opt/muse-bridge/adapter.log

# is the tool-call cache actually being used?
grep -c replayed /opt/muse-bridge/adapter.log

# did anything reach the adapter at all?
journalctl -u muse-adapter --since '-5min' --no-pager | grep POST

# where is 9router sending Muse traffic?
python3 - <<'PY'
import json, sqlite3
c = sqlite3.connect("/root/ai-services/data-9router/db/data.sqlite")
for cid, raw in c.execute("SELECT id, data FROM providerConnections"):
    d = json.loads(raw)
    b = (d.get("providerSpecificData") or {}).get("baseUrl", "")
    if b:
        print(cid, "->", b)
PY

# what does 9router think happened?
journalctl -u 9router --since '-10min' --no-pager | grep -E 'COMBO|ERROR|403|401'

# capture full request bodies for one run (debug only, then unset!)
sudo mkdir -p /tmp/adapter-dumps
sudo systemctl set-environment MUSE_DUMP_DIR=/tmp/adapter-dumps
sudo systemctl restart muse-adapter
# …reproduce…
ls -t /tmp/adapter-dumps | head
sudo systemctl unset-environment MUSE_DUMP_DIR && sudo systemctl restart muse-adapter
```

## Rules of thumb

- **403 with a Muse key = payload shape.** Never chase credentials or headers first; confirm the
  request went through the adapter (`tools->namespace` in the log) before anything else.
- **`arguments: "{}"` = history replay.** Not a model-quality problem, not a prompt problem.
- **A 401 from 9router is auth, a 403 from Meta is shape, a 404 on `/muse-code/models` is the
  gateway.** Three different layers, three different fixes.
- **Empty content with 200 is a token budget**, not a bridge failure.
- When in doubt, run `scripts/selftest.sh` — it walks every layer and prints the failing one.

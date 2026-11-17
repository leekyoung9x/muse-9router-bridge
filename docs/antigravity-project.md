# Antigravity through 9router vs the official `agy` CLI

## Symptom

Combo request to `ag/gemini-4-hydrogen-medium` (664 messages, 21 tools, stream) failed after 16 s:

```
429 {"error":{"code":429,"message":"Resource has been exhausted (e.g. check quota).","status":"RESOURCE_EXHAUSTED"}}
```

…while the official `agy` CLI, signed in with the **same Google account**, answered fine, and the
account's Antigravity quota screen showed every bucket at 0/1,000 used.

## What was ruled out (all measured against the live 9router)

- **Subscription quota**: weekly and 5 h buckets 0/1,000 used. (Note: that screen lists six buckets —
  Gemini, Gemini 4 Argon, Claude & GPT, Flash Image — and **no Hydrogen bucket**, so it cannot prove
  Hydrogen quota either way.)
- **Request size/shape**: the exact failed shape replayed (665 messages, 856 KB, stream, 21 tools,
  reasoning medium) returned 200 in 3.6 s; a 390k-token single prompt also returned 200. The 429 was
  the only one in the whole Antigravity history, single account, 16 s upstream latency, no quota
  details attached — a per-project capacity/quota-window event, not account exhaustion.

## The structural difference

| | `agy` CLI (official) | 9router `antigravity` provider |
|---|---|---|
| project in payload | `default-cli-project`, resolved dynamically (`Backend project ID updated dynamically to: default-cli-project`) | `projectId` stored at OAuth time from `loadCodeAssist` → `cloudaicompanionProject.id` = **`aicode-consumers`** |
| quota handling | refreshes quota around startup/model select (`quota_manager doRefreshQuota`) | `[AG_QUOTA]` refresh only *after* a 429; combo then locks the model briefly and falls to the next model/account |
| model list | `fetchAvailableModels` per account | static provider model list; an account without the model returns 404 (measured: second account 404s on all `gemini-4-*`) |
| payload extras | native session/tools | tool-name sanitising, `toolConfig VALIDATED`, `safetySettings` stripped, thought-signature patching, `maxOutputTokens` clamped to 64k |

Google buckets quota/capacity per (account, project), so the project mismatch is the difference that
matters. Same client ID, same endpoint (`daily-cloudcode-pa.googleapis.com/v1internal`), same
`userAgent: "antigravity"` payload field.

## Fix

Set `projectId` to `default-cli-project` on every Antigravity connection in 9router's DB
(9router reads connections per request, so no restart is needed; token refresh preserves the stored
projectId). The dashboard re-login flow re-stores `aicode-consumers`, so run the guard on a timer:

```bash
python3 scripts/fix-antigravity-project.py --db /root/.9router/db/data.sqlite          # dry-run
python3 scripts/fix-antigravity-project.py --db /root/.9router/db/data.sqlite --apply
```

Install as a timer (units in `scripts/`, adjust paths):

```bash
cp scripts/fix-antigravity-project.py /opt/muse-bridge/scripts/
cp scripts/fix-antigravity-project.{service,timer} /etc/systemd/system/
systemctl enable --now fix-antigravity-project.timer
```

Verified live: after `--apply`, the next request's `requestDetails.providerRequest.project` is
`default-cli-project` and the 665-message replay returns 200.

## What this does not fix

A guard on the DB cannot replicate agy's in-client behaviour (pre-flight quota refresh, waiting out a
429 inside the client). If 429s recur on the correct project, the remaining option is a real
antigravity adapter (custom `agp/` provider → local adapter → `v1internal`, reusing the stored refresh
token, retrying 429 with the upstream reset delay instead of failing the combo) — same pattern as
`or-harness-proxy/`, but a much larger build. Not implemented; the project fix plus combo fallback has
been sufficient so far.

Backup before editing the DB by hand: `cp data.sqlite data.sqlite.bak`.

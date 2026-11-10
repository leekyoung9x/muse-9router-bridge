# Agent "stops" after starting a long background job (not an adapter bug)

**Symptom.** A Muse Spark session launches a build or test in the background, writes something like
*"this takes 10-15 minutes, I'll report when done"*, and the session goes silent. The chat shows a red X
instead of a completed check. Nothing ever follows up, even hours later.

**It is not the adapter.** In the incident that prompted this page the adapter log showed `200` for every
request, the stateful chain was intact, and Hermes logged
`Turn ended: reason=text_response(finish_reason=stop) api_calls=13/150`. The model emitted a plain-text
answer, which ends a Hermes turn. The background job had actually died one second after launch
(`EXIT:1`), and nothing told the session.

## Why nothing woke the session (three faults stacked)

1. **stdout was empty.** The command was `... > /tmp/build.log 2>&1; echo "EXIT:$?" >> /tmp/build.log`.
   Hermes `watch_patterns` match process stdout, so `["error","failed","Build xong"]` never matched, not even
   when the log contained `error`.
2. **`notify_on_complete` was not set.** Without it Hermes sends no message when the process exits.
3. **The exit code was masked.** The command ended with `echo`, so the process always exited 0 even though
   the build exited 1.

After that the model ended its turn with a promise instead of re-reading the log 25 s later, so it never
noticed the build was dead.

## Measured, not assumed (Hermes `state.db`, every background `terminal` call)

| model | background calls | did NOT set any completion notice |
|---|---|---|
| deepseek-v4.1-flash | 36 | 1 (3%) |
| claude | 117 | 9 (8%) |
| thtung-paid | 3 | 0 |
| **muse-spark-1.3** | 11 | **6 (55%)** |

All models sometimes end their turn after launching a background job (deepseek 7/16, claude 6/10). The
difference is that the others almost always armed a completion notice first, so ending the turn was safe:
Hermes wakes the session when the process exits. Muse often did not. Sample for Muse is small (11); the
direction is clear, the exact percentage is not.

`notify` / `notify_on_complete` is a **model-chosen tool argument**. Nothing in Hermes `config.yaml` forces it.
(`process_wait_auto_notify` only promotes it after the model polls twice with no new output.)

## What is guaranteed vs what is only improved

| layer | how | guarantee |
|---|---|---|
| Hermes forces `notify_on_complete=true` for every background command | patch in `tools/terminal_tool_background.py` | **absolute for notification**: a finished or dead job always wakes the session |
| Adapter appends a reminder to `instructions` (`MUSE_AGENT_HINT=1`) | `adapter.py` | **probabilistic**: lowers the chance the model forgets; the model can still ignore it |
| Out-of-model watcher that resumes idle sessions | not built | would be needed for a hard guarantee that the model never abandons work mid-task |

Even with the first row, the adapter cannot promise the model *acts correctly when woken* (reads the exit code,
fixes the failure) or never ends a turn early for unrelated reasons. That is model behaviour. Measure it; do not
claim it is solved.

## Does the reminder work? (same prompt, 8 trials each, live Meta)

A task that asks Muse to start a ~12 min build in the background, with a `terminal` tool exposing `notify`,
`notify_on_complete` and `watch_patterns`:

| | armed a completion notice | set none |
|---|---|---|
| `MUSE_AGENT_HINT=0` | 5/8 | 3/8 |
| `MUSE_AGENT_HINT=1` | 8/8 | 0/8 |

Small sample, so read it as "clearly helps", not "always works".

## Enabling the adapter reminder

```
# /etc/systemd/system/muse-adapter.service.d/agent-hint.conf
[Service]
Environment=MUSE_AGENT_HINT=1
# optional: Environment=MUSE_AGENT_HINT_TEXT=...your wording...
```

`systemctl daemon-reload && systemctl restart muse-adapter`. Default is **off** in the repo.

The hint is added **after** the per-session `prompt_cache_key` is computed (the key hashes `instructions`), so
turning it on or off never changes the cache slot or the stateful chain key. This is covered by a test.
Restarting the adapter makes live sessions re-bootstrap their chain once (tested; no work is lost).

## Rules for anyone launching background jobs from an agent

- Run with `notify_on_complete=true`. Do not redirect all stdout to a file and rely on `watch_patterns`.
- Keep the real exit code: `set -o pipefail; cmd 2>&1 | tee log; exit ${PIPESTATUS[0]}`.
- ~25 s after launch, check the process is alive and the log is growing. A job that exits within seconds is an
  environment error (user, permissions, licence, `TMPDIR`), not a long build.
- Never end the turn with "I'll report when done" unless a completion notice is armed.

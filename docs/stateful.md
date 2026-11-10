# Stateful chaining (experimental, opt-in)

Meta expires `reasoning.encrypted_content` after ~45 min, which breaks the
call_cache replay on long agent sessions. `stateful.py` instead chains turns
with `store:true` + `previous_response_id`, sending only NEW input items.

Enable (default OFF):
- `META_STATEFUL_ENABLED=1` in the muse-adapter environment
- `stateful.allow` (hot-reloaded): one session-key prefix per line, `*` = all

Behaviour: per-session chain in `stateful.sqlite`; retries with no new items
fall back to the classic replay path; a dead response id clears the chain and
retries once with the client's full history; a progress checkpoint is written
to `checkpoints/` each turn (recovery does not read it yet).

Verified: 2-turn text memory, 25-file/28-turn tool loop, 2 parallel sessions,
dead-rid recovery, 72-min 22-turn real coding task (chain stayed valid past
45 min). Not verified: Hermes context compaction mid-chain, long idle gaps.

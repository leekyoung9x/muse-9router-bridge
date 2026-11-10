"""Stateful chaining for the Muse adapter (EXPERIMENTAL, opt-in).

Problem: Meta's Responses API needs each turn's history to replay the exact
`reasoning` + `function_call` items it produced. The adapter's call_cache
replay handles that, but Meta expires reasoning `encrypted_content` after
~45 min, so long agent sessions degenerate into empty-argument tool calls.

This module implements the alternative: server-side history via
`previous_response_id` + `store:true`. Instead of resending the full rebuilt
history every turn, the adapter sends only NEW input items and points Meta at
the previous response. No replay needed, nothing to expire client-side.

OPT-IN ONLY. Engaged per session, never globally by accident:
  META_STATEFUL_ENABLED=0/1   (default 0 — whole feature off)
  META_STATEFUL_ALLOW_FILE    (default /opt/muse-bridge/stateful.allow)
      one session-key prefix per line, '#' comments, '*' = all sessions.
      Hot-reloaded on mtime change, so sessions can be enrolled without
      restarting the adapter.

Session identity reuses the adapter's prompt_cache_key
(`tbh:adp:<sha1(model|instructions|tools|first-input-item)>`), so two
parallel Hermes sessions get two isolated chains automatically.

Modes returned by apply():
  'passthrough'  client already drives its own previous_response_id — untouched.
  'chained'      rid attached, input reduced to new items, store forced on.
  'bootstrap'    no chain yet — full history sent once with store on so the
                 response becomes the chain head. Classic replay still runs.
  'stateless'    chaining not applicable (non-list input / empty delta) —
                 request left for the classic replay path.
"""

import hashlib
import json
import os
import sqlite3
import threading
import time

ENABLED_ENV = "META_STATEFUL_ENABLED"
DB_ENV = "META_STATEFUL_DB"
ALLOW_ENV = "META_STATEFUL_ALLOW_FILE"

DEFAULT_DB = "/opt/muse-bridge/stateful.sqlite"
DEFAULT_ALLOW = "/opt/muse-bridge/stateful.allow"

_lock = threading.Lock()
_allow_cache = {"mtime": 0, "entries": ()}


def enabled():
    return os.environ.get(ENABLED_ENV, "0").strip().lower() in ("1", "true", "yes", "on")


def _db_path():
    return os.environ.get(DB_ENV, DEFAULT_DB)


def _allow_path():
    return os.environ.get(ALLOW_ENV, DEFAULT_ALLOW)


def _conn():
    d = _db_path()
    try:
        os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
    except OSError:
        pass
    c = sqlite3.connect(d, timeout=10, check_same_thread=False)
    c.execute("CREATE TABLE IF NOT EXISTS chains"
              "(sk TEXT PRIMARY KEY, rid TEXT, turn INTEGER DEFAULT 0, updated REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS sent"
              "(sk TEXT, h TEXT, turn INTEGER, PRIMARY KEY(sk, h))")
    return c


def allow_entries():
    """Hot-reloaded allowlist of session-key prefixes. () = nobody."""
    p = _allow_path()
    try:
        mt = os.stat(p).st_mtime_ns
    except OSError:
        return ()
    c = _allow_cache
    if mt != c["mtime"]:
        try:
            with open(p) as f:
                entries = tuple(sorted(
                    ln.strip() for ln in f
                    if ln.strip() and not ln.strip().startswith("#")))
        except OSError:
            entries = ()
        c["mtime"] = mt
        c["entries"] = entries
    return c["entries"]


def decide(sk):
    """True if this session is enrolled in stateful chaining."""
    if not enabled() or not sk:
        return False
    allow = allow_entries()
    if not allow:
        return False
    if "*" in allow:
        return True
    return any(sk.startswith(p) for p in allow)


def fp(item):
    h = hashlib.sha1()
    try:
        h.update(json.dumps(item, sort_keys=True, ensure_ascii=False,
                            separators=(",", ":")).encode())
    except (TypeError, ValueError):
        h.update(str(item).encode())
    return h.hexdigest()[:16]


def get_chain(sk):
    with _lock:
        try:
            c = _conn()
            row = c.execute("SELECT rid FROM chains WHERE sk=?", (sk,)).fetchone()
            c.close()
        except Exception:
            return None
    return row[0] if row else None


def record(sk, rid):
    """Advance the chain head. Only adapter-managed responses (store forced on)."""
    if not sk or not rid or not str(rid).startswith("resp_"):
        return False
    with _lock:
        try:
            c = _conn()
            c.execute("INSERT INTO chains(sk, rid, turn, updated) VALUES(?,?,1,?)"
                      " ON CONFLICT(sk) DO UPDATE SET rid=excluded.rid,"
                      " turn=chains.turn+1, updated=excluded.updated",
                      (sk, rid, time.time()))
            # forget sessions idle > 7 days
            c.execute("DELETE FROM chains WHERE updated < ?", (time.time() - 7 * 86400,))
            c.commit()
            c.close()
        except Exception:
            return False
    return True


def clear(sk):
    with _lock:
        try:
            c = _conn()
            c.execute("DELETE FROM chains WHERE sk=?", (sk,))
            c.execute("DELETE FROM sent WHERE sk=?", (sk,))
            c.commit()
            c.close()
        except Exception:
            pass


_pending = {}   # sk -> hashes added by the in-flight request (for rollback)


def rollback(sk):
    """Request failed: forget the hashes it marked as 'sent' so a retry is
    treated as a normal chained turn, not as a duplicate."""
    hs = _pending.pop(sk, None)
    if not hs:
        return
    with _lock:
        try:
            c = _conn()
            c.executemany("DELETE FROM sent WHERE sk=? AND h=?", [(sk, h) for h in hs])
            c.commit()
            c.close()
        except Exception:
            pass


def known_hashes(sk):
    with _lock:
        try:
            c = _conn()
            rows = c.execute("SELECT h FROM sent WHERE sk=?", (sk,)).fetchall()
            c.close()
        except Exception:
            return set()
    return {r[0] for r in rows}


def add_hashes(sk, hashes):
    if not hashes:
        return
    with _lock:
        try:
            c = _conn()
            c.executemany("INSERT OR IGNORE INTO sent(sk, h, turn) VALUES(?,?,0)",
                          [(sk, h) for h in hashes])
            c.commit()
            c.close()
        except Exception:
            pass


CKPT_DIR_ENV = "META_STATEFUL_CKPT_DIR"


def _ckpt_path(sk):
    d = os.environ.get(CKPT_DIR_ENV, "/opt/muse-bridge/checkpoints")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, sk.replace(":", "_") + ".json")


def save_checkpoint(sk, items):
    """Independent-of-Meta progress record: goal, tool progress, files touched,
    last assistant text. Rewritten from the client's full history each turn."""
    try:
        goal, calls, files, last_text = None, [], [], None
        by_id = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            t = it.get("type")
            if goal is None and (t in (None, "message")) and it.get("role") == "user":
                c = it.get("content")
                goal = c if isinstance(c, str) else " ".join(
                    x.get("text", "") for x in (c or []) if isinstance(x, dict))
            if t == "function_call":
                by_id[it.get("call_id")] = {"name": it.get("name"), "args": (it.get("arguments") or "")[:300]}
                calls.append(it.get("call_id"))
                try:
                    a = json.loads(it.get("arguments") or "{}")
                    p = a.get("path") or a.get("file_path") or a.get("filename")
                    if p and p not in files:
                        files.append(p)
                except ValueError:
                    pass
            if t == "function_call_output" and it.get("call_id") in by_id:
                by_id[it["call_id"]]["out"] = str(it.get("output"))[:200]
            if (t in (None, "message")) and it.get("role") == "assistant":
                c = it.get("content")
                tx = c if isinstance(c, str) else " ".join(
                    x.get("text", "") for x in (c or []) if isinstance(x, dict))
                if tx.strip():
                    last_text = tx[:500]
        prev = load_checkpoint(sk) or {}
        # Merge: a truncated/compacted client history must never shrink progress.
        merged_files = list(prev.get("files_touched") or [])
        for p in files:
            if p not in merged_files:
                merged_files.append(p)
        seen = set(prev.get("call_ids") or [])
        seen.update(calls)
        ck = {"updated": time.time(), "goal": (prev.get("goal") or goal or "")[:2000],
              "tool_calls": max(len(seen), int(prev.get("tool_calls") or 0)),
              "call_ids": sorted(seen)[-2000:],
              "files_touched": merged_files[-100:],
              "last_assistant_text": last_text or prev.get("last_assistant_text"),
              "recent_calls": [by_id[c] for c in calls[-8:] if c in by_id] or prev.get("recent_calls") or []}
        tmp = _ckpt_path(sk) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(ck, f, ensure_ascii=False)
        os.replace(tmp, _ckpt_path(sk))
    except Exception:
        pass


def load_checkpoint(sk):
    try:
        with open(_ckpt_path(sk)) as f:
            return json.load(f)
    except Exception:
        return None


def recovery_note(ck, items):
    """Text for a recovery request, or None. Only when the client's history is
    SHORTER than what the checkpoint remembers (compacted/truncated), because
    a full history already carries everything."""
    if not ck:
        return None
    have = sum(1 for it in items if isinstance(it, dict) and it.get("type") == "function_call")
    if have >= int(ck.get("tool_calls") or 0):
        return None
    lines = ["[bridge checkpoint — earlier history was truncated; this is the progress so far]",
             "Goal: %s" % (ck.get("goal") or "")[:1500],
             "Tool calls completed so far: %s" % ck.get("tool_calls"),
             "Files touched so far: %s" % ", ".join(ck.get("files_touched") or []) or "none"]
    if ck.get("last_assistant_text"):
        lines.append("Last assistant note: %s" % ck["last_assistant_text"][:400])
    rc = ck.get("recent_calls") or []
    if rc:
        lines.append("Most recent calls: " + "; ".join(
            "%s(%s)->%s" % (c.get("name"), (c.get("args") or "")[:80], (c.get("out") or "")[:60]) for c in rc[-5:]))
    lines.append("Do NOT redo completed work. Continue with the next step.")
    return "\n".join(lines)


# ---- per-session serialisation + completed-response cache (idempotent retries) ----
_slocks = {}
_slocks_guard = threading.Lock()
_done = {}          # (sk, fingerprint) -> (expires, status, ctype, payload_bytes)
_DONE_TTL = 600
_DONE_MAX = 200


def session_lock(sk):
    """One lock per session: state read -> Meta call -> chain commit is atomic
    w.r.t. other requests of the SAME session. Single-process server, so an
    in-RAM lock is sufficient (ThreadingHTTPServer, one adapter process)."""
    with _slocks_guard:
        lk = _slocks.get(sk)
        if lk is None:
            lk = _slocks[sk] = threading.RLock()
        return lk


def request_fp(raw_body):
    try:
        d = json.loads(raw_body)
        d.pop("stream", None)
        return hashlib.sha1(json.dumps(d, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode()).hexdigest()
    except Exception:
        return hashlib.sha1(raw_body or b"").hexdigest()


def done_get(sk, fp_):
    with _lock:
        now = time.time()
        for k in [k for k, v in list(_done.items()) if v[0] < now]:
            _done.pop(k, None)
        v = _done.get((sk, fp_))
    return v


def done_put(sk, fp_, status, kind, ctype, payload):
    if status != 200 or not payload:
        return
    with _lock:
        if len(_done) >= _DONE_MAX:
            _done.pop(min(_done, key=lambda k: _done[k][0]), None)
        _done[(sk, fp_)] = (time.time() + _DONE_TTL, kind, status, ctype, payload)


def chain_info(sk):
    """Debug/introspection: (rid, turn, sent_count) or None."""
    with _lock:
        try:
            c = _conn()
            row = c.execute("SELECT rid, turn FROM chains WHERE sk=?", (sk,)).fetchone()
            n = c.execute("SELECT COUNT(*) FROM sent WHERE sk=?", (sk,)).fetchone()
            c.close()
        except Exception:
            return None
    if not row:
        return None
    return {"rid": row[0], "turn": row[1], "sent_items": n[0] if n else 0}


def apply(body, sk):
    """Mutate a parsed Responses request body for stateful chaining.

    Returns (notes:list[str], mode:str). Never raises.
    """
    try:
        return _apply(body, sk)
    except Exception as e:  # noqa: BLE001 — proxy must never die on this
        return (["stateful err %s" % e], "stateless")


def _apply(body, sk):
    if not isinstance(body, dict):
        return ([], "stateless")
    if body.get("previous_response_id"):
        return (["stateful passthrough (client drives chain)"], "passthrough")
    items = body.get("input")
    if not isinstance(items, list) or not items:
        return (["stateful skip (non-list input)"], "stateless")
    if not body.get("previous_response_id"):
        save_checkpoint(sk, items)
    rid = get_chain(sk)
    if not rid:
        body["store"] = True
        _hs = [fp(it) for it in items]
        _pending[sk] = [h for h in _hs if h not in known_hashes(sk)]
        add_hashes(sk, _hs)
        return (["stateful bootstrap (full history, store on)"], "bootstrap")
    known = known_hashes(sk)

    def _server_side(it):
        # Items the MODEL produced are already in Meta's stored history; the
        # client echoes them back in its next request. Resending duplicates.
        if not isinstance(it, dict):
            return False
        t = it.get("type")
        if t in ("reasoning", "function_call"):
            return True
        if (t in (None, "message")) and it.get("role") == "assistant":
            return True
        return False

    new_items = [it for it in items if not _server_side(it) and fp(it) not in known]
    if not new_items:
        # Nothing new by hash (exact retries are already answered from the done-cache
        # before we get here). Answering statelessly would create tool calls in an
        # UNSTORED response that the next chained turn can never reference -> 400.
        # Re-anchor instead: send the full history with store on; this response
        # becomes the new chain head, so any tool call it emits lives in the chain.
        body["store"] = True
        _hs = [fp(it) for it in items]
        _pending[sk] = []
        add_hashes(sk, _hs)
        return (["stateful bootstrap (no-delta re-anchor, store on)"], "bootstrap")
    body["input"] = new_items
    body["previous_response_id"] = rid
    body["store"] = True
    _hs = [fp(it) for it in new_items]
    _pending[sk] = [h for h in _hs if h not in known]
    add_hashes(sk, _hs)
    return (["stateful chained %s (-%d old +%d new)" %
             (rid[:14] + "...", len(items) - len(new_items), len(new_items))],
            "chained")


def record_from_payload(sk, payload_bytes):
    """Capture response id from a non-streaming JSON body. Returns rid|None."""
    try:
        d = json.loads(payload_bytes)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    rid = d.get("id")
    if record(sk, rid):
        return rid
    return None


_RID_ERR_HINTS = ("previous_response_id", "invalid response", "response not found",
                  "unknown response", "expired",
                  # chain head holds a tool call that never got its output (user interrupt)
                  # or the delta's outputs belong to another response
                  "no tool output found", "does not match any function_call")


def is_rid_error(payload_text):
    t = (payload_text or "").lower()
    return any(h in t for h in _RID_ERR_HINTS)

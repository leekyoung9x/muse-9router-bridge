#!/usr/bin/env python3
"""Lift nested prompt-cache counters to top level so the 9router dashboard shows them.

Reads requestDetails.data JSON, and for rows whose tokens carry cache only inside
prompt_tokens_details / input_tokens_details (Meta-native shape), copies the value
to top-level cached_tokens / cache_creation_input_tokens -- the exact fields the
dashboard Usage page reads (tokens.cached_tokens || tokens.cache_read_input_tokens).

Idempotent: never overwrites an existing nonzero top-level value, never touches
prompt/completion counts. Safe to re-run (cron/systemd-timer).

Usage:
    python3 normalize_cache.py --db /path/data.sqlite [--apply] [--since-min N]
    default is dry-run over all rows; --apply writes; --since-min limits to recent rows.
"""
import argparse
import json
import sqlite3
import sys


def normalize_tokens(tok):
    if not isinstance(tok, dict):
        return False
    changed = False
    det = tok.get("prompt_tokens_details") or tok.get("input_tokens_details") or {}
    if not isinstance(det, dict):
        det = {}
    if not tok.get("cached_tokens") and not tok.get("cache_read_input_tokens"):
        v = det.get("cached_tokens") or 0
        try:
            v = int(v)
        except (ValueError, TypeError):
            v = 0
        if v > 0:
            tok["cached_tokens"] = v
            changed = True
    if not tok.get("cache_creation_input_tokens"):
        key = "cache_creation_tokens" if "cache_creation_tokens" in det else "cache_creation_input_tokens"
        v = det.get(key) or 0
        try:
            v = int(v)
        except (ValueError, TypeError):
            v = 0
        if v > 0:
            tok["cache_creation_input_tokens"] = v
            changed = True
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--since-min", type=float, default=0,
                    help="only rows newer than N minutes (0 = all)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    cur = conn.execute("SELECT rowid, data FROM requestDetails")
    scanned = fixed = 0
    updates = []
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    for rowid, data in cur.fetchall():
        scanned += 1
        try:
            d = json.loads(data)
        except (ValueError, TypeError):
            continue
        if args.since_min:
            try:
                dt = datetime.datetime.fromisoformat(str(d.get("timestamp", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if (now - dt).total_seconds() > args.since_min * 60:
                continue
        tok = d.get("tokens")
        if normalize_tokens(tok or {}):
            fixed += 1
            updates.append((json.dumps(d, ensure_ascii=False), rowid))
    print(f"scanned={scanned} rows_to_fix={fixed}")
    if args.apply and updates:
        conn.executemany("UPDATE requestDetails SET data=? WHERE rowid=?", updates)
        conn.commit()
        print(f"applied={len(updates)}")
    elif not args.apply and updates:
        print("dry-run: no writes (pass --apply to write)")
    conn.close()


if __name__ == "__main__":
    sys.exit(main())

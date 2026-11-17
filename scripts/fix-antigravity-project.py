#!/usr/bin/env python3
"""Force 9router's Antigravity connections onto the same project the official agy CLI uses.

Why: 9router's Antigravity OAuth flow stores `cloudaicompanionProject.id` from loadCodeAssist
("aicode-consumers" for consumer accounts), while agy dynamically resolves "default-cli-project"
(log: `Backend project ID updated dynamically to: default-cli-project`). Google buckets quota and
capacity per project; on aicode-consumers a 664-message agent request got
`429 RESOURCE_EXHAUSTED` (16 s upstream) while every other shape succeeded, and weekly/5h
subscription quotas showed 0/1,000 used. Re-logging in via the dashboard re-stores the old value,
so this guard is meant to run on a timer. Token refresh preserves the stored projectId, so the
fix sticks until the next dashboard re-login.

Idempotent, dry-run by default; prints account emails and old->new project only, never tokens.
Usage: fix-antigravity-project.py --db /path/data.sqlite [--apply] [--project default-cli-project]
"""
import argparse, json, sqlite3, sys

ap = argparse.ArgumentParser()
ap.add_argument("--db", required=True)
ap.add_argument("--apply", action="store_true")
ap.add_argument("--project", default="default-cli-project")
a = ap.parse_args()

c = sqlite3.connect(a.db)
changed = 0
for rid, email, data in c.execute(
        "select id,email,data from providerConnections where provider='antigravity'"):
    d = json.loads(data or "{}")
    old = d.get("projectId")
    if old != a.project:
        print("%s: projectId %r -> %r%s" % (email or rid[:8], old, a.project,
                                            "" if a.apply else " (dry-run)"))
        if a.apply:
            d["projectId"] = a.project
            c.execute("update providerConnections set data=? where id=?",
                      (json.dumps(d), rid))
            changed += 1
if a.apply and changed:
    c.commit()
print("connections changed: %d" % changed if a.apply else "dry-run, nothing written")
sys.exit(0)

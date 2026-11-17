#!/usr/bin/env python3
"""Register the Antigravity-Sanitize custom provider in 9router's SQLite DB (idempotent).

Creates (or updates) an OpenAI-compatible *chat* provider node with prefix `ags`
pointing at the local ag-sanitize-proxy, one connection, and one customModel per
--model. The connection's apiKey is 9router's OWN active API key (read from the
apiKeys table): the proxy forwards it, so the sanitized inner request
authenticates against 9router itself. The key is never printed.

With --set-combo NAME, also rewrites that combo: any `ag/gemini-4-hydrogen*`
entry becomes `ags/gemini-4-hydrogen*` so existing sessions pick up the
sanitizer without changing their model name.

Usage: register-9router.py DB [--port 20135] [--prefix ags] [--model ID ...] [--set-combo thtung-paid]
"""
import argparse, datetime, json, sqlite3, sys, uuid

ap = argparse.ArgumentParser()
ap.add_argument("db")
ap.add_argument("--port", default="20135")
ap.add_argument("--prefix", default="ags")
ap.add_argument("--model", action="append", default=[])
ap.add_argument("--set-combo", default="")
a = ap.parse_args()
models = a.model or ["gemini-4-hydrogen-medium"]
NAME = "Antigravity-Sanitize"
base = "http://127.0.0.1:%s/v1" % a.port
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

c = sqlite3.connect(a.db)
r = c.execute("select key from apiKeys where isActive=1 order by createdAt limit 1").fetchone()
if not r:
    sys.exit("no active 9router API key found in apiKeys table")
key = r[0]

node = {"prefix": a.prefix, "apiType": "chat", "baseUrl": base}
row = c.execute("select id from providerNodes where name=?", (NAME,)).fetchone()
if row:
    nid = row[0]
    c.execute("update providerNodes set data=?, updatedAt=? where id=?", (json.dumps(node), now, nid))
    act = "updated"
else:
    nid = "openai-compatible-chat-" + str(uuid.uuid4())
    c.execute("insert into providerNodes(id,type,name,data,createdAt,updatedAt) values(?,?,?,?,?,?)",
              (nid, "openai-compatible", NAME, json.dumps(node), now, now))
    act = "created"

conn = {"defaultModel": models[0], "apiKey": key, "testStatus": "active", "backoffLevel": 0,
        "providerSpecificData": {**node, "nodeName": NAME, "connectionProxyEnabled": False,
                                 "connectionProxyUrl": "", "connectionNoProxy": ""}}
r = c.execute("select id from providerConnections where provider=?", (nid,)).fetchone()
if r:
    c.execute("update providerConnections set data=?, updatedAt=? where id=?", (json.dumps(conn), now, r[0]))
else:
    c.execute("insert into providerConnections(id,provider,authType,name,email,priority,isActive,data,createdAt,updatedAt) values(?,?,?,?,?,?,?,?,?,?)",
              (str(uuid.uuid4()), nid, "apikey", a.prefix, None, 1, 1, json.dumps(conn), now, now))
for m in models:
    c.execute("insert or replace into kv(scope,key,value) values(?,?,?)",
              ("customModels", "%s|%s|llm" % (nid, m),
               json.dumps({"providerAlias": nid, "id": m, "type": "llm", "name": m})))

combo_msg = ""
if a.set_combo:
    row = c.execute("select id, models from combos where name=?", (a.set_combo,)).fetchone()
    if not row:
        sys.exit("combo not found: %s" % a.set_combo)
    ms = json.loads(row[1] or "[]")
    new = [("%s/%s" % (a.prefix, m.split("/", 1)[-1])) if m.startswith("ag/gemini-4-hydrogen") else m for m in ms]
    if new != ms:
        c.execute("update combos set models=?, updatedAt=? where id=?", (json.dumps(new), now, row[0]))
        combo_msg = "; combo %s: %s -> %s" % (a.set_combo, ms, new)
    else:
        combo_msg = "; combo %s already sanitized" % a.set_combo
c.commit()
print("%s provider %s (prefix %s, base %s); models: %s%s" % (
    act, NAME, a.prefix, base, ", ".join(a.prefix + "/" + m for m in models), combo_msg))

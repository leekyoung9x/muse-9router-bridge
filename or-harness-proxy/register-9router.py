#!/usr/bin/env python3
"""Register the OpenRouter-Harness custom provider in 9router's SQLite DB (idempotent).

Creates (or updates) an OpenAI-compatible *chat* provider node with prefix `orh`
pointing at the local or-harness-proxy, one connection, and one customModel per
--model. The OpenRouter key is reused from the existing `openrouter` connection,
or taken from $OPENROUTER_API_KEY. The key is never printed.
Usage: register-9router.py DB [--port 20134] [--prefix orh] [--model ID ...]
"""
import argparse, datetime, json, os, sqlite3, sys, uuid

ap = argparse.ArgumentParser()
ap.add_argument("db")
ap.add_argument("--port", default="20134")
ap.add_argument("--prefix", default="orh")
ap.add_argument("--model", action="append", default=[])
a = ap.parse_args()
models = a.model or ["thinkingmachines/inkling-2-small:free"]
NAME = "OpenRouter-Harness"
base = "http://127.0.0.1:%s/v1" % a.port
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

c = sqlite3.connect(a.db)
key = os.environ.get("OPENROUTER_API_KEY", "").strip()
if not key:
    r = c.execute("select data from providerConnections where provider='openrouter' and isActive=1 order by priority limit 1").fetchone()
    if r:
        key = json.loads(r[0]).get("apiKey", "")
if not key:
    sys.exit("no OpenRouter key: add an OpenRouter connection in the 9router dashboard first, or set OPENROUTER_API_KEY")

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
              (str(uuid.uuid4()), nid, "apikey", "orh", None, 1, 1, json.dumps(conn), now, now))
for m in models:
    c.execute("insert or replace into kv(scope,key,value) values(?,?,?)",
              ("customModels", "%s|%s|llm" % (nid, m),
               json.dumps({"providerAlias": nid, "id": m, "type": "llm", "name": m})))
c.commit()
print("%s provider %s (prefix %s, base %s); models: %s" % (act, NAME, a.prefix, base, ", ".join(a.prefix + "/" + m for m in models)))

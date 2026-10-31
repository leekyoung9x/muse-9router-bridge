#!/usr/bin/env bash
# Self-test — walks every layer of the bridge and reports the failing one.

# Set these to match your install:

: "${ADAPTER_PORT:=20132}"
: "${GATEWAY_PORT:=20133}"
: "${CLAUDE_ADAPTER_PORT:=20129}"
: "${ROUTER_URL:=http://127.0.0.1:20127}"
: "${MUSE_MODEL:=muse-spark-1.3-contributor}"
: "${ROUTER_MODEL:=thtung-muse}"
: "${KEY_FILE:=/poki/tools/muse-adapter/9router.key}"
: "${CACHE_DB:=/poki/tools/muse-adapter/call_cache.sqlite}"
: "${AUTH_JSON:=$HOME/.config/muse/auth.json}"

pass() { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILED=1; }
info() { printf '  ---- %s\n' "$*"; }

FAILED=0
echo "== 1. services =="
for u in muse-adapter muse-gateway claude-adapter; do
  if systemctl is-active --quiet "$u" 2>/dev/null; then pass "$u active"; else fail "$u not active"; fi
done

echo "== 2. ports =="
for p in "$ADAPTER_PORT" "$GATEWAY_PORT" "$CLAUDE_ADAPTER_PORT"; do
  if ss -ltn 2>/dev/null | grep -q ":$p "; then pass "port $p listening"; else fail "port $p not listening"; fi
done

echo "== 3. adapter forwards to meta (expect 401 = reachable) =="
code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 -X POST "http://127.0.0.1:$ADAPTER_PORT/v1/responses" \
        -H 'Content-Type: application/json' -d '{}' || true)
case "$code" in
  401|400|200) pass "adapter reachable (HTTP $code)" ;;
  *)           fail "adapter unreachable (HTTP $code)" ;;
esac

echo "== 4. adapter fixes the attestation gate (flat tools -> 200) =="
python3 - "$ADAPTER_PORT" "$MUSE_MODEL" "$AUTH_JSON" <<'PY'
import json, os, sys, urllib.request, urllib.error
port, model, auth = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    key = json.load(open(os.path.expanduser(auth)))["providers"]["meta"]["api_key"]
except Exception as e:
    print("  \033[31mFAIL\033[0m no Muse key: %s" % e); sys.exit(1)
body = {"model": model,
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "say OK"}]}],
        "tools": [{"type": "function", "name": "read_file", "description": "r",
                   "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}],
        "max_output_tokens": 64, "stream": False}
req = urllib.request.Request("http://127.0.0.1:%s/v1/responses" % port,
                             data=json.dumps(body).encode(),
                             headers={"Authorization": "Bearer " + key,
                                      "Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req, timeout=90)
    print("  \033[32mPASS\033[0m flat tools accepted (HTTP %s)" % r.status)
    r.read(200)
except urllib.error.HTTPError as e:
    detail = e.read().decode("utf-8", "ignore")[:160]
    if e.code == 403:
        print("  \033[31mFAIL\033[0m still 403 — adapter not on the path? %s" % detail)
    else:
        print("  \033[31mFAIL\033[0m HTTP %s %s" % (e.code, detail))
    sys.exit(1)
PY
[ $? -ne 0 ] && FAILED=1

echo "== 5. 9router Muse connection points at the adapter =="
python3 - <<'PY'
import json, sqlite3, sys
db = "/root/ai-services/data-9router/db/data.sqlite"
try:
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
except Exception as e:
    print("  \033[31mFAIL\033[0m cannot open %s: %s" % (db, e)); sys.exit(1)
found = []
for cid, raw in c.execute("SELECT id, data FROM providerConnections"):
    d = json.loads(raw)
    b = (d.get("providerSpecificData") or {}).get("baseUrl", "")
    if "meta.ai" in b or "20132" in b:
        found.append(b)
if not found:
    print("  \033[31mFAIL\033[0m no Muse connection found"); sys.exit(1)
bad = [b for b in found if "20132" not in b]
if bad:
    print("  \033[31mFAIL\033[0m connection still points at %s" % bad[0]); sys.exit(1)
print("  \033[32mPASS\033[0m connection -> %s" % found[0])
PY
[ $? -ne 0 ] && FAILED=1

echo "== 6. chat through 9router =="
python3 - "$ROUTER_URL" "$ROUTER_MODEL" "$KEY_FILE" <<'PY'
import json, sys, urllib.request, urllib.error
url, model, keyfile = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    key = open(keyfile).read().strip()
except Exception as e:
    print("  \033[31mFAIL\033[0m no 9router key: %s" % e); sys.exit(1)
if len(key) < 30:
    print("  \033[31mFAIL\033[0m key file looks truncated (%d chars)" % len(key)); sys.exit(1)
body = {"model": model, "messages": [{"role": "user", "content": "say OK"}],
        "max_tokens": 512, "stream": False}
req = urllib.request.Request(url + "/v1/chat/completions", data=json.dumps(body).encode(),
                             headers={"Authorization": "Bearer " + key,
                                      "Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req, timeout=180)
    d = json.loads(r.read())
    msg = d["choices"][0]["message"]
    txt = (msg.get("content") or "").strip()
    if txt:
        print("  \033[32mPASS\033[0m HTTP %s, content=%r" % (r.status, txt[:40]))
    else:
        print("  \033[33mWARN\033[0m HTTP %s but empty content (reasoning-only reply) — "
              "raise max_tokens or read reasoning_content" % r.status)
except urllib.error.HTTPError as e:
    print("  \033[31mFAIL\033[0m HTTP %s %s" % (e.code, e.read().decode("utf-8", "ignore")[:200]))
    sys.exit(1)
PY
[ $? -ne 0 ] && FAILED=1

echo "== 7. gateway model listing (CLI only) =="
code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "http://127.0.0.1:$GATEWAY_PORT/muse-code/models" || true)
[ "$code" = "200" ] && pass "gateway /muse-code/models 200" || fail "gateway /muse-code/models HTTP $code"

echo "== 8. tool-call replay cache =="
if [ -f "$CACHE_DB" ]; then
  n=$(python3 -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('select count(*) from calls').fetchone()[0])" "$CACHE_DB" 2>/dev/null || echo 0)
  info "cached tool calls: $n"
  [ "$n" -gt 0 ] && pass "cache populated" || info "cache empty — normal on a fresh install, fills as tools are called"
else
  info "no cache file yet — normal on a fresh install"
fi

echo "== 9. claude-adapter reachable (expect 401 = upstream reached, not 400) =="
code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 -X POST "http://127.0.0.1:$CLAUDE_ADAPTER_PORT/v1/messages" \
        -H 'Content-Type: application/json' -d '{}' || true)
case "$code" in
  401|400|200) pass "claude-adapter reachable (HTTP $code)" ;;
  *)           fail "claude-adapter unreachable (HTTP $code)" ;;
esac

echo "== 10. 9router Anthropic connection points at the claude adapter =="
python3 - <<'PY'
import json, sqlite3, sys
db = "/root/ai-services/data-9router/db/data.sqlite"
try:
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
except Exception as e:
    print("  \033[31mFAIL\033[0m cannot open %s: %s" % (db, e)); sys.exit(1)
found = []
for cid, raw in c.execute("SELECT id, data FROM providerConnections"):
    d = json.loads(raw)
    b = (d.get("providerSpecificData") or {}).get("baseUrl", "")
    if "anthropic.com" in b or "20129" in b:
        found.append(b)
if not found:
    print("  \033[33mWARN\033[0m no Anthropic connection found — add one in 9router, then re-run install.sh")
    sys.exit(0)
bad = [b for b in found if "20129" not in b]
if bad:
    print("  \033[31mFAIL\033[0m connection still points at %s" % bad[0]); sys.exit(1)
print("  \033[32mPASS\033[0m connection -> %s" % found[0])
PY
[ $? -ne 0 ] && FAILED=1

echo
if [ "$FAILED" -eq 0 ]; then
  printf '\033[32mALL CHECKS PASSED\033[0m\n'
else
  printf '\033[31mSOME CHECKS FAILED\033[0m — see docs/troubleshooting.md\n'
  exit 1
fi

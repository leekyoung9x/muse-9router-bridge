#!/usr/bin/env bash
# Install the Muse Code <-> 9router bridge.
#
#   bash install.sh              # full install
#   SKIP_RESTART=1 bash install.sh   # do not restart 9router
#
# Override anything via env:
#   INSTALL_DIR=/opt/muse-bridge
#   DB=/root/ai-services/data-9router/db/data.sqlite
#   ADAPTER_PORT=20132  GATEWAY_PORT=20133  CLAUDE_ADAPTER_PORT=20129
#   ADAPTER_URL=http://127.0.0.1:20132/v1     # what 9router should point at
#   CLAUDE_ADAPTER_URL=http://127.0.0.1:20129  # what the Anthropic connection should point at
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$HERE}"
DB="${DB:-/root/ai-services/data-9router/db/data.sqlite}"
ADAPTER_PORT="${ADAPTER_PORT:-20132}"
GATEWAY_PORT="${GATEWAY_PORT:-20133}"
CLAUDE_ADAPTER_PORT="${CLAUDE_ADAPTER_PORT:-20129}"
ADAPTER_URL="${ADAPTER_URL:-http://127.0.0.1:$ADAPTER_PORT/v1}"
CLAUDE_ADAPTER_URL="${CLAUDE_ADAPTER_URL:-http://127.0.0.1:$CLAUDE_ADAPTER_PORT}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SKIP_RESTART="${SKIP_RESTART:-0}"

say() { printf '==> %s\n' "$*"; }

say "install dir: $INSTALL_DIR"
say "9router db:  $DB"
say "adapter:     :$ADAPTER_PORT  (9router will point at $ADAPTER_URL)"
say "gateway:     :$GATEWAY_PORT  (Muse Code CLI entry)"
say "claude:      :$CLAUDE_ADAPTER_PORT  (Anthropic connection will point at $CLAUDE_ADAPTER_URL)"

[ -f "$DB" ] || { echo "ERROR: 9router DB not found at $DB (set DB=...)"; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: python3 required"; exit 1; }

say "writing the 9router API key"
python3 - "$DB" "$INSTALL_DIR/9router.key" <<'PY'
import os, sqlite3, sys
db, out = sys.argv[1], sys.argv[2]
c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
row = c.execute("SELECT key FROM apiKeys WHERE isActive=1 ORDER BY createdAt LIMIT 1").fetchone()
if not row:
    sys.exit("ERROR: no active 9router API key — create one in the dashboard")
key = row[0]
if len(key) < 30:
    sys.exit("ERROR: key looks truncated (%d chars)" % len(key))
with open(out, "w") as f:
    f.write(key + "\n")
os.chmod(out, 0o600)
print("    key written (%d chars) -> %s" % (len(key), out))
PY

say "installing systemd units"
cat > "$UNIT_DIR/muse-adapter.service" <<UNIT
[Unit]
Description=Muse adapter - 9router -> api.meta.ai
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $INSTALL_DIR/adapter.py --host 127.0.0.1 --port $ADAPTER_PORT
Restart=always
RestartSec=3
Environment=MUSE_CALL_CACHE=$INSTALL_DIR/call_cache.sqlite
Environment=MUSE_ADAPTER_LOG=$INSTALL_DIR/adapter.log

[Install]
WantedBy=multi-user.target
UNIT

cat > "$UNIT_DIR/muse-gateway.service" <<UNIT
[Unit]
Description=Muse gateway - Muse Code CLI -> 9router
After=network-online.target 9router.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $INSTALL_DIR/muse_gateway.py --host 127.0.0.1 --port $GATEWAY_PORT
Restart=always
RestartSec=3
Environment=MUSE_GW_UPSTREAM=http://127.0.0.1:20127
Environment=MUSE_GW_KEY_FILE=$INSTALL_DIR/9router.key
Environment=MUSE_GW_LOG=$INSTALL_DIR/gateway.log

[Install]
WantedBy=multi-user.target
UNIT

cat > "$UNIT_DIR/claude-adapter.service" <<UNIT
[Unit]
Description=Claude adapter - 9router -> api.anthropic.com (strips CC markers)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $INSTALL_DIR/claude_adapter.py --host 127.0.0.1 --port $CLAUDE_ADAPTER_PORT
Restart=always
RestartSec=3
Environment=CLAUDE_ADAPTER_LOG=$INSTALL_DIR/claude_adapter.log

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now muse-adapter muse-gateway claude-adapter

say "installing cache normalizer timer (dashboard cache display)"
cat > "$UNIT_DIR/normalize-cache.service" <<UNIT
[Unit]
Description=Normalize nested prompt-cache counters in 9router requestDetails
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $INSTALL_DIR/scripts/normalize-cache.py --db $DB --apply
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
UNIT

cat > "$UNIT_DIR/normalize-cache.timer" <<UNIT
[Unit]
Description=Normalize 9router cache counters every 2 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now normalize-cache.timer

say "pointing the 9router Muse connection at the adapter"
python3 - "$DB" "$ADAPTER_URL" <<'PY'
import json, sqlite3, sys
db, adapter_url = sys.argv[1], sys.argv[2]
c = sqlite3.connect(db)
n = 0
for cid, raw in c.execute("SELECT id, data FROM providerConnections").fetchall():
    d = json.loads(raw)
    psd = d.get("providerSpecificData") or {}
    base = psd.get("baseUrl") or ""
    if "meta.ai" not in base and ":" + adapter_url.rsplit(":", 1)[-1] not in base:
        continue
    psd["baseUrl"] = adapter_url
    psd.setdefault("apiType", "responses")
    d["providerSpecificData"] = psd
    for k in [k for k in d if k.startswith("modelLock_")]:
        d.pop(k, None)
    for k in ("lastError", "errorCode", "lastErrorAt"):
        d.pop(k, None)
    d["testStatus"] = "active"
    c.execute("UPDATE providerConnections SET data=? WHERE id=?", (json.dumps(d), cid))
    n += 1
c.commit()
if n == 0:
    print("    WARNING: no Muse connection found — add one (docs/install.md section 4)")
else:
    print("    repointed %d connection(s) -> %s" % (n, adapter_url))
PY

say "pointing the 9router Anthropic connection at the claude adapter"
python3 - "$DB" "$CLAUDE_ADAPTER_URL" <<'PY'
import json, sqlite3, sys
db, claude_url = sys.argv[1], sys.argv[2]
c = sqlite3.connect(db)
n = 0
for cid, raw in c.execute("SELECT id, data FROM providerConnections").fetchall():
    d = json.loads(raw)
    psd = d.get("providerSpecificData") or {}
    base = psd.get("baseUrl") or ""
    if "anthropic.com" not in base and ":" + claude_url.rsplit(":", 1)[-1] not in base:
        continue
    psd["baseUrl"] = claude_url
    d["providerSpecificData"] = psd
    for k in [k for k in d if k.startswith("modelLock_")]:
        d.pop(k, None)
    for k in ("lastError", "errorCode", "lastErrorAt"):
        d.pop(k, None)
    d["testStatus"] = "active"
    c.execute("UPDATE providerConnections SET data=? WHERE id=?", (json.dumps(d), cid))
    n += 1
c.commit()
if n == 0:
    print("    WARNING: no Anthropic connection found — add one in 9router (baseUrl %s), then re-run install.sh" % claude_url)
else:
    print("    repointed %d connection(s) -> %s" % (n, claude_url))
PY

if [ "$SKIP_RESTART" = "1" ]; then
  say "skipping 9router restart (SKIP_RESTART=1) — restart it yourself"
else
  say "restarting 9router"
  systemctl restart 9router
  sleep 3
fi

say "health"
systemctl is-active muse-adapter muse-gateway claude-adapter 9router || true
systemctl is-enabled normalize-cache.timer || true
printf 'muse-code/models: '
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:$GATEWAY_PORT/muse-code/models" || true
printf 'claude adapter (expect 401 = reachable): '
curl -s -o /dev/null -w '%{http_code}\n' -X POST "http://127.0.0.1:$CLAUDE_ADAPTER_PORT/v1/messages" -H 'Content-Type: application/json' -d '{}' || true

echo
echo "Next: bash $INSTALL_DIR/scripts/selftest.sh"

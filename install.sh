#!/usr/bin/env bash
# Install the Muse Code <-> 9router bridge (adapter + gateway) and point 9router at the adapter.
set -euo pipefail

DIR=/poki/tools/muse-adapter
DB=/root/ai-services/data-9router/db/data.sqlite
UNIT_DIR=/etc/systemd/system

echo "==> writing 9router API key"
python3 - <<'PY'
import os, sqlite3
c = sqlite3.connect("/root/ai-services/data-9router/db/data.sqlite")
row = c.execute("SELECT key FROM apiKeys WHERE isActive=1 ORDER BY createdAt LIMIT 1").fetchone()
if not row:
    raise SystemExit("no active 9router API key found")
k = row[0]
p = "/poki/tools/muse-adapter/9router.key"
with open(p, "w") as f:
    f.write(k + "\n")
os.chmod(p, 0o600)
print("    key written (%d chars)" % len(k))
PY

echo "==> installing systemd units"
cat > "$UNIT_DIR/muse-adapter.service" <<'UNIT'
[Unit]
Description=Muse Adapter — 9router -> api.meta.ai attestation shim
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /poki/tools/muse-adapter/adapter.py --host 127.0.0.1 --port 20132
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
SyslogIdentifier=muse-adapter

[Install]
WantedBy=multi-user.target
UNIT

cat > "$UNIT_DIR/muse-gateway.service" <<'UNIT'
[Unit]
Description=Muse Gateway — Muse Code CLI -> 9router shim
After=network-online.target 9router.service
Wants=network-online.target
Requires=9router.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /poki/tools/muse-adapter/muse_gateway.py --host 127.0.0.1 --port 20133
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
SyslogIdentifier=muse-gateway

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now muse-adapter muse-gateway

echo "==> pointing 9router muse connection at the adapter"
python3 - <<'PY'
import json, sqlite3
db = "/root/ai-services/data-9router/db/data.sqlite"
c = sqlite3.connect(db)
rows = c.execute("SELECT id, data FROM providerConnections").fetchall()
n = 0
for cid, raw in rows:
    d = json.loads(raw)
    psd = d.get("providerSpecificData") or {}
    if "meta.ai" in (psd.get("baseUrl") or ""):
        psd["baseUrl"] = "http://127.0.0.1:20132/v1"
        d["providerSpecificData"] = psd
        for k in [k for k in d if k.startswith("modelLock_")]:
            d.pop(k, None)
        for k in ("lastError", "errorCode", "lastErrorAt"):
            d.pop(k, None)
        d["testStatus"] = "active"
        c.execute("UPDATE providerConnections SET data=? WHERE id=?", (json.dumps(d), cid))
        n += 1
c.commit()
print("    repointed %d connection(s)" % n)
PY

if [ "${SKIP_RESTART:-0}" = "1" ]; then
  echo "==> skipping 9router restart (SKIP_RESTART=1)"
else
  systemctl restart 9router
  sleep 3
fi

echo "==> health"
systemctl is-active muse-adapter muse-gateway 9router
printf 'muse-code/models: '
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:20133/muse-code/models
echo "done"

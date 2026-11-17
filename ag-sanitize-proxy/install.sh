#!/usr/bin/env bash
# Install the Antigravity system-prompt sanitizer proxy + register it in 9router as provider `ags`,
# and point a combo at it. Env: INSTALL_DIR (/opt/ag-sanitize-proxy)  DB (/root/.9router/db/data.sqlite)
#   PORT (20135)  UNIT_DIR (/etc/systemd/system)  MODELS ("gemini-4-hydrogen-medium", space-separated)
#   COMBO (thtung-paid; empty = don't touch combos)  SKIP_SERVICE=1  SKIP_RESTART=1
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/ag-sanitize-proxy}"
DB="${DB:-/root/.9router/db/data.sqlite}"
PORT="${PORT:-20135}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
MODELS="${MODELS:-gemini-4-hydrogen-medium}"
COMBO="${COMBO:-thtung-paid}"
python3 -c "import aiohttp" 2>/dev/null || { echo "need aiohttp: pip install aiohttp" >&2; exit 1; }
[ -f "$DB" ] || { echo "9router DB not found: $DB (set DB=...)" >&2; exit 1; }

if [ "${SKIP_SERVICE:-0}" != 1 ]; then
  mkdir -p "$INSTALL_DIR" && install -m 644 "$HERE/proxy.py" "$INSTALL_DIR/proxy.py"
  cat > "$UNIT_DIR/ag-sanitize-proxy.service" <<UNIT
[Unit]
Description=Antigravity system-prompt sanitizer proxy
After=network.target
[Service]
Environment=AG_PROXY_PORT=$PORT
ExecStart=$(command -v python3) $INSTALL_DIR/proxy.py
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload && systemctl enable --now ag-sanitize-proxy && systemctl restart ag-sanitize-proxy
fi

args=(); for m in $MODELS; do args+=(--model "$m"); done
[ -n "$COMBO" ] && args+=(--set-combo "$COMBO")
cp "$DB" "$DB.bak_ags.$(date +%s)"
python3 "$HERE/register-9router.py" "$DB" --port "$PORT" "${args[@]}"
[ "${SKIP_RESTART:-0}" = 1 ] || systemctl restart 9router
echo "done. Use model 'ags/gemini-4-hydrogen-medium' (or a combo repointed to it)."

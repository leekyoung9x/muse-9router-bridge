#!/usr/bin/env bash
# Install the OpenRouter harness-header proxy + register it in 9router as a custom provider.
# Env: INSTALL_DIR (/opt/or-harness-proxy)  DB (/root/.9router/db/data.sqlite)  PORT (20134)
#      UNIT_DIR (/etc/systemd/system)  MODELS ("thinkingmachines/inkling-2-small:free", space-separated)
#      SKIP_SERVICE=1 (only register)  SKIP_RESTART=1 (don't restart 9router)  OPENROUTER_API_KEY (optional)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/or-harness-proxy}"
DB="${DB:-/root/.9router/db/data.sqlite}"
PORT="${PORT:-20134}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
MODELS="${MODELS:-thinkingmachines/inkling-2-small:free}"
python3 -c "import aiohttp" 2>/dev/null || { echo "need aiohttp: pip install aiohttp" >&2; exit 1; }
[ -f "$DB" ] || { echo "9router DB not found: $DB (set DB=...)" >&2; exit 1; }

if [ "${SKIP_SERVICE:-0}" != 1 ]; then
  mkdir -p "$INSTALL_DIR" && install -m 644 "$HERE/proxy.py" "$INSTALL_DIR/proxy.py"
  cat > "$UNIT_DIR/or-harness-proxy.service" <<UNIT
[Unit]
Description=OpenRouter harness-header proxy
After=network.target
[Service]
Environment=OR_PROXY_PORT=$PORT
ExecStart=$(command -v python3) $INSTALL_DIR/proxy.py
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload && systemctl enable --now or-harness-proxy && systemctl restart or-harness-proxy
fi

args=(); for m in $MODELS; do args+=(--model "$m"); done
cp "$DB" "$DB.bak_orh.$(date +%s)"
python3 "$HERE/register-9router.py" "$DB" --port "$PORT" "${args[@]}"
[ "${SKIP_RESTART:-0}" = 1 ] || systemctl restart 9router
echo "done. Use model names prefixed 'orh/' (NOT 'openrouter/')."

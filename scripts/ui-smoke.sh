#!/usr/bin/env bash
# Real-browser smoke for `oi ui`: sandbox HOME, fake Discord + fake OpenCode,
# headless Chromium (installed into .sandbox/, never the real cache), one
# screenshot per step, asserts the written config/secrets and that
# install_daemon was called through the recorded fake runner.
# Exits 0 only if every step passed; kills everything it started.
set -euo pipefail
cd "$(dirname "$0")/.."

SB="$PWD/.sandbox/ui-smoke"
rm -rf "$SB"
mkdir -p "$SB/home" "$SB/bin" "$SB/ui-shots" "$SB/browsers" "$SB/home/project/.git"

export HOME="$SB/home"
export PLAYWRIGHT_BROWSERS_PATH="$SB/browsers"
export SMOKE_DIR="$SB"

# fixture git clone the wizard can discover and pick
printf '[init]\n\tdefaultBranch = main\n' > "$SB/home/project/.git/config"

# fake `opencode` on PATH: --version answers, serve delegates to the fake
cat > "$SB/bin/opencode" <<'SH'
#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then echo "opencode 2.0.5 (smoke-fake)"; exit 0; fi
if [ "${1:-}" = "serve" ]; then shift; exec python3 scripts/ui_smoke_fakes.py serve "$@"; fi
echo "smoke fake opencode: unsupported args: $*" >&2
exit 64
SH
chmod +x "$SB/bin/opencode"
export PATH="$SB/bin:$PATH"

SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ]; then kill -TERM "-$SERVER_PID" 2>/dev/null || true; fi
  pkill -f "ui_smoke_fakes.py serve" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT

setsid uv run python scripts/ui_smoke_server.py > "$SB/ui-server.log" 2>&1 &
SERVER_PID=$!

ready=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 \
         21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40; do
  if [ -f "$SB/ui.json" ]; then ready=1; break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ui server died:" >&2; cat "$SB/ui-server.log" >&2; exit 1
  fi
  sleep 0.5
done
[ "$ready" = 1 ] || { echo "ui server never became ready" >&2; exit 1; }

PORT=$(python3 -c "import json;print(json.load(open('$SB/ui.json'))['port'])")
TOKEN=$(python3 -c "import json;print(json.load(open('$SB/ui.json'))['token'])")
URL="http://127.0.0.1:$PORT/?t=$TOKEN"

export NODE_PATH="$SB/npm/node_modules"
if [ ! -d "$NODE_PATH/playwright" ]; then
  npm i --prefix "$SB/npm" --no-fund --no-audit --loglevel=error playwright@1 >/dev/null
fi
npx -y playwright@1 install chromium > "$SB/pw-install.log" 2>&1

node scripts/ui-smoke.cjs "$URL" "$SB"

echo "Smoke screenshots:"
for f in "$SB"/ui-shots/*.png; do echo "  $f"; done

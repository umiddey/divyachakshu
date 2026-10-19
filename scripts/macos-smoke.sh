#!/usr/bin/env bash
# Real-macOS smoke for the npm wrapper + the launchd LaunchAgent (plan P6).
# Runs on a GitHub-hosted macOS runner (never on a dev Linux box).
# Assumes: repo checkout as cwd, `uv build` produced dist/*.whl, the npm
# tarball was packed to ./oi-agent-*.tgz and installed with `npm install -g`.
# Verifies: the first `oi` run installs the pinned spec into the wrapper's
# private uv tool dirs, the second run reuses it, `daemon install --user-mode`
# registers the LaunchAgent pointing at the PRIVATE Python oi (never the node
# shim), launchd actually starts it and writes the log file (the fake Discord
# token makes the bot exit; the log still proves the right binary ran),
# status/logs work without hanging, and uninstall removes everything.
set -euo pipefail

SERVICE="oi"
LABEL="ai.oi-agent.${SERVICE}"
LOG_DIR="$HOME/Library/Logs/oi"
LOG_FILE="$LOG_DIR/${SERVICE}.log"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PRIVATE_OI="$HOME/Library/Application Support/oi-agent/npm/bin/oi"
TMPD="${RUNNER_TEMP:-$(mktemp -d)}"
mkdir -p "$TMPD"

fail() { echo "macos-smoke: FAIL: $*" >&2; exit 1; }

WHEEL="$(ls "$PWD"/dist/*.whl 2>/dev/null | head -n 1)"
[ -n "$WHEEL" ] || fail "no wheel under dist/ (run: uv build)"
TARBALL="$(ls "$PWD"/oi-agent-*.tgz 2>/dev/null | head -n 1)"
[ -n "$TARBALL" ] || fail "no oi-agent-*.tgz tarball in the workspace"
command -v oi >/dev/null 2>&1 \
  || fail "oi not on PATH (install the npm tarball globally first)"

# First run: the wrapper installs the pinned spec into its private dirs.
set +e
OI_NPM_PACKAGE_SPEC="$PWD/$WHEEL" oi --help >"$TMPD/first.out" 2>"$TMPD/first.err"
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  cat "$TMPD/first.err" >&2
  fail "first oi --help exited $rc"
fi
grep -q "installing" "$TMPD/first.err" \
  || fail "first run did not install the pinned tool"
[ -x "$PRIVATE_OI" ] || fail "private oi binary missing at $PRIVATE_OI"

# Second run: must reuse the private install, never reinstall.
set +e
OI_NPM_PACKAGE_SPEC="$PWD/$WHEEL" oi --help >"$TMPD/second.out" 2>"$TMPD/second.err"
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  cat "$TMPD/second.err" >&2
  fail "second oi --help exited $rc"
fi
if grep -q "installing" "$TMPD/second.err"; then
  fail "second run reinstalled the tool"
fi

# Throwaway config written through the real CLI (checkout is a git clone,
# which is all watch-add requires); fake Discord token for the daemon.
CFG="$TMPD/oi-smoke-config.toml"
oi config watch-add --channel-id 111 --repo-path "$PWD" --config "$CFG" >/dev/null \
  || fail "could not write the throwaway config"
mkdir -p "$HOME/.config/oi"
printf 'OI_DISCORD_TOKEN=fake-smoke-token\n' > "$HOME/.config/oi/.env"

# LaunchAgent user-mode install with the throwaway config.
oi daemon install --user-mode --config "$CFG" || fail "daemon install failed"
[ -f "$PLIST" ] || fail "plist missing at $PLIST"

# The registered service must point at the private Python oi.
UID_N="$(id -u)"
if ! launchctl print "gui/$UID_N/$LABEL" >"$TMPD/print.txt" 2>&1; then
  cat "$TMPD/print.txt" >&2
  fail "print gui/$UID_N/$LABEL failed"
fi
grep -qF "$PRIVATE_OI" "$TMPD/print.txt" \
  || fail "LaunchAgent program is not the private Python oi"

# launchd must have started it: wait up to ~60s for the log file.
for _ in $(seq 1 30); do
  [ -s "$LOG_FILE" ] && break
  sleep 2
done
[ -s "$LOG_FILE" ] || fail "log file never appeared at $LOG_FILE"

oi daemon status || fail "daemon status failed"
oi daemon logs || fail "daemon logs failed"  # non-follow mode: must not hang

# Uninstall: bootout + plist removal.
oi daemon uninstall || fail "daemon uninstall failed"
[ ! -e "$PLIST" ] || fail "plist still present after uninstall"
if launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1; then
  fail "service still registered after uninstall"
fi

echo "macos-smoke: PASS ($(basename "$WHEEL"))"

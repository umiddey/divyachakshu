#!/usr/bin/env bash
# Real-macOS smoke for the npm wrapper + the launchd LaunchAgent (plan P6).
# Runs on a GitHub-hosted macOS runner (never on a dev Linux box).
# Assumes: repo checkout as cwd, `uv build` produced dist/*.whl, the npm
# tarball was packed to ./divyachakshu-*.tgz and installed with `npm install -g`.
# Verifies: the first `oi` run installs the pinned spec into the wrapper's
# private uv tool dirs, the second run reuses it, `daemon install --user-mode`
# registers the LaunchAgent pointing at the PRIVATE Python oi (never the node
# shim), launchd actually starts it and writes the log file (the fake Discord
# token makes the bot exit; the log still proves the right binary ran),
# status/logs work without hanging, and uninstall removes everything.
set -euo pipefail

SERVICE="oi"
LABEL="ai.divyachakshu.${SERVICE}"
LOG_DIR="$HOME/Library/Logs/oi"
LOG_FILE="$LOG_DIR/${SERVICE}.log"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PRIVATE_OI="$HOME/Library/Application Support/divyachakshu/npm/bin/oi"
TMPD="${RUNNER_TEMP:-$(mktemp -d)}"
mkdir -p "$TMPD"

fail() { echo "macos-smoke: FAIL: $*" >&2; exit 1; }

WHEEL="$(ls "$PWD"/dist/*.whl 2>/dev/null | head -n 1)"
[ -n "$WHEEL" ] || fail "no wheel under dist/ (run: uv build)"
TARBALL="$(ls "$PWD"/divyachakshu-*.tgz 2>/dev/null | head -n 1)"
[ -n "$TARBALL" ] || fail "no divyachakshu-*.tgz tarball in the workspace"
command -v oi >/dev/null 2>&1 \
  || fail "oi not on PATH (install the npm tarball globally first)"

# First run: the wrapper installs the pinned spec into its private dirs.
set +e
OI_NPM_PACKAGE_SPEC="$WHEEL" oi --help >"$TMPD/first.out" 2>"$TMPD/first.err"
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
OI_NPM_PACKAGE_SPEC="$WHEEL" oi --help >"$TMPD/second.out" 2>"$TMPD/second.err"
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
oi config watch-add --channel-id 111 --repo-path "$PWD" --config "$CFG" >"$TMPD/cfg.out" 2>&1 \
  || { cat "$TMPD/cfg.out" >&2; fail "could not write the throwaway config"; }
mkdir -p "$HOME/.config/oi"
printf 'OI_DISCORD_TOKEN=fake-smoke-token\n' > "$HOME/.config/oi/.env"

# LaunchAgent user-mode install with the throwaway config.
oi daemon install --user-mode --config "$CFG" || fail "daemon install failed"
[ -f "$PLIST" ] || fail "plist missing at $PLIST"

# The registered service must run the real Python oi from the wrapper's
# private uv tool dir (the plist stores the symlink-resolved venv binary
# <private>/tools/divyachakshu/bin/oi), never the npm node shim.
UID_N="$(id -u)"
if ! launchctl print "gui/$UID_N/$LABEL" >"$TMPD/print.txt" 2>&1; then
  cat "$TMPD/print.txt" >&2
  fail "print gui/$UID_N/$LABEL failed"
fi
PRIVATE_VENV_OI="$HOME/Library/Application Support/divyachakshu/npm/tools/divyachakshu/bin/oi"
[ "$(realpath "$PRIVATE_OI")" = "$PRIVATE_VENV_OI" ] \
  || fail "private oi symlink does not resolve to $PRIVATE_VENV_OI"
grep -qF "$PRIVATE_VENV_OI" "$TMPD/print.txt" \
  || { grep -n "program\|arguments" "$TMPD/print.txt" >&2; fail "LaunchAgent program is not the private Python oi"; }

# launchd must have started it and the bot must get as far as Discord login:
# with the fake token the only acceptable end is discord.py's LoginFailure.
# Any earlier crash (e.g. state DB not openable) fails the smoke.
for _ in $(seq 1 30); do
  grep -q "LoginFailure" "$LOG_FILE" 2>/dev/null && break
  sleep 2
done
[ -s "$LOG_FILE" ] || fail "log file never appeared at $LOG_FILE"
if ! grep -q "LoginFailure" "$LOG_FILE"; then
  tail -n 40 "$LOG_FILE" >&2
  fail "bot did not reach Discord login (expected LoginFailure on the fake token)"
fi

oi daemon status || fail "daemon status failed"
oi daemon logs || fail "daemon logs failed"  # non-follow mode: must not hang

# Uninstall: bootout + plist removal.
oi daemon uninstall || fail "daemon uninstall failed"
[ ! -e "$PLIST" ] || fail "plist still present after uninstall"
if launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1; then
  fail "service still registered after uninstall"
fi

echo "macos-smoke: PASS ($(basename "$WHEEL"))"

#!/usr/bin/env bash
# Sandbox smoke for the npm wrapper (plan P3):
#   uv build -> npm pack -> npm install -g --prefix <.sandbox>/prefix ->
#   <sandbox>/bin/oi --help with OI_NPM_PACKAGE_SPEC=<locally built wheel>.
# HOME and XDG_DATA_HOME live under .sandbox/, so uv, npm caches and the
# wrapper's private uv tool dir never leave the sandbox. The smoke fails if
# anything changes under the real ~/.local/share/divyachakshu, and removes
# .sandbox/ on the way out.
set -euo pipefail

WORKTREE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX="$WORKTREE/.sandbox"
REAL_DATA="$HOME/.local/share/divyachakshu"

fail() { echo "npm-sandbox-smoke: FAIL: $*" >&2; exit 1; }
cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

cd "$WORKTREE"
uv build

WHEEL="$(ls "$WORKTREE"/dist/*.whl 2>/dev/null | head -n 1)"
[ -n "$WHEEL" ] || fail "uv build produced no wheel"

mkdir -p "$SANDBOX"
(cd "$WORKTREE/npm" && npm pack --pack-destination "$SANDBOX" >/dev/null)
TARBALL="$(ls "$SANDBOX"/divyachakshu-*.tgz 2>/dev/null | head -n 1)"
[ -n "$TARBALL" ] || fail "npm pack produced no tarball"

# Baseline of the real data dir before the wrapper runs.
real_before="$(ls -1AR "$REAL_DATA" 2>/dev/null || true)"

export HOME="$SANDBOX/home"
export XDG_DATA_HOME="$SANDBOX/data"
mkdir -p "$HOME"

npm install -g --prefix "$SANDBOX/prefix" --no-fund --no-audit "$TARBALL" >/dev/null

OI="$SANDBOX/prefix/bin/oi"
[ -x "$OI" ] || fail "npm install did not create $OI"

# First run: installs the pinned spec into the private uv dirs.
set +e
OI_NPM_PACKAGE_SPEC="$WHEEL" "$OI" --help >"$SANDBOX/first.out" 2>"$SANDBOX/first.err"
first_rc=$?
set -e
if [ "$first_rc" -ne 0 ]; then
  cat "$SANDBOX/first.err" >&2
  fail "first oi --help exited $first_rc"
fi
grep -q "installing" "$SANDBOX/first.err" \
  || fail "first run did not install (no 'installing' line on stderr)"
[ -x "$XDG_DATA_HOME/divyachakshu/npm/bin/oi" ] \
  || fail "private oi binary not inside the sandbox data dir"

# Second run: must reuse the install, not reinstall.
set +e
OI_NPM_PACKAGE_SPEC="$WHEEL" "$OI" --help >"$SANDBOX/second.out" 2>"$SANDBOX/second.err"
second_rc=$?
set -e
if [ "$second_rc" -ne 0 ]; then
  cat "$SANDBOX/second.err" >&2
  fail "second oi --help exited $second_rc"
fi
if grep -q "installing" "$SANDBOX/second.err"; then
  fail "second run reinstalled the tool"
fi

# Nothing may appear under the real ~/.local/share/divyachakshu.
real_after="$(ls -1AR "$REAL_DATA" 2>/dev/null || true)"
if [ "$real_before" != "$real_after" ]; then
  diff <(printf '%s\n' "$real_before") <(printf '%s\n' "$real_after") >&2 || true
  fail "real $REAL_DATA changed during the smoke"
fi

echo "npm-sandbox-smoke: PASS (wheel: $(basename "$WHEEL"))"

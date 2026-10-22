#!/usr/bin/env bash
# The only way to upgrade uv.lock. It resolves against releases published at
# least 7 days ago (uv --exclude-newer), so a brand-new release cannot enter
# the lock on the day it appears.
#
# Usage: scripts/upgrade-lock.sh [--dry-run] [pkg ...]
#   no pkg   upgrade every locked package (uv lock --upgrade)
#   pkg ...  upgrade only the named packages (uv lock --upgrade-package pkg)
#   --dry-run resolve and report without writing uv.lock
set -euo pipefail

WORKTREE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WORKTREE"

# uv 0.9 accepts only absolute --exclude-newer timestamps, so compute the
# cutoff here. GNU date first, then BSD date (macOS).
cutoff="$(date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
  || date -u -v-7d +%Y-%m-%dT%H:%M:%SZ)"

args=(--exclude-newer "$cutoff")
named=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) args+=(--dry-run) ;;
    -*) echo "upgrade-lock: unknown option: $arg" >&2; exit 2 ;;
    *) args+=(--upgrade-package "$arg"); named=$((named + 1)) ;;
  esac
done
if [ "$named" -eq 0 ]; then
  args+=(--upgrade)
fi

echo "upgrade-lock: releases published after $cutoff are ignored" >&2
exec uv lock "${args[@]}"

#!/usr/bin/env bash
# Release gate: the git tag (without the leading v) and both package versions
# must agree. Locally and on PRs (no tag) it compares the Python and npm
# versions only; on a v* tag push (release.yml) all three must match.
set -euo pipefail

WORKTREE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WORKTREE"

fail() { echo "check-release-versions: FAIL: $*" >&2; exit 1; }

py_version="$(sed -nE 's/^version = "(.+)"$/\1/p' pyproject.toml | head -n 1)"
npm_version="$(sed -nE 's/^[[:space:]]*"version":[[:space:]]*"([^"]+)",?$/\1/p' npm/package.json | head -n 1)"

[ -n "$py_version" ] || fail "pyproject.toml: no version found"
[ -n "$npm_version" ] || fail "npm/package.json: no version found"
[ "$py_version" = "$npm_version" ] \
  || fail "pyproject.toml $py_version != npm/package.json $npm_version"

tag=""
if [ "${GITHUB_REF_TYPE:-}" = "tag" ]; then
  tag="${GITHUB_REF_NAME:-}"
  tag="${tag#v}"
fi
if [ -n "$tag" ]; then
  [ "$tag" = "$py_version" ] || fail "tag v$tag != pyproject.toml $py_version"
  [ "$tag" = "$npm_version" ] || fail "tag v$tag != npm/package.json $npm_version"
fi

echo "check-release-versions: OK ($py_version)"

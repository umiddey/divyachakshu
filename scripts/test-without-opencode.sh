#!/usr/bin/env bash
# Run the full test suite the way a fresh CI machine sees it: no OpenCode
# binary anywhere (PATH, /usr/bin, ~/.opencode/bin) and an empty HOME, so no
# personal git config or OpenCode credentials leak in. Owned by Main (P8).
set -euo pipefail
cd "$(dirname "$0")/.."

tmp=$(mktemp -d "${TMPDIR:-/tmp}/oi-no-oc.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/home"

uv_bin=$(command -v uv)
for f in /usr/bin/* /bin/* "$uv_bin"; do
	name=${f##*/}
	case $name in opencode*) continue ;; esac
	[ -e "$tmp/bin/$name" ] || [ -L "$tmp/bin/$name" ] || ln -s "$f" "$tmp/bin/$name"
done

if env HOME="$tmp/home" PATH="$tmp/bin" sh -c 'command -v opencode' >/dev/null 2>&1; then
	echo "test-without-opencode: FAIL: opencode is still reachable" >&2
	exit 1
fi
if [ -e "$tmp/home/.opencode/bin/opencode" ]; then
	echo "test-without-opencode: FAIL: official install dir present" >&2
	exit 1
fi

exec env HOME="$tmp/home" UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}" PATH="$tmp/bin" \
	uv run --frozen pytest -q -p no:cacheprovider "$@"

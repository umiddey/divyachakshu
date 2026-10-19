# oi-agent (npm wrapper)

Installs the `oi` command for [OI](https://github.com/umiddey/oi-agent), a
standalone Discord bot that answers only when explicitly addressed and audits
local git clones read-only through [OpenCode](https://opencode.ai).

```bash
npm install -g oi-agent
oi --help
```

## How it works

- Zero npm dependencies, no install scripts. Everything happens on the first
  `oi` run.
- The first run finds `uv` (`PATH`, `~/.local/bin`, `~/.cargo/bin`) or
  installs it with the official installer (`UV_NO_MODIFY_PATH=1`, your `PATH`
  is not modified).
- OI itself is the Python distribution [`divyachakshu`](https://pypi.org/project/divyachakshu/),
  pinned to exactly this package's version and installed with
  `uv tool install --force --python 3.11` into a private uv root:
  `~/.local/share/oi-agent/npm` on Linux, `~/Library/Application Support/oi-agent/npm`
  on macOS (`XDG_DATA_HOME` respected). It never collides with an `oi` you
  installed yourself, and later runs reuse it — the tool is only reinstalled
  when the pinned version changes.
- The command, arguments, stdin/stdout, exit code, and SIGINT/SIGTERM/SIGHUP
  are passed through to the installed `oi`.

Requires Node >= 18 on Linux or macOS. Windows is not supported (use WSL2).

## Development hook

`OI_NPM_PACKAGE_SPEC=<spec>` overrides the pinned distribution spec; the
sandbox smoke (`scripts/npm-sandbox-smoke.sh` in the repository) uses it to
install a locally built wheel.

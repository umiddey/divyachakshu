# OI Agent

OI ("ultron" on Discord) is a standalone bot that watches configured Discord
channels or whole guilds, answers only when explicitly triggered, and runs a
read-only audit of local git clones through [OpenCode](https://opencode.ai).
OI owns Discord transport, allowlists, rate caps, and delivery; OpenCode owns
model, tools, and repository exploration. With `external_inputs = "read-only"`
it also does public web research in an isolated second run.

## Quick start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a supported
OpenCode release (`opencode --version`).

```bash
uv sync --dev
opencode auth login --standalone    # provider credentials for OpenCode
oi init --model provider/model      # interactive: token, personality, watches
oi run                              # foreground run
```

- `oi init` writes `~/.config/oi/config.toml` and stores the Discord token as
  the environment variable named by `discord_token_env` (default
  `OI_DISCORD_TOKEN`) in `~/.config/oi/secrets.env`, mode 0600. The token
  value never appears in the config file or `oi config show`.
- Non-interactive: `oi init --install-opencode --model provider/model` also
  installs OpenCode through a fixed package-manager command (`npm install
  --global opencode-ai`, brew, or `mise use --global npm:opencode-ai`) and
  verifies `opencode --version`; declining installation aborts setup.
  `oi init --environment-mode workstation|server` and `oi init
  --install-daemon` (dedicated unprivileged user plus hardened systemd system
  unit) cover server setups.
- Other commands: `oi pause` / `oi resume` (global posting switch), `oi
  doctor` (preflight: OpenCode binary, model id, repos, token, state), `oi
  queue status|retry|purge`, `oi session list|forget`, `oi memory status`.

### systemd user service

This checkout runs as a user service: `~/.config/systemd/user/oi.service`
executes `oi run --config ~/.config/oi/config.toml` with `Restart=always`
(`RestartSec=10`) and a drop-in `oi.service.d/mise-trust.conf` carrying the
mise environment:

```bash
systemctl --user daemon-reload
systemctl --user enable --now oi.service
systemctl --user restart oi.service
journalctl --user -u oi.service -f        # follow logs
```

### Minimal config

```toml
discord_token_env = "OI_DISCORD_TOKEN"
opencode_model = "provider/model"      # the model you authenticated in OpenCode

[[watch]]
channel_id = 123456789012345678        # or: guild_id = ... for a whole server
repos = ["/srv/repos/backend"]
bot_user_id = 345678901234567890       # the bot's own Discord user id
post_hourly_cap = 3
```

## Install with npm

On Linux or macOS, `oi` installs from npm without any Python setup:

```bash
npm install -g divyachakshu    # needs Node >= 18; no npm dependencies, no install scripts
oi --help
```

The npm package is a thin wrapper around the Python distribution
[`divyachakshu`](https://pypi.org/project/divyachakshu/). The first `oi` run:

1. finds `uv` (`PATH`, `~/.local/bin`, `~/.cargo/bin`) or installs it with the
   official installer (`UV_NO_MODIFY_PATH=1`; your `PATH` is not modified);
2. installs `divyachakshu`, pinned to exactly the npm package's version, with
   `uv tool install --force --python 3.11` into a private uv root
   (`~/.local/share/divyachakshu/npm` on Linux, `~/Library/Application
   Support/divyachakshu/npm` on macOS; `XDG_DATA_HOME` respected), so it never
   collides with an `oi` you installed yourself;
3. hands your arguments, stdio, exit code, and SIGINT/SIGTERM/SIGHUP to the
   installed `oi`.

Later runs reuse the install; it is refreshed only when the pinned version
changes. `OI_NPM_PACKAGE_SPEC=<spec>` overrides the pin (development hook;
`scripts/npm-sandbox-smoke.sh` uses it for a locally built wheel). Windows is
not supported — use WSL2.

## macOS (launchd)

On macOS the daemon runs as a per-user LaunchAgent; Linux systemd behavior is
unchanged. `oi daemon install` (or `oi init --install-daemon`) writes
`~/Library/LaunchAgents/ai.divyachakshu.<service_name>.plist` (rendered with
`plistlib`; `RunAtLoad` + `KeepAlive`; program arguments are the real Python
`oi` binary with `run --config <path>`; the environment `PATH` includes the
OpenCode bin dir; stdout/stderr go to `~/Library/Logs/oi/<service_name>.log`),
then loads it with `launchctl bootstrap gui/<uid>` and starts it. Status and
logs: `oi daemon status` (`launchctl print gui/<uid>/<label>`) and `oi daemon
logs` (tail of the log file); `oi daemon uninstall` boots the agent out and
removes the plist. User mode only: the dedicated-daemon-user system mode
refuses on macOS with a clear message. Not yet tested on real Mac hardware.

## Configuration reference

Edit with `oi config set KEY VALUE`, `oi config watch-add ...`, `oi config
watch-remove ID`, `oi config show`. Writes are validated before an atomic
replacement. Defaults:

| Key | Default | Meaning |
|---|---|---|
| `discord_token_env` | `OI_DISCORD_TOKEN` | Name of the env var holding the Discord token. |
| `db_path` | `~/.local/state/oi/state.db` | SQLite state (queue, sessions, memory). |
| `personality` | teammate-style default | Persona text handed to the model. |
| `max_reply_chars` | `2000` | Reply length cap. |
| `reply_delivery` | `chunked` | `chunked` or `single_message` (must fit Discord's 2000-char limit). |
| `opencode_binary` | `opencode` | Binary name or path; version-checked in isolation per run. |
| `opencode_model` | `openai/gpt-4o-mini` | Model id passed to OpenCode. |
| `opencode_steps` | `30` (max 50) | Step budget for answer runs; research adds one per fetched URL. |
| `opencode_timeout_seconds` | `900` (max 86400) | Wall-clock cap per OpenCode run. |
| `environment_mode` | `workstation` | `workstation` or `server`; legacy `auto` fails with a migration error. |
| `write_mode` | `read-only` | `read-only`, or `docs-only` to allow edit access for documentation paths. |
| `write_dirs` | `[]` | Directories `docs-only` may write; none usable means it refuses to widen. |
| `max_session_turns` | `30` | Completed deliveries before a conversation session rotates. |
| `session_ttl_days` | `7` | Idle days before rotation; `0` disables idle rotation only. |
| `max_context_messages` | `200` (max 1000) | Bounded history window shown to the model. |
| `max_context_age_days` | `30` (max 365) | Age cutoff for that window. |
| `reminder_channel_id` | `0` | Channel that receives posted reminders (`0` = off). |
| `external_inputs` | `disabled` | `disabled` or `read-only` (public web research; see below). |
| `max_external_evidence_items` | `8` (1–50) | Cap on evidence items and research source links. |
| `max_attachments` | `4` (0–16) | Attachments accepted per message. |
| `max_attachment_bytes` | `8388608` (max 33554432) | Per-attachment byte cap. |
| `image_inputs` | `off` | `off`, or `on` to pass images as real image files. |
| `watches` | `[]` | `[[watch]]` entries: `channel_id` or `guild_id`, `repos`, `bot_user_id`, `post_hourly_cap` (default 3), `ignored_channels`, `allowed_channels`. |

## Using the bot in Discord

A message is answered only when it **explicitly addresses the bot**: written
`<@bot-id>` mention markup, a start with `oi:`, the marker `#oi`, `#audit`,
or `#bugreport`, or the bot's name used as a direct call (`Ultron, …`,
`hey ultron`, `…, ultron?`). A reply to one of the bot's messages counts
too, unless it addresses someone else (other `<@…>` markup, or a direct
call to another member or bot). Everything else is ignored (log reason:
`no explicit trigger`). Replies are capped at `max_reply_chars` and
delivered chunked by default.

Conversation sessions are scoped to (conversation, repository) and rotate
automatically after `max_session_turns` completed deliveries or
`session_ttl_days` idle days.

Commands (the command must be the leading token; multi-line arguments are one
command):

| Input | Effect |
|---|---|
| `!fresh` / `!new` | Starts a new session for this conversation; optionally after the bot mention. |
| `!search <topic>` | Forces public web research on `<topic>`. |
| `!news <topic>` | Same, framed as news lookup. |
| `!web <topic>` | Alias of `!search`. |
| `!fetch <url>` / `!url <url>` | Fetches and summarizes that public URL. |
| plain phrases | "search the web/internet", "search online", "google X", "look up X online", "find/research X online or on the internet/web" also force research; the whole message becomes the query. |

Research input also includes every public `http(s)` URL found in the request
and in recent channel history when the message refers to links or URLs.

What the refusal replies mean (the public part is never answered from
repository memory):

- *"Public internet retrieval is disabled on this bot"* — `external_inputs`
  is `disabled` (the default); set it to `read-only` to enable.
- *"That looks like a public lookup, but it doesn't name a public topic or
  URL"* — no usable subject; reply with a topic or URL.
- *"I can't look that up: it refers to private conversation content"* — the
  subject points at chat content ("the token above"); OI only fetches public
  sources.
- *"I couldn't decide whether this needs web research (the classifier is
  unavailable right now)"* — see troubleshooting; `!search <topic>` bypasses
  the classifier.

Merge-request audits: `MR 188` or `!188` (GitLab), `PR 188` or
`pull request 188` (GitHub), or a canonical merge-request URL triggers a
read-only review instead of the checkout audit; bare numbers are ordinary
text.

## How web research works

When `external_inputs = "read-only"`, each request goes through two OpenCode
runs:

1. A tool-free **classifier** (the configured model) decides whether the
   request needs public research, answering one strict JSON object
   (`{"research": bool, "query": str}`) with one retry. Forced inputs
   (commands, plain web phrases, link references) skip it;
   `disabled` mode never calls it.
2. An isolated **research run** in an empty temporary directory and a
   disposable home may use only `websearch` and `webfetch`. It sees the
   question plus public `http(s)` URLs from the request and recent history —
   never the repository, channel text, or OI credentials. Private, loopback,
   and `file:` URLs are dropped.

Limits: topic capped at 200 characters; step budget `opencode_steps` plus one
per fetched URL; timeout 180 seconds plus 60 seconds per URL; research
summary capped at 6000 characters; sources and evidence items bounded by
`max_external_evidence_items` and a 12,000-character evidence budget. The
answer session receives the summary as bounded untrusted evidence; any
outbound link not in the research source list or the user's own message is
replaced with `[link removed]` and embeds are suppressed.

## Troubleshooting

- **Bot never replies** — the message did not explicitly address the bot.
  Write `<@bot-id>` markup, start with `oi:`, use `#oi`, `#audit`, or
  `#bugreport`, call the bot by name (`Ultron, …`), or reply to one of its
  messages (a reply addressed to someone else stays silent); the log line
  shows `no explicit trigger`.
- **`403` / "Missing Access" in logs** — the bot is not in the configured
  guild (or lacks channel access). OI treats HTTP 401/403/404 as permanent
  and does not retry: invite the bot and check its channel permissions.
- **`No usable OpenCode ...` / binary errors** — the first `opencode` on
  `PATH` is often a launcher shim (npx/mise, distro wrapper). OI
  version-checks every candidate in its isolated environment and fails
  naming each rejection. Install a supported release (`npm install --global
  opencode-ai`, brew, or `mise use --global npm:opencode-ai`).
- **Service can't find `oi` or `opencode` (mise)** — mise shims refuse to run
  in untrusted directories: trust the checkout (`mise trust`) and keep the
  `oi.service.d/mise-trust.conf` drop-in carrying the mise environment into
  the user service (`systemctl --user cat oi.service` shows it).
- **"classifier is unavailable" reply** — the decision model returned no
  usable JSON verdict even after one retry, so the public part is left
  unanswered rather than answered from memory. Retry, or force the lookup
  with `!search <topic>`.
- **Doctor warning** `model <id> not verified against OpenCode's model list;
  provider credential present` — OpenCode returned an empty model listing;
  a warning, not a failure; otherwise `oi doctor` must end in `all checks
  passed`.

## Releasing

A single `v*` tag publishes both packages from GitHub Actions (agents never
publish):

1. Bump `version` in **both** `pyproject.toml` and `npm/package.json` — they
   must match; `bash scripts/check-release-versions.sh` enforces it (on a tag
   push it also requires tag `vX.Y.Z` == version).
2. Commit, tag `vX.Y.Z`, and push the tag. `release.yml` then:
   - builds the wheel/sdist and publishes `divyachakshu` to PyPI by trusted
     publishing (OIDC, `environment: pypi`, no API token), then
   - publishes the `divyachakshu` npm wrapper with provenance (OIDC, no
     `NPM_TOKEN`).

One-time bootstrap (owner + maintainer, before the first tagged release):
register a PyPI *pending trusted publisher* for project `divyachakshu`
(repo `umiddey/divyachakshu`, workflow `release.yml`, environment `pypi`); create
the npm package by publishing `divyachakshu` 0.1.0 once by hand; then register
the npm Trusted Publisher for the same workflow. Every later tag publishes
from Actions.

## Development

```bash
uv sync --dev
uv run pytest            # known baseline: 48 failures from a git-push fixture,
                         # pre-existing and unrelated to documentation changes
uv run ruff check .      # line length 120
uv run python -c "import oi_agent"
```


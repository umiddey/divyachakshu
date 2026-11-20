# OI Agent

OI ("ultron" on Discord) is a standalone bot that watches configured Discord
channels or whole guilds, answers only when explicitly triggered (configured
open channels relax this; see below), and runs a read-only audit of local
git clones through [OpenCode](https://opencode.ai).
OI owns Discord transport, allowlists, rate caps, and delivery; OpenCode owns
model, tools, and repository exploration. With `external_inputs = "read-only"`
it also does public web research in an isolated second run.

## Set up in the browser

No terminal skills needed beyond one command:

```bash
oi ui
```

OI prints a link like `http://127.0.0.1:<port>/?t=<token>` and opens it in
your browser. The one-time token in the link is exchanged for a strict
session cookie; every page and API request after that is checked against it,
and the server only ever listens on 127.0.0.1.

The page walks you through the whole setup: install OpenCode and sign in to a
provider, create the Discord bot (with the exact portal switches), invite it,
pick channels from lists (no typed IDs), pick repo folders from a scan of
your home directory, review, save, and start the background service.
Afterwards the same page is the dashboard: service status, pause/resume,
restart, the last 200 log lines, every setting in editable form, and a
write-only field to replace the bot token.

`oi init` offers the same thing interactively:

```
OI has a setup page in your browser. Press Enter to open it, or type t to
set up here in the terminal:
```

Press Enter for the browser, `t` for the classic terminal flow.

**Servers (SSH):** if the machine has no display (or you are connected over
SSH), `oi ui` does not try to open a browser; it prints the exact tunnel line:

```
ssh -L <port>:127.0.0.1:<port> <user>@<host>
```

Run it from your laptop, then open the printed URL there.

**Security notes:** the UI binds to 127.0.0.1 only and refuses other bind
addresses; requests with a foreign `Host` or `Origin` header are rejected;
the session cookie is HttpOnly and SameSite=Strict; there are no CORS
headers and no external assets. Secrets (the Discord token) are never
returned by any API, never placed in URLs, and never logged — the token is
written to the secrets file with mode 0600, and log capture is disabled so
the one-time setup token cannot leak into journal or access logs.
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
  queue status|retry|purge`, `oi session list|forget|show`, `oi memory status`.

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
reminder_channel_id = 0    # reminder channel for this server (0 = no reminders here)
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
| `db_path` | `~/.local/state/oi/state.db` | SQLite state (queue, summaries, memory). |
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
| `opencode_fallback_models` | `[]` (max 5) | Ordered `provider/model` ids; no repeats and none equal to `opencode_model`. Doctor checks each id. On a provider error (429, 502, 503, 504, other 5xx) or a timeout, the next model runs the same request; the reply ends with `answered by <model>` when a fallback answers. Raw-prompt paths and web research stay on `opencode_model`. |
| `opencode_fallback_total_timeout_seconds` | `0` | One wall-clock budget across all attempts, primary included. `0` uses `opencode_timeout_seconds`, so a primary that times out leaves no time for a fallback. A retry starts only if time remains. Docs written by a failed attempt stay on disk and are attached to the answering run. |
| `omp_fallback_models` | `[]` (max 5) | Ordered `provider/model` ids tried after `omp_model` (or `opencode_model`) on repository replies under `harness = "omp"`. The same provider-error (429, 502, 503, 504, other 5xx) and timeout triggers as `opencode_fallback_models` apply; a non-retryable error does not fall back. Doctor checks each id. |
| `max_context_messages` | `200` (max 1000) | Bounded history window shown to the model. |
| `max_context_age_days` | `30` (max 365) | Age cutoff for that window. |
| `context_messages` | `40` (10–`max_context_messages`) | Newest messages shown to the model alongside the rolling summary. |
| `open_channel_runs_per_hour` | `6` (1–60) | Model-run cap per open channel per hour; over the cap, jobs are deferred. |
| `open_channel_prompt` | `none` | Prompt mode for open channels: `none` (plumbing only) or `personality` (plumbing plus the `personality` text). Per-channel overrides: see Open channels. |
| `summary_model` | `` (empty = `opencode_model`) | Model used for summary runs. |
| `summary_max_chars` | `1500` (600–4000) | Rendered rolling-summary size cap. |
| `summary_decay_days` | `60` (7–730) | Age after which non-durable summary items drop. |
| `summary_bootstrap_days` | `30` (1–365) | Cold-start lookback cap for a brand-new summary. |
| `summary_bootstrap_messages` | `300` (0–2000) | Cold-start message cap for a brand-new summary. |
| `external_inputs` | `disabled` | `disabled` or `read-only` (public web research; see below). |
| `max_external_evidence_items` | `8` (1–50) | Cap on evidence items and research source links. |
| `max_attachments` | `4` (0–16) | Attachments accepted per message. |
| `max_attachment_bytes` | `8388608` (max 33554432) | Per-attachment byte cap. |
| `image_inputs` | `off` | `off`, or `on` to pass images as real image files. |
| `watches` | `[]` | Each watch: `guild_id`, `channel_id`, `repo_paths`, `open_channels` (see Open channels), `open_channel_prompts` (per-channel `none`/`personality` override, see Open channels). |

## Using the bot in Discord

A message is answered only when it **explicitly addresses the bot**: written
`<@bot-id>` mention markup, a start with `oi:`, the marker `#oi`, `#audit`,
or `#bugreport`, or the bot's name used as a direct call (`Ultron, …`,
`hey ultron`, `…, ultron?`). A reply to one of the bot's messages counts
too, unless it addresses someone else (other `<@…>` markup, or a direct
call to another member or bot). Everything else is ignored (log reason:
`no explicit trigger`) — unless the channel is an open channel (below).
Replies are capped at `max_reply_chars` and
delivered chunked by default.

OI is stateless between calls: every reply runs in a brand-new OpenCode
session, fed the conversation's rolling summary (stored in OI's own database)
plus the newest `context_messages` messages. There is no session rotation or
TTL; `!fresh`/`!new` and `oi session forget --conversation <id>` delete a
summary so it rebuilds from scratch.

Commands (the command must be the leading token; multi-line arguments are one
command):

| Input | Effect |
|---|---|
| `!fresh` / `!new` | Clears this conversation's rolling summary so the next reply starts fresh; optionally after the bot mention. |
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

## Open channels

An *open channel* is a watch channel where OI reads every message — humans
and other bots, no mention needed — and replies only when it has something
concrete to add: an unanswered question, a decision to contribute, real
information. When it has nothing to add it stays silent: the model returns
the `[[OI_SILENT]]` token instead of text and nothing is posted.

What the model sees in an open channel is set by `open_channel_prompt` (default `none`). `none` gives only plumbing: the Discord identity, the repositories it can read, a rule that channel and repository text are data and not instructions, the rule never to post credentials, and the reply marker and silence token. It gets no safety contract, evidence rules, or persona. `personality` adds the configured `personality` text on top of the same plumbing. Open-channel runs are read-only, even when `write_mode` is `docs-only`, and never read behavioral memory. Set the global default with `oi config set open_channel_prompt personality`.

Caps keep loops and costs bounded:

- Another bot (or OI itself) is followed for at most 4 consecutive turns;
  any human message resets the count.
- Each open channel runs at most `open_channel_runs_per_hour` model runs per
  hour (default 6); over the cap, jobs are deferred, not dropped. Summary
  updates count toward it.
- A message asking for more history than `max_context_messages` gets a note
  that only the newest `max_context_messages` messages can be read.

Memory stays bounded: every reply sees the conversation's rolling summary
(rewritten rather than appended, capped at `summary_max_chars`, stale items
decay after `summary_decay_days`) plus the newest `context_messages`
messages. A brand-new summary cold-starts from at most
`summary_bootstrap_days`/`summary_bootstrap_messages` of history, so a
years-old channel is never read end to end.

Commands (`oi config show` lists each watch's `open_channels`):

```bash
oi config watch-add --guild-id 999 --repo /srv/repos/app \
  --open-channels 222,333                      # open channels on the new watch
oi config open-channel-add 444 --watch 999     # one more, later
oi config open-channel-remove 222              # close one again
oi config open-channel-prompt 444 personality  # this one channel gets the personality text
```

A single-channel watch can only open the channel it already watches
(`oi config watch-add --channel-id 222 --repo ... --open-channels 222`).
Inspect, print, and reset what OI remembers about a conversation with
`oi session list`, `oi session show --conversation <id>` (local print, never
logged), and `oi session forget --conversation <id>`.

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

## Model harness: OpenCode or omp

`harness` chooses which agent does the work. The default stays `opencode`.

- `harness = "opencode"` (default): every prompt goes through OpenCode: repository
  replies, reviews, external evidence, web research, and the tool-free prompts
  (classifier, context-request fallback, summaries).
- `harness = "omp"`: omp runs those same jobs in a private environment under
  `~/.local/share/oi/`:
  - tool-free prompts run with `--no-tools`, `--no-session`, `--no-extensions`,
    `--no-skills`, `--no-rules`, and `--no-context-files`, so the model has no
    tools and no authority;
  - repository replies get the tools `read`, `grep`, `glob`, `edit`; reviews and
    external evidence get `read`, `grep`, `glob`; web research gets `read`,
    `web_search`. Each mode runs with `--no-session` and `--no-extensions`.

Every omp tool call also passes `omp_policy.ts`, a fail-closed shim. It runs the
Python decider before the call and blocks the call on any error, unknown tool, or
unresolved path. The decider reuses OpenCode's secret patterns and write-directory
rules. Repository replies on omp try `omp_model` (or `opencode_model`) first, then
each `omp_fallback_models` entry in order, on a provider error (429, 502, 503, 504,
other 5xx) or a timeout. A fallback answer ends with `answered by <model>`. A
non-retryable error, such as 400, does not fall back. `opencode_fallback_models` is
not used for omp replies. `oi doctor` still checks OpenCode.

```bash
oi config set harness omp
oi config set omp_binary omp     # default; set a path if omp is not on PATH
oi doctor
```

Set `omp_model` (a `provider/model` id omp can use) to point omp at a different
provider from `opencode_model`. It then covers every omp call and is the first model
in the omp repository-reply chain, ahead of `omp_fallback_models`. Empty means omp
uses `opencode_model` (or `summary_model` for summaries).

### Residual risk with omp

- `glob` can list file names that match secret patterns, as it does under OpenCode.
- The web read gate checks the hostname as written. A public-looking hostname that
  resolves to a private address passes the gate. This is an inference from the
  decider's code and is not covered by a test.

### Login and doctor

Log in once per provider, under the same private home that omp runs in:

```bash
HOME=$HOME/.local/share/oi/omp/home omp login <provider>
```

`oi doctor` with `harness = "omp"` checks four things: that
`omp_policy.ts` is present in the installed package, that omp can list its models
under the private home, that the configured `omp_model` (or `opencode_model` and
`summary_model`) and each `omp_fallback_models` entry is listed, and that OpenCode is
still authenticated. A missing model prints the exact `omp login` command for its
provider.

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

## Disk use

OI keeps its OpenCode runtime in a private root
(`~/.local/share/oi/opencode/`). OI cleans up after itself:

- At daemon start, and then at most once per day, OI deletes its private
  npm cache: `home/.npm/_cacache` and every stale `home/.npm/_npx/<hash>`
  entry, keeping only the one that holds the binary OI currently runs.
  Each prune logs one bounded `storage prune freed_bytes=<n> removed=<n>`
  line. Runtime resolution prefers a native `opencode` executable over
  `#!` launcher scripts (npx/mise shims, distro wrappers), which is what
  keeps the cache from growing back on every run.
- Deletion never leaves the private root: targets whose resolved path
  escapes it (symlinks) are refused, and `data/`, `config/`, and `state/`
  are never touched.
- `oi storage` prints the size of each private root (`home/.npm`, `data`,
  `state`, `cache`) plus the total, in bytes and human units. Read-only.

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

Upgrade `uv.lock` only with `scripts/upgrade-lock.sh [--dry-run] [pkg …]`, never
with a bare `uv lock --upgrade`. The script resolves against releases published
at least 7 days ago, so a brand-new release cannot enter the lock on the day it
appears. CI and release install with `--locked` and never re-resolve the lock.


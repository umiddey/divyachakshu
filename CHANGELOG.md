# Changelog

## 0.2.2 - 2026-11-20

- New `omp_model` setting. With `harness = "omp"` it is the one model omp uses for every tool-free call (classifier, context-request fallback, summaries), so omp and OpenCode can sit on different providers. `opencode_model` keeps serving OpenCode repository replies. Empty keeps the old behavior (omp uses `opencode_model`, or `summary_model` for summaries). `oi doctor` checks `omp_model` against `omp models` when it is set.
- `opencode_fallback_models` is now allowed with `harness = "omp"`, because repository replies still run on OpenCode.
- Fix: an OpenCode `provider.rate-limit` error with no HTTP status (quota exhausted) now counts as a retryable provider error, so the fallback chain runs instead of the main answer failing.

## 0.2.1 - 2026-11-18

- New `harness = "omp"` option. Tool-free prompts (classifier, context-request
  fallback, summaries) run through omp with every tool, session, and extension
  disabled, in a private environment. Repository replies and research stay on
  OpenCode. The default stays `opencode`.
- `oi doctor` with `harness = "omp"` checks that omp lists the configured chat
  and summary models and names the `omp login` command when one is missing.
- **Breaking:** open channels no longer get the full OI system prompt. They get minimal plumbing (Discord identity, repository list, untrusted-data rule, no-credentials rule, reply marker, silence token). The global `open_channel_prompt` (default `none`, or `personality`) sets the mode; `open_channel_prompts` and `oi config open-channel-prompt <id> none|personality|inherit` override one channel. Open-channel runs are read-only and do not read memory. Non-open-channel prompts are unchanged.
- Model fallback: `opencode_fallback_models` is an ordered chain. A provider error (429, 502, 503, 504, other 5xx) or a timeout on one model runs the same request on the next model. A fallback answer ends with `answered by <model>`. `opencode_fallback_total_timeout_seconds` caps the whole chain. Raw-prompt paths and web research stay single-model. The default (empty list) is unchanged.

## 0.2.0 - 2026-11-18

**Breaking:** this release is 0.2.0, not 0.1.2, because the session keys
`max_session_turns` and `session_ttl_days` are removed. Old config files
still load: the loader accepts and drops the two keys and never writes them
back.

### Added

- `oi storage`: prints the size of each OI private root (`home/.npm`,
  `data`, `state`, `cache`) plus the total, in bytes and human units.
  Read-only.
- **Open channels**: a per-watch `open_channels` list marks channels where
  OI reads every non-self message — humans and other bots, no mention
  needed — and replies only when it has something concrete to add.
  Commands: `oi config watch-add ... --open-channels <id>[,<id>…]`,
  `oi config open-channel-add <id> --watch <id>`, and
  `oi config open-channel-remove <id>`; `oi config show` lists them.
  Silence is a first-class outcome: the model returns the `[[OI_SILENT]]`
  token and nothing is posted. Cost caps: another bot is followed for at
  most 4 consecutive turns (any human message resets the count), and each
  open channel runs at most `open_channel_runs_per_hour` model runs per hour
  (default 6); over the cap, jobs are deferred, not dropped.
- **Stateless calls with a rolling conversation summary**: every call runs
  in a brand-new OpenCode session, fed the conversation's rolling summary
  plus the newest `context_messages` messages. The summary is rewritten —
  never appended — capped at `summary_max_chars` (default 1500); stale
  items decay after `summary_decay_days` (default 60); a brand-new summary
  cold-starts from at most `summary_bootstrap_days` /
  `summary_bootstrap_messages` (30 days / 300 messages). `summary_model`
  optionally picks a different model for summary runs (empty =
  `opencode_model`). Inspect and manage summaries with `oi session list`,
  `oi session show --conversation <id>` (local print, never logged), and
  `oi session forget --conversation <id>`.
- Clamp notice: when a message asks for more history than
  `max_context_messages`, the reply ends with a note that only the newest
  `max_context_messages` messages can be read. The notice is never attached
  to a silent outcome.

### Changed

- OI cleans up after itself: at daemon start and then at most once per day
  it prunes its private npm cache (`home/.npm/_cacache` and stale
  `home/.npm/_npx/*` entries), keeping only the entry that holds the binary
  OI currently runs. Each prune logs one bounded
  `storage prune freed_bytes=<n> removed=<n>` line. Deletion never leaves
  the private root; symlink escapes are refused and never raised into the
  daemon.
- Runtime resolution prefers native `opencode` executables (ELF/Mach-O) over
  `#!` launcher scripts, PATH order preserved within each group. npx/mise
  launchers no longer hit the registry and repopulate the private npm cache
  on every run.
- Reminders now go to each server's own reminder channel via the per-`[[watch]]`
  `reminder_channel_id` field (`0` = no reminders in that server). Migrate with
  `oi config watch-add ... --reminder-channel-id <id>`; a non-zero top-level
  `reminder_channel_id` is refused at load with a message naming its `[[watch]]`
  entry as the new home.
- `!fresh` / `!new` also deletes the conversation's rolling summary, so the
  next reply starts from scratch.
- Storage rule: the per-conversation rolling summary — capped at
  `summary_max_chars`, schema-validated by a deterministic controller with
  URLs stripped and secret-looking items dropped, and never logged — is now
  the ONLY model-written text stored in SQLite.

### Removed

- Session resume. Conversations no longer map to OpenCode sessions: every
  call is a new session, the `opencode_sessions` table is dropped by
  migration, and rotation/TTL are gone (`max_session_turns`,
  `session_ttl_days`; old configs still load, the keys are dropped).
  `oi session list|forget|show` now operate on the rolling summaries.

### Fixed

- Discord history evidence lines render admitted messages whole up to
  Discord's own 4,000-character message maximum instead of silently
  truncating at 600 characters; longer lines end with an explicit
  `[clipped: <kept> of <total> chars]` marker so the model never has to
  guess whether it saw the whole message.
- "pic", "photo", "Bild(er)" and "Foto(s)" now count as explicit
  history-attachment references ("the one in the pic", "the pics above",
  "auf dem Bild oben"), like "the image" already did. Which messages may
  load attachments is unchanged: still only the rendered, authorized
  history window on approved Discord CDN hosts.

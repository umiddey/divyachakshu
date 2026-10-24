# Changelog

## Unreleased

### Added

- `oi storage`: prints the size of each OI private root (`home/.npm`,
  `data`, `state`, `cache`) plus the total, in bytes and human units.
  Read-only.

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

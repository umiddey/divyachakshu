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

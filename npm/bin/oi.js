#!/usr/bin/env node
// divyachakshu npm wrapper: runs the real Python OI (PyPI distribution
// "divyachakshu") through a private `uv tool` install, so the npm `oi` never
// collides with a user's own `oi`. Node built-ins only. Installs on first
// run — never at npm install time (users may run --ignore-scripts; there
// are no install hooks and no npm dependencies).
"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

function fail(message) {
  process.stderr.write(`divyachakshu: ${message}\n`);
  process.exit(1);
}

if (process.platform === "win32") {
  fail("Windows is not supported; run OI under WSL2 (Linux) instead.");
}

const pkg = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"),
);
// Dev hook: OI_NPM_PACKAGE_SPEC overrides the pinned spec (the sandbox smoke
// uses it to install a locally built wheel); see the npm README.
const spec =
  process.env.OI_NPM_PACKAGE_SPEC || `divyachakshu==${pkg.version}`;

// Private uv roots under the user data dir. The pinned OI lives in
// <data>/divyachakshu/npm/{tools,bin}, separate from uv's default locations and
// from any `oi` the user installed by hand.
function dataHome() {
  if (process.platform === "darwin") {
    return path.join(os.homedir(), "Library", "Application Support");
  }
  const xdg = process.env.XDG_DATA_HOME;
  if (xdg && path.isAbsolute(xdg)) return xdg;
  return path.join(os.homedir(), ".local", "share");
}

const toolDir = path.join(dataHome(), "divyachakshu", "npm", "tools"); // UV_TOOL_DIR
const toolBinDir = path.join(dataHome(), "divyachakshu", "npm", "bin"); // UV_TOOL_BIN_DIR
const privateOi = path.join(toolBinDir, "oi");

function isExecutableFile(p) {
  try {
    return fs.statSync(p).isFile() && fs.accessSync(p, fs.constants.X_OK) === undefined;
  } catch {
    return false;
  }
}

function findUv() {
  const dirs = (process.env.PATH || "")
    .split(path.delimiter)
    .filter(Boolean)
    .concat([
      path.join(os.homedir(), ".local", "bin"),
      path.join(os.homedir(), ".cargo", "bin"),
    ]);
  for (const dir of dirs) {
    const candidate = path.join(dir, "uv");
    if (isExecutableFile(candidate)) return candidate;
  }
  return null;
}

function uvEnv(extra) {
  return {
    ...process.env,
    UV_TOOL_DIR: toolDir,
    UV_TOOL_BIN_DIR: toolBinDir,
    ...extra,
  };
}

function runCapture(uv, args, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(uv, args, {
      env,
      stdio: ["ignore", "pipe", "ignore"],
    });
    let out = "";
    child.stdout.on("data", (chunk) => {
      out += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => resolve({ code, out }));
  });
}

function runInstall(uv, args, env) {
  return new Promise((resolve, reject) => {
    // Installer and uv progress go to stderr only; stdout stays reserved for
    // the `oi` command the user invoked.
    const child = spawn(uv, args, {
      env,
      stdio: ["ignore", "ignore", "inherit"],
    });
    child.on("error", reject);
    child.on("close", (code) => resolve(code));
  });
}

// uv tool list prints one "<name> v<version>" line per installed tool.
function installedTools(uv) {
  return runCapture(uv, ["tool", "list"], uvEnv()).then(({ code, out }) => {
    if (code !== 0) return new Map();
    const records = new Map();
    for (const line of out.split("\n")) {
      const match = /^(\S+) v(\S+)\s*$/.exec(line);
      if (match) records.set(match[1], match[2]);
    }
    return records;
  });
}

// The default spec is an exact `name==version` pin and must match the private
// install. Overridden specs (dev hook, e.g. a local wheel path) only require
// that the private tool dir already holds a usable install.
function installedSpecMatches(records) {
  const pin = spec.split("==");
  if (pin.length === 2) return records.get(pin[0]) === pin[1];
  return records.size > 0;
}

async function main() {
  let uv = findUv();
  if (!uv) {
    process.stderr.write(
      "divyachakshu: uv not found. Installing uv with the official installer\n" +
        "  (curl -LsSf https://astral.sh/uv/install.sh | sh, UV_NO_MODIFY_PATH=1,\n" +
        "  PATH is not modified), then installing OI into\n" +
        `  ${toolDir}\n`,
    );
    const code = await runInstall(
      "sh",
      ["-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"],
      uvEnv({ UV_NO_MODIFY_PATH: "1" }),
    ).catch((err) => fail(`uv installer could not start: ${err.message}`));
    if (code !== 0) {
      fail(`uv installer failed (exit ${code}); install uv and rerun oi.`);
    }
    uv = findUv();
    if (!uv) {
      fail(
        "uv installer finished but uv was not found on PATH, ~/.local/bin or ~/.cargo/bin.",
      );
    }
  }

  if (isExecutableFile(privateOi)) {
    const records = await installedTools(uv);
    if (installedSpecMatches(records)) {
      return spawnOi();
    }
    process.stderr.write(
      `divyachakshu: upgrading private OI to ${spec} (uv tool dir: ${toolDir})...\n`,
    );
  } else {
    process.stderr.write(
      `divyachakshu: installing ${spec} into the private uv tool dir ${toolDir}...\n`,
    );
  }
  const code = await runInstall(
    uv,
    ["tool", "install", "--force", "--python", "3.11", spec],
    uvEnv(),
  ).catch((err) => fail(`uv tool install could not start: ${err.message}`));
  if (code !== 0) fail(`uv tool install ${spec} failed (exit ${code}).`);
  if (!isExecutableFile(privateOi)) {
    fail(`uv tool install finished but ${privateOi} is missing.`);
  }
  spawnOi();
}

function spawnOi() {
  const child = spawn(privateOi, process.argv.slice(2), { stdio: "inherit" });
  child.on("error", (err) =>
    fail(`cannot run installed oi at ${privateOi}: ${err.message}`),
  );
  let settled = false;
  child.on("close", (code, signal) => {
    settled = true;
    if (signal) {
      // Die the way the child died so the shell sees the same signal.
      for (const sig of FORWARDED_SIGNALS) process.removeAllListeners(sig);
      process.kill(process.pid, signal);
      process.exit(1);
    }
    process.exit(code === null ? 0 : code);
  });
  for (const sig of FORWARDED_SIGNALS) {
    process.on(sig, () => {
      if (!settled && child.pid) child.kill(sig);
    });
  }
}

const FORWARDED_SIGNALS = ["SIGINT", "SIGTERM", "SIGHUP"];

main().catch((err) => fail(err && err.message ? err.message : String(err)));

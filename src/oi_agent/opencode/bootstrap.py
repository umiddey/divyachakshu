"""Safe OpenCode dependency, authentication, and model bootstrap helpers."""

from __future__ import annotations

import os
import platform
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .native import NATIVE_DB_RELATIVE
from .paths import OpenCodePaths

# The implemented contract is native v2 only. v1 used `agent.<id>.permission`
# maps and v1 action names (`bash`, `task`) that v2 ignores, so a v1 binary
# would run the model under the native allow-by-default base policy while OI
# believed it was confined. v1 is therefore rejected outright rather than
# carried with a compatibility shim that would not be enforced.
MIN_OPENCODE_VERSION = (2, 0, 0)
INSTALL_DOCS_URL = "https://opencode.ai/docs/#install"
# Doctor/init probes must answer quickly. A v2 CLI that cannot (for example
# one looping because the user's own `opencode serve --service` already holds
# the managed port 49374) must surface as a reported failure, not a 600 s hang.
PROBE_TIMEOUT_SECONDS = 15.0
# Interactive login hands the terminal to the operator; it is not a probe.
LOGIN_TIMEOUT_SECONDS = 600.0
_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
_MODEL_RE = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_./:@+-]+)")


@dataclass(frozen=True)
class OpenCodeVersion:
    """Parsed compatible OpenCode version and the executable path."""

    binary: str
    version: tuple[int, int, int]
    raw: str


class OpenCodeBootstrapError(RuntimeError):
    """Raised when OpenCode cannot satisfy the OI runtime contract."""


def resolve_binary(binary: str) -> str:
    """Resolve an executable without shell interpolation.

    Args:
        binary: Command name or filesystem path.

    Returns:
        Absolute executable path.

    Raises:
        OpenCodeBootstrapError: If the executable is missing or not runnable.
    """
    if not isinstance(binary, str) or not binary.strip():
        raise OpenCodeBootstrapError("OpenCode binary is empty")
    candidate = Path(binary).expanduser()
    if candidate.parent != Path("."):
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise OpenCodeBootstrapError(
                f"OpenCode binary cannot be resolved: {binary!r}"
            ) from exc
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
    else:
        found = shutil.which(binary)
        if found:
            return str(Path(found).resolve())
    raise OpenCodeBootstrapError(
        f"OpenCode executable not found or not executable: {binary!r}; "
        f"install it from {INSTALL_DOCS_URL}"
    )


def check_version(binary: str, paths: OpenCodePaths) -> OpenCodeVersion:
    """Verify the executable responds with a supported semantic version.

    Args:
        binary: Configured executable command or path.
        paths: Isolated OpenCode environment.

    Returns:
        Parsed version information.

    Raises:
        OpenCodeBootstrapError: On execution failure or incompatible version.
    """
    resolved = resolve_binary(binary)
    try:
        completed = subprocess.run(
            [resolved, "--version"],
            env=paths.environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpenCodeBootstrapError(
            "OpenCode exists but `opencode --version` failed"
        ) from exc
    raw = (completed.stdout or "").strip()
    match = _VERSION_RE.search(raw)
    if completed.returncode != 0 or match is None:
        raise OpenCodeBootstrapError(
            "OpenCode returned no parseable compatible version"
        )
    version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    # Exactly major 2. A future major could change the permission schema
    # entirely, and silently running under an unproven contract is the same
    # failure as running under v1's ignored rules.
    if version[0] != MIN_OPENCODE_VERSION[0]:
        raise OpenCodeBootstrapError(
            f"OpenCode {'.'.join(map(str, version))} is not a supported native v"
            f"{MIN_OPENCODE_VERSION[0]} release; OI implements the v"
            f"{MIN_OPENCODE_VERSION[0]} permission contract only. Install a v"
            f"{MIN_OPENCODE_VERSION[0]} release from {INSTALL_DOCS_URL}"
        )
    if version < MIN_OPENCODE_VERSION:
        minimum = ".".join(map(str, MIN_OPENCODE_VERSION))
        raise OpenCodeBootstrapError(
            f"OpenCode {'.'.join(map(str, version))} is too old; minimum is {minimum}"
        )
    return OpenCodeVersion(resolved, version, raw)


# Official installer target (curl -fsSL https://opencode.ai/install | bash).
OFFICIAL_INSTALL_DIR = Path("~/.opencode/bin")

# Magic bytes of native executables: ELF and Mach-O (32/64-bit, both endians,
# plus fat/universal). Anything else — typically a `#!` script — is a launcher.
_NATIVE_MAGICS: tuple[bytes, ...] = (
    b"\x7fELF",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
)

_runtime_binaries: dict[str, str] = {}


def _runtime_candidates(binary: str) -> list[Path]:
    """List every executable a bare command name could mean, PATH order first."""
    candidate = Path(binary).expanduser()
    if candidate.parent != Path("."):
        return [candidate]
    seen: list[Path] = []
    dirs = [Path(d) for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    for directory in [*dirs, OFFICIAL_INSTALL_DIR.expanduser()]:
        path = directory / binary
        if path.is_file() and os.access(path, os.X_OK):
            resolved = path.resolve()
            if resolved not in seen:
                seen.append(resolved)
    return seen


def _is_native_binary(path: Path) -> bool:
    """Return True when the file carries native ELF/Mach-O executable magic.

    PATH candidates are frequently `#!` launcher scripts (npx/mise shims,
    distro wrappers) that re-resolve the real binary on every invocation and
    repopulate the npm cache. Reading the first magic bytes classifies them
    cheaply and without running anything.
    """
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError:
        return False
    return magic in _NATIVE_MAGICS


def _ordered_runtime_candidates(binary: str) -> list[Path]:
    """Rank candidates native executables first, PATH order kept per group.

    Args:
        binary: Bare executable name to resolve.

    Returns:
        Resolved candidate paths, native binaries before `#!` scripts.
    """
    candidates = _runtime_candidates(binary)
    native = [path for path in candidates if _is_native_binary(path)]
    scripts = [path for path in candidates if not _is_native_binary(path)]
    return [*native, *scripts]


def resolve_runtime_binary(binary: str, paths: OpenCodePaths) -> str:
    """Return the first candidate that passes `check_version` in OI's isolation.

    The first `opencode` on PATH is often a launcher (npx/mise shims, distro
    wrappers) that installs a different major or breaks under OI's disposable
    HOME. Candidates are tried native executables first (PATH order inside
    each group), so a real release binary always beats an earlier launcher
    script. Every tried candidate is version-checked in the isolated
    environment and the first passing one is cached for the process.

    Raises:
        OpenCodeBootstrapError: When no candidate passes, naming each rejection.
    """
    cached = _runtime_binaries.get(binary)
    if cached is not None:
        return cached
    rejected: list[str] = []
    for candidate in _ordered_runtime_candidates(binary):
        try:
            version = check_version(str(candidate), paths)
        except OpenCodeBootstrapError as exc:
            rejected.append(f"{candidate}: {exc}")
            continue
        _runtime_binaries[binary] = version.binary
        return version.binary
    detail = "; ".join(rejected) or "no executable found"
    raise OpenCodeBootstrapError(
        f"No usable OpenCode for {binary!r} ({detail}); install a supported "
        f"release from {INSTALL_DOCS_URL}"
    )


def install_command() -> list[str]:
    """Select one fixed official installer command available on this host."""
    if shutil.which("npm"):
        return ["npm", "install", "--global", "opencode-ai"]
    if platform.system() == "Darwin" and shutil.which("brew"):
        return ["brew", "install", "anomalyco/tap/opencode"]
    if shutil.which("mise"):
        return ["mise", "use", "--global", "npm:opencode-ai"]
    raise OpenCodeBootstrapError(
        f"No supported installer found; install OpenCode from {INSTALL_DOCS_URL}"
    )


def install_opencode(paths: OpenCodePaths) -> str:
    """Install OpenCode through a fixed official package-manager command.

    Args:
        paths: Isolated environment used for the installer subprocess.

    Returns:
        Resolved installed executable path.

    Raises:
        OpenCodeBootstrapError: If installation or post-install verification fails.
    """
    command = install_command()
    try:
        completed = subprocess.run(
            command,
            env=paths.environment(),
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpenCodeBootstrapError("OpenCode installation could not start") from exc
    if completed.returncode != 0:
        raise OpenCodeBootstrapError(
            f"OpenCode installer exited with status {completed.returncode}"
        )
    return resolve_binary("opencode")


def _run(binary: str, args: list[str], paths: OpenCodePaths,
         *, inherit_stdio: bool = False,
         timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run one allowlisted OpenCode bootstrap command.

    Every subcommand runs ``--standalone``: without it a v2 CLI connects to
    (or starts) the managed background service on 127.0.0.1:49374, and when
    the user's own `opencode serve --service` already holds that port the CLI
    loops forever in OI's isolated environment. The default bounded timeout
    turns any remaining wedge into a reported failure instead of a hang;
    interactive commands pass their own timeout.
    """
    resolved = resolve_binary(binary)
    kwargs = {
        "env": paths.environment(),
        "check": False,
        "timeout": PROBE_TIMEOUT_SECONDS if timeout is None else timeout,
    }
    if not inherit_stdio:
        kwargs.update(capture_output=True, text=True)
    return subprocess.run([resolved, *args, "--standalone"], **kwargs)


def credential_integrations(paths: OpenCodePaths) -> frozenset[str]:
    """Return integration IDs from rows not explicitly deactivated, read-only.

    Reuses the read pattern of native.copy_isolated_credentials: the isolated
    native database is opened read-only and only the ``integration_id``
    column is selected, so no credential value ever enters OI memory. The
    ``active != 0`` exclusion is a CONSERVATIVE, fail-closed choice: v2.0.5
    itself inserts credential rows without ``active`` (NULL) and its
    credential lookup does not filter on the flag at all, so excluding an
    explicit ``0`` can only cause a false FAIL here — never a false pass.
    """
    database = paths.data_home / NATIVE_DB_RELATIVE
    if not database.is_file():
        return frozenset()
    try:
        connection = sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro", uri=True
        )
    except sqlite3.Error:
        return frozenset()
    try:
        rows = connection.execute(
            "SELECT integration_id FROM credential WHERE active IS NOT 0"
        ).fetchall()
    except sqlite3.Error:
        return frozenset()
    finally:
        connection.close()
    return frozenset(str(row[0]) for row in rows if row and row[0])


def authenticated(paths: OpenCodePaths) -> bool:
    """Return whether the isolated native store holds a provider credential.

    v2 ``auth list`` reports INTEGRATIONS, not provider credentials: on a
    working install it prints "No authenticated integrations" while runs
    authenticate fine from the credential table, so parsing that output
    returned True by accident. The honest probe is the credential store
    itself (see credential_integrations). A probe that cannot run, times out,
    or finds no store is reported as not authenticated.
    """
    return bool(credential_integrations(paths))


def authenticate(binary: str, paths: OpenCodePaths) -> None:
    """Hand an interactive terminal to OpenCode authentication."""
    try:
        completed = _run(binary, ["auth", "login"], paths,
                         inherit_stdio=True, timeout=LOGIN_TIMEOUT_SECONDS)
    except subprocess.SubprocessError as exc:
        raise OpenCodeBootstrapError(
            f"OpenCode authentication did not complete within "
            f"{LOGIN_TIMEOUT_SECONDS:g}s; rerun `opencode auth login --standalone`"
        ) from exc
    except OSError as exc:
        raise OpenCodeBootstrapError(
            "OpenCode authentication could not start; verify the OpenCode install"
        ) from exc
    if completed.returncode != 0 or not authenticated(paths):
        raise OpenCodeBootstrapError(
            "OpenCode authentication was not completed; rerun "
            "`opencode auth login --standalone`"
        )


def list_models(binary: str, paths: OpenCodePaths) -> list[str]:
    """Return exact provider/model IDs from OpenCode's model listing."""
    try:
        completed = _run(binary, ["models"], paths)
    except subprocess.TimeoutExpired as exc:
        raise OpenCodeBootstrapError(
            f"OpenCode model listing did not finish within "
            f"{PROBE_TIMEOUT_SECONDS:g}s; another OpenCode process may be "
            "holding the managed service port"
        ) from exc
    except (OpenCodeBootstrapError, OSError, subprocess.SubprocessError) as exc:
        raise OpenCodeBootstrapError("OpenCode model listing failed") from exc
    if completed.returncode != 0:
        raise OpenCodeBootstrapError("OpenCode model listing failed")
    models = sorted(set(_MODEL_RE.findall(completed.stdout or "")))
    return models


@dataclass(frozen=True)
class ModelStatus:
    """Outcome of a model availability probe.

    ``available=False`` is a hard failure. ``available=True`` with
    ``verified=False`` is the empty-listing fallback: a provider credential
    exists, but OpenCode's own model listing could not confirm the exact id
    (the listing is also empty on working installs, so it carries no model-id
    signal). Callers must surface that state as a warning — it proves the
    provider is credentialed, NOT that the model id exists or that a run
    would succeed.
    """

    available: bool
    verified: bool


def model_status(binary: str, paths: OpenCodePaths, model: str) -> ModelStatus:
    """Probe whether `model` can run in the isolated native environment.

    A non-empty `models` listing is the authoritative availability set: a
    listed id is (available, verified), an unlisted id is unavailable. An
    empty listing is a proven v2.0.5 shape (`models --standalone` exits 0
    with no output even on working installs), so it must not false-fail: the
    probe falls back to the credential store for the model's provider prefix
    and returns the distinct unverified-availability state documented on
    ModelStatus.

    What `models` filters on (v2.0.5): its help says only "List all
    available models" with no filter flags. Observed 2026-10-11 on the
    operator's machine: `opencode models` and `opencode models --standalone`
    both exit 0 with EMPTY output, in the operator's normal HOME as well as
    in OI's isolated environment, whether or not credentials exist. So the
    empty listing carries no auth or availability signal (hence the
    fallback). The non-empty path is kept for CLIs that do list models; it
    deliberately does NOT require a provider credential, because requiring
    one would false-fail models reachable via environment-provided keys that
    never touch the credential store.
    """
    models = list_models(binary, paths)
    if models:
        return ModelStatus(model in models, True)
    provider = model.split("/", 1)[0]
    return ModelStatus(provider in credential_integrations(paths), False)


def bootstrap(binary: str, paths: OpenCodePaths, *, install: bool = False,
              login: bool = True) -> OpenCodeVersion:
    """Verify OpenCode and optionally install/authenticate it in isolation.

    Args:
        binary: Configured OpenCode executable.
        paths: Shared isolated OpenCode paths.
        install: Permit the selected package-manager installation path.
        login: Run interactive auth login when no provider is configured.

    Returns:
        Verified executable version.
    """
    paths.ensure()
    try:
        version = check_version(binary, paths)
    except OpenCodeBootstrapError:
        if not install:
            raise
        installed = install_opencode(paths)
        version = check_version(installed, paths)
    if login and not authenticated(paths):
        authenticate(version.binary, paths)
    return version

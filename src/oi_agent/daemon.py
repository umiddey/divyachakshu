"""Unprivileged systemd/launchd installation and OpenCode-aware daemon diagnostics."""

from __future__ import annotations

import os
import plistlib
import pwd
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, DEFAULT_SECRETS_PATH, Config
from .opencode.bootstrap import (
    OpenCodeBootstrapError,
    authenticated,
    check_version,
    model_status,
)
from .opencode.paths import resolve_opencode_paths
from .store import Store

SERVICE_DIR = Path("/etc/systemd/system")
DEFAULT_SERVICE_NAME = "oi"
DEFAULT_DAEMON_USER = "oi"
LAUNCHD_LABEL_PREFIX = "ai.divyachakshu"
LAUNCHD_LOG_TAIL_LINES = 200


class DaemonError(RuntimeError):
    """Raised for unsafe or incomplete daemon provisioning operations."""


@dataclass(frozen=True)
class DaemonIdentity:
    """Dedicated service account identity used by generated units."""

    user: str
    home: Path
    uid: int
    gid: int


def _identity(user: str, home: Path | None = None) -> DaemonIdentity:
    """Resolve a system user without copying personal operator state."""
    try:
        record = pwd.getpwnam(user)
    except KeyError as exc:
        if home is None:
            raise DaemonError(
                f"service user {user!r} does not exist; run daemon install as root"
            ) from exc
        return DaemonIdentity(user, home, -1, -1)
    return DaemonIdentity(user, Path(record.pw_dir), record.pw_uid, record.pw_gid)


def _quote(value: str | Path) -> str:
    """Quote one systemd Environment value."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def render_unit(cfg: Config, identity: DaemonIdentity,
                *, service_name: str = DEFAULT_SERVICE_NAME,
                config_path: Path | None = None,
                secrets_path: Path | None = None,
                oi_binary: str = "/usr/local/bin/oi",
                user_mode: bool = False) -> str:
    """Render the systemd unit without performing filesystem actions.

    Args:
        cfg: Validated OpenCode-only configuration.
        identity: Dedicated unprivileged service user.
        service_name: Unit name without the ``.service`` suffix.
        config_path: OI config path owned by the daemon user.
        secrets_path: OI Discord-token env file.
        oi_binary: Deterministic OI executable path used by ExecStart and PATH.
        user_mode: Render as user-level service without User=/Group= directives.

    Returns:
        Complete systemd unit text.
    """
    config_path = config_path or identity.home / ".config/oi/config.toml"
    secrets_path = secrets_path or identity.home / ".config/oi/.env"
    paths = resolve_opencode_paths(identity.home)
    db_value = cfg.db_path
    db_path = (identity.home / db_value[2:].lstrip("/")
               if db_value.startswith("~/")
               else Path(db_value).expanduser())
    if not db_path.is_absolute():
        db_path = identity.home / db_path
    read_write = {
        config_path.parent,
        secrets_path.parent,
        db_path.parent,
        paths.config_home,
        paths.data_home,
        paths.state_home,
        paths.cache_home,
        *(Path(p).expanduser().resolve() for target in cfg.watches for p in target.repo_paths if p),
    }
    rw_paths = " ".join(sorted(str(path) for path in read_write))
    path_value = _daemon_path_value(cfg, oi_binary, identity.home)
    if user_mode:
        return "\n".join([
            "[Unit]",
            "Description=OI Discord OpenCode auditor",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={identity.home}",
            f"Environment=HOME={_quote(identity.home)}",
            f"Environment=PATH={_quote(path_value)}",
            f"Environment=OI_CONFIG_PATH={_quote(config_path)}",
            f"Environment=OI_SECRETS_PATH={_quote(secrets_path)}",
            "Environment=OPENCODE_DISABLE_PROJECT_CONFIG=1",
            f"EnvironmentFile=-{secrets_path}",
            f"ExecStart={oi_binary} run --config {config_path}",
            "Restart=always",
            "RestartSec=10",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ])
    return "\n".join([
        "[Unit]",
        "Description=OI Discord OpenCode auditor",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"User={identity.user}",
        f"Group={identity.user}",
        f"WorkingDirectory={identity.home}",
        f"Environment=HOME={_quote(identity.home)}",
        f"Environment=PATH={_quote(path_value)}",
        f"Environment=OI_CONFIG_PATH={_quote(config_path)}",
        f"Environment=OI_SECRETS_PATH={_quote(secrets_path)}",
        f"Environment=XDG_CONFIG_HOME={_quote(paths.config_home)}",
        f"Environment=XDG_DATA_HOME={_quote(paths.data_home)}",
        f"Environment=XDG_STATE_HOME={_quote(paths.state_home)}",
        f"Environment=XDG_CACHE_HOME={_quote(paths.cache_home)}",
        "Environment=OPENCODE_DISABLE_PROJECT_CONFIG=1",
        f"EnvironmentFile=-{secrets_path}",
        f"ExecStart={oi_binary} run --config {config_path}",
        "Restart=always",
        "RestartSec=10",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        "RestrictRealtime=true",
        "RestrictNamespaces=true",
        f"ReadWritePaths={rw_paths}",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def _run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run one fixed system administration argv list."""
    return subprocess.run(command, capture_output=True, text=True,
                          check=check, timeout=120)


def is_darwin() -> bool:
    """True on macOS, where launchd replaces systemd as the service manager."""
    return sys.platform == "darwin"


def launchd_label(service_name: str = DEFAULT_SERVICE_NAME) -> str:
    """Reverse-DNS LaunchAgent label for one service name."""
    return f"{LAUNCHD_LABEL_PREFIX}.{service_name}"


def resolve_oi_binary() -> str:
    """Absolute path of the real Python ``oi`` running right now.

    Resolves ``sys.argv[0]`` first so a generated unit/plist never points at
    the npm node shim, then falls back to a ``PATH`` lookup.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    candidate: Path | None = None
    if argv0:
        direct = Path(argv0).expanduser()
        if direct.is_absolute() or os.sep in argv0:
            candidate = direct
        else:
            found = shutil.which(argv0)
            candidate = Path(found) if found else None
    if candidate is not None:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        if resolved.is_file():
            return str(resolved)
    return shutil.which("oi") or "/usr/local/bin/oi"


def _daemon_path_value(cfg: Config, oi_binary: str, home: Path) -> str:
    """PATH value shared by the systemd unit and the launchd plist."""
    opencode_bin = shutil.which(cfg.opencode_binary) or shutil.which("opencode")
    opencode_dir = f":{Path(opencode_bin).parent}" if opencode_bin else ""
    return f"{Path(oi_binary).parent}:{home}/.local/bin{opencode_dir}:/usr/local/bin:/usr/bin"


def _launchd_plist_path(service_name: str = DEFAULT_SERVICE_NAME,
                        home: Path | None = None) -> Path:
    """LaunchAgent plist location for one service name."""
    base = home if home is not None else Path.home()
    return base / "Library" / "LaunchAgents" / f"{launchd_label(service_name)}.plist"


def _launchd_log_path(service_name: str = DEFAULT_SERVICE_NAME,
                      home: Path | None = None) -> Path:
    """LaunchAgent stdout/stderr log location for one service name."""
    base = home if home is not None else Path.home()
    return base / "Library" / "Logs" / "oi" / f"{service_name}.log"


def render_launchd_plist(cfg: Config, *, service_name: str = DEFAULT_SERVICE_NAME,
                         config_path: Path | None = None,
                         secrets_path: Path | None = None,
                         oi_binary: str | None = None,
                         home: Path | None = None) -> bytes:
    """Render the LaunchAgent plist as plistlib XML without filesystem actions.

    Args:
        cfg: Validated OpenCode-only configuration.
        service_name: Service name appended to the ``ai.divyachakshu`` label prefix.
        config_path: OI config path passed to ``oi run --config``.
        secrets_path: OI Discord-token env file, exported as ``OI_SECRETS_PATH``.
        oi_binary: Real Python ``oi`` executable; resolved from the running
            process when omitted.
        home: User home root; defaults to the current user's home.

    Returns:
        Serialized XML plist bytes ready for ``~/Library/LaunchAgents``.
    """
    home = Path(home) if home is not None else Path.home()
    config_path = Path(config_path) if config_path is not None \
        else home / ".config/oi/config.toml"
    secrets_path = Path(secrets_path) if secrets_path is not None \
        else home / ".config/oi/.env"
    oi_bin = oi_binary or resolve_oi_binary()
    log_path = _launchd_log_path(service_name, home)
    payload = {
        "Label": launchd_label(service_name),
        "ProgramArguments": [oi_bin, "run", "--config", str(config_path)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {
            "HOME": str(home),
            "PATH": _daemon_path_value(cfg, oi_bin, home),
            "OI_CONFIG_PATH": str(config_path),
            "OI_SECRETS_PATH": str(secrets_path),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        },
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML)


def _install_launchd(cfg: Config, *, service_name: str = DEFAULT_SERVICE_NAME,
                     config_path: Path = DEFAULT_CONFIG_PATH,
                     secrets_path: Path = DEFAULT_SECRETS_PATH,
                     dry_run: bool = False) -> bytes:
    """Write the LaunchAgent plist and bootstrap it into the user's GUI domain.

    Raises:
        DaemonError: If ``launchctl bootstrap`` or ``kickstart`` fails.
    """
    home = Path.home()
    daemon_config = config_path if config_path.exists() else home / ".config/oi/config.toml"
    daemon_secrets = secrets_path if secrets_path.exists() else home / ".config/oi/.env"
    plist = render_launchd_plist(cfg, service_name=service_name,
                                 config_path=daemon_config, secrets_path=daemon_secrets,
                                 home=home)
    if dry_run:
        return plist
    plist_path = _launchd_plist_path(service_name, home)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plist)
    _launchd_log_path(service_name, home).parent.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    target = f"gui/{uid}/{launchd_label(service_name)}"
    # bootout first; "not loaded" failures are expected on fresh installs.
    _run(["launchctl", "bootout", target])
    result = _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DaemonError(
            f"launchctl bootstrap failed: {detail}" if detail
            else "launchctl bootstrap failed"
        )
    result = _run(["launchctl", "kickstart", "-k", target])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DaemonError(
            f"launchctl kickstart failed: {detail}" if detail
            else "launchctl kickstart failed"
        )
    return plist


def _launchd_lifecycle(action: str, service_name: str) -> str:
    """Run one non-destructive launchctl status/restart command."""
    target = f"gui/{os.getuid()}/{launchd_label(service_name)}"
    command = (["launchctl", "kickstart", "-k", target] if action == "restart"
               else ["launchctl", "print", target])
    result = _run(command)
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        raise DaemonError(output or f"launchctl exited {result.returncode}")
    return output


def _launchd_logs(service_name: str) -> str:
    """Return the tail of the LaunchAgent log file."""
    log_path = _launchd_log_path(service_name)
    if not log_path.is_file():
        raise DaemonError(f"no LaunchAgent log file yet: {log_path}")
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-LAUNCHD_LOG_TAIL_LINES:])


def _launchd_stream_logs(service_name: str) -> None:
    """Follow the LaunchAgent log file interactively without timeout."""
    try:
        subprocess.run(["tail", "-f", str(_launchd_log_path(service_name))], check=False)
    except KeyboardInterrupt:
        pass


def _launchd_uninstall(service_name: str) -> None:
    """Bootout the LaunchAgent and remove its plist."""
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{launchd_label(service_name)}"])
    plist_path = _launchd_plist_path(service_name)
    if plist_path.exists():
        plist_path.unlink()


def ensure_user(identity: DaemonIdentity, *, dry_run: bool = False) -> None:
    """Create the dedicated system user and private directory hierarchy."""
    if identity.uid == -1:
        if dry_run:
            return
        command = ["useradd", "--system", "--create-home", "--shell",
                   "/usr/sbin/nologin", "--home-dir", str(identity.home),
                   identity.user]
        result = _run(command)
        if result.returncode != 0:
            raise DaemonError(f"could not create service user {identity.user!r}")
        identity = _identity(identity.user, identity.home)
    if dry_run:
        return
    for path in (
        identity.home / ".config",
        identity.home / ".config/oi",
        identity.home / ".local",
        identity.home / ".local/state",
        identity.home / ".local/state/oi",
        identity.home / ".local/share",
        identity.home / ".local/share/oi",
        identity.home / ".local/bin",
    ):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        if identity.uid != -1 and identity.gid != -1:
            try:
                shutil.chown(path, identity.user, identity.user)
            except (OSError, PermissionError):
                pass


def ensure_opencode_for_user(cfg: Config, identity: DaemonIdentity,
                             *, dry_run: bool = False) -> None:
    """Ensure the daemon user can execute OpenCode and owns auth state."""
    if dry_run:
        return
    paths = resolve_opencode_paths(identity.home)
    paths.ensure(uid=identity.uid, gid=identity.gid)
    for parent in (
        identity.home / ".local",
        identity.home / ".local/share",
        identity.home / ".local/share/oi",
        identity.home / ".local/share/oi/opencode",
    ):
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent.chmod(0o700)
        if identity.uid != -1 and identity.gid != -1:
            try:
                shutil.chown(parent, identity.user, identity.user)
            except (OSError, PermissionError):
                pass
    candidates = (
        identity.home / ".local/bin/opencode",
        Path("/usr/local/bin/opencode"),
        Path("/usr/bin/opencode"),
    )
    configured = Path(cfg.opencode_binary).expanduser()
    if configured.is_absolute():
        executable = configured if configured.is_file() and os.access(configured, os.X_OK) else None
    else:
        executable = next(
            (candidate for candidate in candidates
             if candidate.is_file() and os.access(candidate, os.X_OK)),
            None,
        )
    if executable is None or not executable.is_file() \
            or not os.access(executable, os.X_OK):
        raise DaemonError(
            f"OpenCode is not executable for service user {identity.user!r}; "
            f"install it into {identity.home}/.local/bin or /usr/local/bin"
        )
    auth_file = paths.data_home / "opencode" / "auth.json"
    if not auth_file.is_file():
        raise DaemonError(
            f"OpenCode auth is missing for {identity.user!r}; run "
            f"`sudo -u {identity.user} -H oi init --model "
            f"{cfg.opencode_model}` before starting the service"
        )

def _copy_owned(source: Path, destination: Path, identity: DaemonIdentity) -> None:
    """Copy one explicitly allowed OI file with restrictive permissions."""
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(source, destination)
    destination.chmod(0o600)
    shutil.chown(destination, identity.user, identity.user)


def install(cfg: Config, *, user: str = DEFAULT_DAEMON_USER,
            service_name: str = DEFAULT_SERVICE_NAME,
            config_path: Path = DEFAULT_CONFIG_PATH,
            secrets_path: Path = DEFAULT_SECRETS_PATH,
            dry_run: bool = False,
            service_dir: Path | None = None,
            user_mode: bool = False) -> str:
    """Provision the daemon service: launchd LaunchAgent on macOS, systemd elsewhere.

    Returns:
        Rendered unit text; dry-run callers can print it directly.

    Raises:
        DaemonError: If root privileges (for system mode), config, or commands are missing.
    """
    if is_darwin():
        if not user_mode:
            raise DaemonError(
                "macOS supports only the per-user LaunchAgent (user mode); "
                "the dedicated daemon-user system mode is Linux-only"
            )
        return _install_launchd(cfg, service_name=service_name,
                                config_path=config_path, secrets_path=secrets_path,
                                dry_run=dry_run).decode("utf-8")

    if not user_mode and os.geteuid() != 0 and not dry_run:
        raise DaemonError("daemon install requires root; use sudo or --dry-run")

    oi_bin = resolve_oi_binary()

    if user_mode:
        actual_user = os.getenv("USER") or pwd.getpwuid(os.getuid()).pw_name
        identity = DaemonIdentity(actual_user, Path.home(), os.getuid(), os.getgid())
        daemon_config = config_path if config_path.exists() else identity.home / ".config/oi/config.toml"
        daemon_secrets = secrets_path if secrets_path.exists() else identity.home / ".config/oi/.env"
        unit = render_unit(cfg, identity, service_name=service_name,
                           config_path=daemon_config, secrets_path=daemon_secrets,
                           oi_binary=oi_bin, user_mode=True)
        if dry_run:
            return unit
        dest_dir = service_dir or (identity.home / ".config/systemd/user")
        dest_dir.mkdir(parents=True, exist_ok=True)
        unit_path = dest_dir / f"{service_name}.service"
        unit_path.write_text(unit, encoding="utf-8")
        for command in (["systemctl", "--user", "daemon-reload"],
                        ["systemctl", "--user", "enable", "--now", f"{service_name}.service"]):
            result = _run(command)
            if result.returncode != 0:
                raise DaemonError(f"systemctl --user command failed: {result.stderr or result.stdout}")
        return unit

    identity = _identity(user, Path(f"/home/{user}"))
    ensure_user(identity, dry_run=dry_run)
    ensure_opencode_for_user(cfg, identity, dry_run=dry_run)
    daemon_config = identity.home / ".config/oi/config.toml"
    daemon_secrets = identity.home / ".config/oi/.env"
    unit = render_unit(cfg, identity, service_name=service_name,
                       config_path=daemon_config, secrets_path=daemon_secrets,
                       oi_binary=oi_bin, user_mode=False)
    if dry_run:
        return unit
    if not config_path.exists():
        raise DaemonError(f"missing OI config {config_path}; run `oi init` first")
    _copy_owned(config_path, daemon_config, identity)
    _copy_owned(secrets_path, daemon_secrets, identity)
    dest_dir = service_dir or SERVICE_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    unit_path = dest_dir / f"{service_name}.service"
    unit_path.write_text(unit, encoding="utf-8")
    unit_path.chmod(0o644)
    for command in (["systemctl", "daemon-reload"],
                    ["systemctl", "enable", "--now", f"{service_name}.service"]):
        result = _run(command)
        if result.returncode != 0:
            raise DaemonError(f"systemctl command failed: {command[1]}")
    return unit


def stream_logs(service_name: str = DEFAULT_SERVICE_NAME, *, user_mode: bool = False) -> None:
    """Stream journalctl (or the LaunchAgent log on macOS) interactively."""
    if is_darwin():
        _launchd_stream_logs(service_name)
        return
    command = (
        ["journalctl", "--user", "-u", f"{service_name}.service", "-f"]
        if user_mode
        else ["journalctl", "-u", f"{service_name}.service", "-f"]
    )
    try:
        subprocess.run(command, check=False)
    except KeyboardInterrupt:
        pass


def lifecycle(action: str, service_name: str = DEFAULT_SERVICE_NAME,
              *, user_mode: bool = False) -> str:
    """Run one non-destructive systemd/journalctl lifecycle command."""
    if action not in {"status", "restart", "logs"}:
        raise DaemonError(f"unsupported daemon action: {action}")
    if is_darwin():
        if action == "logs":
            return _launchd_logs(service_name)
        return _launchd_lifecycle(action, service_name)
    command = (
        ["journalctl", "--user", "-u", f"{service_name}.service", "--no-pager"]
        if action == "logs" and user_mode
        else ["journalctl", "-u", f"{service_name}.service", "--no-pager"]
        if action == "logs"
        else (
            ["systemctl", "--user", action, f"{service_name}.service"]
            if user_mode
            else ["systemctl", action, f"{service_name}.service"]
        )
    )
    result = _run(command)
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        raise DaemonError(output or f"{command[0]} exited {result.returncode}")
    return output


def uninstall(service_name: str = DEFAULT_SERVICE_NAME,
              *, purge: bool = False, user: str = DEFAULT_DAEMON_USER,
              service_dir: Path | None = None,
              user_mode: bool = False) -> None:
    """Stop/remove a service and optionally remove its dedicated user."""
    if is_darwin():
        _launchd_uninstall(service_name)
        return
    if user_mode:
        _run(["systemctl", "--user", "disable", "--now", f"{service_name}.service"])
        unit_dir = service_dir or (Path.home() / ".config/systemd/user")
        unit_path = unit_dir / f"{service_name}.service"
        if unit_path.exists():
            unit_path.unlink()
        _run(["systemctl", "--user", "daemon-reload"])
        return

    if os.geteuid() != 0:
        raise DaemonError("daemon uninstall requires root")
    dest_dir = service_dir or SERVICE_DIR
    _run(["systemctl", "disable", "--now", f"{service_name}.service"])
    unit_path = dest_dir / f"{service_name}.service"
    if unit_path.exists():
        unit_path.unlink()
    _run(["systemctl", "daemon-reload"])
    if purge:
        result = _run(["userdel", "--remove", user])
        if result.returncode != 0:
            raise DaemonError(f"could not remove service user {user!r}")


def doctor(cfg: Config, *, user: str | None = None,
           warnings: list[str] | None = None) -> list[str]:
    """Run OpenCode, auth, model, repository, token, and state preflight checks.

    Args:
        cfg (Config): OpenCode and daemon configuration.
        user (str | None): Optional service user name to validate permissions for.
        warnings (list[str] | None): Optional collector for conditions that
            are neither passes nor failures — currently a configured model
            that could not be verified against OpenCode's (possibly empty)
            model list while its provider holds an active credential.

    Returns:
        list[str]: Human-readable failed checks. An empty list means no check
        failed; entries collected in ``warnings`` still need attention.
    """
    failures: list[str] = []
    warn = warnings if warnings is not None else []
    identity = None
    if user:
        try:
            identity = _identity(user)
        except DaemonError as exc:
            failures.append(str(exc))
    paths = resolve_opencode_paths(identity.home if identity else None)
    try:
        version = check_version(cfg.opencode_binary, paths)
        if not authenticated(paths):
            failures.append("no OpenCode provider is authenticated")
        status = model_status(version.binary, paths, cfg.opencode_model)
        if not status.available:
            failures.append(f"selected model unavailable: {cfg.opencode_model}")
        elif not status.verified:
            warn.append(
                f"model {cfg.opencode_model} not verified against OpenCode's "
                "model list; provider credential present"
            )
    except OpenCodeBootstrapError as exc:
        failures.append(str(exc))
    for target in cfg.watches:
        repo = Path(target.repo_path).expanduser()
        if not repo.is_dir():
            failures.append(f"repository unreadable: {repo}")
        elif os.geteuid() == 0 and identity and identity.uid > 0:
            res = _run(["su", "-s", "/bin/sh", identity.user, "-c", f"test -r '{repo}'"])
            if res.returncode != 0:
                failures.append(f"repository is not readable by {identity.user}: {repo}")
        elif not os.access(repo, os.R_OK):
            failures.append(f"repository unreadable: {repo}")
    state_value = cfg.db_path
    state = (identity.home / state_value[2:].lstrip("/")
             if identity and state_value.startswith("~/")
             else Path(state_value).expanduser())
    if not state.is_absolute():
        state = (identity.home if identity else Path.cwd()) / state
    if not state.parent.exists():
        failures.append(f"state directory does not exist: {state.parent}")
    elif os.geteuid() == 0 and identity and identity.uid > 0:
        res = _run(["su", "-s", "/bin/sh", identity.user, "-c", f"test -w '{state.parent}'"])
        if res.returncode != 0:
            failures.append(f"state directory is not writable by {identity.user}: {state.parent}")
    elif not os.access(state.parent, os.W_OK):
        failures.append(f"state directory is not writable: {state.parent}")
    if state.exists() and os.access(state, os.R_OK):
        try:
            store = Store(state)
            try:
                stats = store.get_queue_stats()
                if stats.get("stale_leases", 0) > 0:
                    failures.append(f"store has {stats['stale_leases']} stale lease(s)")
                if stats.get("dead_jobs", 0) > 0:
                    failures.append(f"store has {stats['dead_jobs']} dead job(s)")
                if stats.get("dead_batches", 0) > 0:
                    failures.append(f"store has {stats['dead_batches']} dead batch(es)")
            finally:
                store.close()
        except Exception as exc:
            failures.append(f"unable to inspect store queue health: {exc}")
    secret_file = (
        identity.home / ".config/oi/.env"
        if identity else Path.home() / ".config/oi/.env"
    )
    token_set = bool(os.environ.get(cfg.discord_token_env))
    if secret_file.exists():
        token_set = token_set or any(
            line.startswith(f"{cfg.discord_token_env}=")
            for line in secret_file.read_text(encoding="utf-8").splitlines()
        )
    if not token_set:
        failures.append(f"Discord token env var is not set: {cfg.discord_token_env}")
    if identity:
        if identity.uid == -1:
            failures.append(f"service user does not exist: {identity.user}")
        elif identity.uid == 0:
            failures.append("service user must not be root")
        elif state.parent.exists() and state.parent.stat().st_uid != identity.uid:
            failures.append(f"state directory is not owned by {identity.user}: {state.parent}")
    return failures

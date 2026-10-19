"""Native OpenCode v2 invocation, disposable isolation, and completion proof.

Three responsibilities, all proven against the installed native v2.0.5:

1. **Invocation contract.** v2 ``opencode run`` has no ``--dir`` flag; the
   working directory is the project Location and is set with the subprocess
   ``cwd``. ``--standalone`` runs a private server, which is what keeps an OI
   run from connecting to the user's shared background service. ``--file``
   attaches real image bytes, and repeated ``--file`` flags were verified to
   persist multiple attachments.

2. **Disposable isolation.** Runs that carry external evidence get a fresh,
   private native root (HOME, XDG config/data/state/cache, and TMPDIR) that is
   deleted afterwards. Only minimal already-isolated credential material is
   copied in, with restrictive modes, and no permission rule grants the model
   read access to those paths.

3. **Authoritative completion.** The v2 CLI stream does NOT emit a terminal
   ``step_finish`` for the final answer. ``exit 0`` plus a completed text part
   is therefore NOT sufficient proof of a completed answer. Completion is
   proven from the native database the run itself wrote: the assistant message
   the CLI reported must carry ``finish == "stop"`` with a completion
   timestamp, and the session's terminal idle record must report a successful
   outcome. The query is read-only, bounded to one session, and uses SQLite
   JSON extraction so no message content is ever read into OI memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .._tempowner import mark_owner

# Credential columns copied into disposable native storage. Sessions,
# projects, events, permissions, and every other table are never copied.
CREDENTIAL_COLUMNS = (
    "id", "integration_id", "label", "value", "connector_id",
    "method_id", "active", "time_created", "time_updated",
)

# A credential is copied only from an already-isolated OI native database, and
# the value never leaves this module: it is written straight to the disposable
# database and never logged, returned, or embedded in a config document.
_DISPOSABLE_DIR_MODE = 0o700
_DISPOSABLE_FILE_MODE = 0o600
NATIVE_DB_RELATIVE = Path("opencode") / "opencode.db"
SCHEMA_INIT_TIMEOUT_SECONDS = 120.0

# Completion classes (bounded enum-like tokens, safe for a Discord reply).
COMPLETION_OK = "completed"
COMPLETION_NO_DB = "native_state_unavailable"
COMPLETION_NO_SESSION = "native_session_unverifiable"
COMPLETION_NOT_STOPPED = "native_answer_not_completed"
COMPLETION_FAILED = "native_run_failed"


@dataclass(frozen=True)
class NativeCompletion:
    """Bounded proof that one native answer actually completed.

    ``message_id`` is the exact assistant message native state confirmed. The
    selector requires the stream's last text message to be THIS message, so a
    trailing unproven message can never become the deliverable answer.
    """

    completed: bool
    error_class: str | None = None
    message_id: str | None = None


def build_argv(
    model: str,
    agent: str,
    *,
    session_id: str | None = None,
    files: list[Path] | None = None,
) -> list[str]:
    """Build the documented native v2 ``run`` argument vector.

    The repository Location is set through the subprocess working directory,
    not a flag, because v2 removed ``--dir``.

    Args:
        model: Configured ``provider/model`` identifier.
        agent: Native agent id selected via ``--agent``.
        session_id: Optional session to resume. Never set for external runs.
        files: Optional image paths attached with repeated ``--file`` flags.

    Returns:
        A deterministic argument vector, without the executable itself.
    """
    argv = [
        "run",
        "--standalone",
        "--format", "json",
        "--model", model,
        "--agent", agent,
    ]
    for path in files or ():
        argv.extend(["--file", str(path)])
    if session_id:
        argv.extend(["--session", session_id])
    return argv


def native_db_path(data_home: Path) -> Path:
    """Return the native database path for an XDG data home."""
    return data_home / NATIVE_DB_RELATIVE


def environment_for(root: Path, data_home: Path) -> dict[str, str]:
    """Build an isolated native environment for one root.

    Only non-secret, non-OI variables are inherited. Personal auth, personal
    OpenCode config, project config, and every credential-bearing variable stay
    out, so a run can never read the operator's real OpenCode identity.
    """
    environment = {
        key: value for key, value in os.environ.items()
        if key in {
            "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "TZ",
            # Non-secret: the disposable HOME hides mise's trust store, so the
            # operator's explicit trust list must be passed through.
            "MISE_TRUSTED_CONFIG_PATHS",
        }
    }
    environment.update({
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "TMPDIR": str(root / "tmp"),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    })
    return environment


class DisposableNativeRoot:
    """A fresh native root that is deleted when the run ends.

    The native harness persists prompt text, attachment bytes (as Base64), and
    session state under its XDG data home and TMPDIR. An external-evidence run
    therefore must not reuse the ordinary conversation roots: it gets a private
    root, and the root is removed on success, failure, timeout, and
    cancellation.
    """

    def __init__(self, base: Path | None = None) -> None:
        """Create the private root tree with restrictive modes."""
        self._base = Path(base) if base is not None else Path(
            tempfile.mkdtemp(prefix="oi-native-")
        )
        if base is None:
            _chmod(self._base, _DISPOSABLE_DIR_MODE)
        for sub in ("home", "config", "data", "state", "cache", "tmp"):
            path = self._base / sub
            path.mkdir(parents=True, exist_ok=True)
            _chmod(path, _DISPOSABLE_DIR_MODE)
        mark_owner(self._base)
        self.home = self._base / "home"
        self.data_home = self._base / "data"
        self.closed = False

    @property
    def base(self) -> Path:
        """Return the private root directory."""
        return self._base

    def environment(self) -> dict[str, str]:
        """Return the isolated environment for this root."""
        return environment_for(self._base, self.data_home)

    def close(self) -> None:
        """Delete the whole private root. Never raises."""
        if self.closed:
            return
        self.closed = True
        shutil.rmtree(self._base, ignore_errors=True)

    def __enter__(self) -> DisposableNativeRoot:
        """Enter a managed scope so cleanup always runs."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Delete the private root when leaving the scope."""
        self.close()


def _chmod(path: Path, mode: int) -> None:
    """Restrict one path, ignoring platforms that do not support it."""
    try:
        path.chmod(mode)
    except OSError:
        pass


async def initialize_native_schema(binary: str, root: DisposableNativeRoot) -> Path:
    """Let the native binary create its own schema in the disposable root.

    The native harness owns its schema and migrations. A bogus model id is
    used so the call initializes storage and then fails at model routing,
    producing no usable session and no side effect.

    The call is asynchronous and runs in its own process group, so a hung or
    cancelled initialization cannot block the event loop and cannot orphan
    private native children. The inline agent config denies every action
    during bootstrap, so even this throwaway call has no tool authority.

    Returns:
        The disposable native database path.
    """
    environment = root.environment()
    environment["OPENCODE_CONFIG_CONTENT"] = config_content({
        "agents": {
            "oi_bootstrap": {
                "description": "Storage bootstrap with no tools",
                "mode": "primary",
                "steps": 1,
                "system": "Initialize storage.",
                "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
            }
        }
    })
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            binary, *build_argv("divyachakshu/__schema_init__", "oi_bootstrap"),
            env=environment,
            cwd=str(root.base / "tmp"),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=SCHEMA_INIT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await terminate_process(process)
    except asyncio.CancelledError:
        if process is not None:
            await terminate_process(process)
        raise
    except (OSError, ValueError):
        pass
    return native_db_path(root.data_home)


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a native process group, escalating to SIGKILL.

    Mirrors the audit runner's cancellation invariant so a timed-out or
    cancelled native call never leaves a private child process behind.
    """
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


def copy_isolated_credentials(source_db: Path, target_db: Path) -> int:
    """Copy ONLY credential rows from an already-isolated native database.

    No session, message, event, project, or permission row is copied, so no
    conversation history or evidence can leak into a disposable run. The
    credential values are written directly into the target database and are
    never returned, logged, or embedded in a config document.

    Args:
        source_db: OI-isolated native database holding the credentials.
        target_db: Freshly initialized disposable native database.

    Returns:
        The number of credential rows copied (0 when unavailable).
    """
    if not source_db.is_file() or not target_db.is_file():
        return 0
    try:
        source = sqlite3.connect(
            source_db.resolve().as_uri() + "?mode=ro", uri=True
        )
    except sqlite3.Error:
        return 0
    try:
        columns = ",".join(CREDENTIAL_COLUMNS)
        rows = source.execute(
            f"SELECT {columns} FROM credential"  # noqa: S608 - fixed column list
        ).fetchall()
    except sqlite3.Error:
        return 0
    finally:
        source.close()
    if not rows:
        return 0
    placeholders = ",".join("?" * len(CREDENTIAL_COLUMNS))
    try:
        target = sqlite3.connect(target_db)
    except sqlite3.Error:
        return 0
    try:
        target.executemany(
            f"INSERT OR REPLACE INTO credential ({columns}) VALUES ({placeholders})",  # noqa: S608
            rows,
        )
        target.commit()
    except sqlite3.Error:
        return 0
    finally:
        target.close()
    _chmod(target_db, _DISPOSABLE_FILE_MODE)
    return len(rows)


def copy_model_catalog(source_db: Path, target_db: Path) -> bool:
    """Copy the public model catalog the provider route needs.

    Disposable research storage otherwise cannot resolve a configured model
    (``provider.no-route``). Only the ``models-dev:catalog`` key is copied.
    Credential values and every other row stay out.
    """
    if not source_db.is_file() or not target_db.is_file():
        return False
    try:
        source = sqlite3.connect(source_db.resolve().as_uri() + "?mode=ro", uri=True)
        row = source.execute(
            "SELECT key, value, time_created, time_updated FROM kv WHERE key = ?",
            ("models-dev:catalog",),
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        try:
            source.close()
        except Exception:
            pass
    if row is None:
        return False
    try:
        target = sqlite3.connect(target_db)
        target.execute(
            "INSERT OR REPLACE INTO kv (key, value, time_created, time_updated) VALUES (?, ?, ?, ?)",
            row,
        )
        target.commit()
    except sqlite3.Error:
        return False
    finally:
        try:
            target.close()
        except Exception:
            pass
    return True


def verify_completion(
    db_path: Path,
    session_id: str | None,
    message_id: str | None,
    *,
    returncode: int | None,
    has_error_event: bool,
) -> NativeCompletion:
    """Prove, from native state, that the reported answer really completed.

    The v2 CLI emits no terminal ``step_finish`` for the final answer, so
    completion is established from the assistant record the run itself wrote.
    The lookup is restricted to the exact session and message the CLI
    reported, and only the ``finish``, ``time.completed``, and idle
    ``outcome`` fields are read, so no message content enters OI memory.

    Args:
        db_path: Native database the run wrote to.
        session_id: Session id reported by the CLI stream.
        message_id: Final assistant message id reported by the CLI stream.
        returncode: Native process exit code.
        has_error_event: Whether the stream carried an error event.

    Returns:
        A :class:`NativeCompletion` describing the bounded outcome.
    """
    if returncode != 0:
        return NativeCompletion(False, COMPLETION_FAILED)
    if has_error_event or not session_id or not message_id:
        return NativeCompletion(False, COMPLETION_NO_SESSION)
    if not db_path.is_file():
        return NativeCompletion(False, COMPLETION_NO_DB)
    try:
        connection = sqlite3.connect(
            db_path.resolve().as_uri() + "?mode=ro", uri=True
        )
    except sqlite3.Error:
        return NativeCompletion(False, COMPLETION_NO_DB)
    try:
        row = connection.execute(
            "SELECT json_extract(data, '$.finish'), "
            "       json_extract(data, '$.time.completed') "
            "FROM session_message WHERE id = ? AND type = 'assistant' "
            "  AND session_id = ?",
            (message_id, session_id),
        ).fetchone()
        if row is None:
            return NativeCompletion(False, COMPLETION_NO_SESSION)
        finish, completed = row
        if finish != "stop" or not completed:
            return NativeCompletion(False, COMPLETION_NOT_STOPPED)
        # The session's terminal idle record must report success, and must
        # come after this assistant message so a prior turn cannot vouch for
        # the current one.
        idle = connection.execute(
            "SELECT json_extract(data, '$.outcome') FROM session_message "
            "WHERE session_id = ? AND type = 'idle' AND seq > "
            "  (SELECT seq FROM session_message WHERE id = ?) "
            "ORDER BY seq DESC LIMIT 1",
            (session_id, message_id),
        ).fetchone()
    except sqlite3.Error:
        return NativeCompletion(False, COMPLETION_NO_DB)
    finally:
        connection.close()
    if idle is None:
        return NativeCompletion(False, COMPLETION_NO_SESSION)
    if idle[0] != "succeeded":
        return NativeCompletion(False, COMPLETION_FAILED)
    return NativeCompletion(True, None, message_id)


def config_content(document: dict) -> str:
    """Serialize inline native configuration deterministically."""
    return json.dumps(document, separators=(",", ":"))

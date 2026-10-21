"""Loopback-only aiohttp server behind `oi ui`.

Security contract (P1):
- binds 127.0.0.1 only; refuses any other host argument;
- one random 32-byte token is printed in the first URL and exchanged for an
  HttpOnly, SameSite=Strict cookie; every API and static request needs it;
- requests whose Host header is not `127.0.0.1:<port>` / `localhost:<port>`
  are rejected (DNS rebinding), and non-GET requests with a foreign Origin
  header are rejected;
- responses are JSON only, no CORS headers, access logging disabled so the
  token in the first URL never lands in a log.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import secrets as _secrets
import socket
import subprocess
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp import web

from ..config import Config, WatchTarget, load_config, save_config
from ..daemon import DEFAULT_SERVICE_NAME, _launchd_log_path, is_darwin, launchd_label
from ..daemon import install as install_daemon
from ..daemon import lifecycle as daemon_lifecycle
from ..opencode import bootstrap as oc
from ..opencode.paths import resolve_opencode_paths
from ..setup_save import SetupSaveError, apply_setup, replace_discord_token
from ..store import Store

COOKIE_NAME = "oi_ui_session"
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = ("app.js", "style.css")

DISCORD_API_BASE_DEFAULT = "https://discord.com/api/v10"
# Bot application flags (Discord Developer Portal). At least one of these must
# be enabled or the bot cannot read message content.
FLAG_MESSAGE_CONTENT = 1 << 15
FLAG_MESSAGE_CONTENT_LIMITED = 1 << 16
# Invite permissions OI needs today: View Channels, Send Messages,
# Read Message History, Send Messages in Threads.
INVITE_PERMISSIONS = (1 << 10) | (1 << 11) | (1 << 16) | (1 << 38)

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
DISCORD_TEXT_CHANNEL_TYPES = {0, 5, 15}


def discord_api_base() -> str:
    """Discord API base URL; tests and the smoke point this at a fake."""
    return os.environ.get("OI_DISCORD_API_BASE") or DISCORD_API_BASE_DEFAULT


def free_port() -> int:
    """Ask the kernel for one free loopback port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class UiServer:
    """Runtime state for one `oi ui` process."""

    def __init__(self, config_path: Path, secrets_path: Path, port: int) -> None:
        self.config_path = config_path
        self.secrets_path = secrets_path
        self.port = port
        self.token = _secrets.token_urlsafe(32)
        # Private `opencode serve` handle: {"proc": Popen | None, "base": str,
        # "integration": str | None}. None when no login session is open.
        self.oc_serve: dict[str, Any] | None = None

    @property
    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    @property
    def allowed_origins(self) -> set[str]:
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    def has_config(self) -> bool:
        return self.config_path.exists()

    def opencode_binary(self) -> str:
        """Configured OpenCode binary, or the default before a config exists."""
        if self.has_config():
            try:
                return load_config(self.config_path).opencode_binary or "opencode"
            except Exception:  # noqa: BLE001 - dashboard must survive a bad config
                pass
        return "opencode"


# --------------------------------------------------------------------------
# security middleware + static/index handlers
# --------------------------------------------------------------------------

def _json(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _err(message: str, status: int = 400) -> web.Response:
    return _json({"ok": False, "error": message}, status=status)


@web.middleware
async def security_middleware(request: web.Request, handler):
    """Enforce Host, Origin, and cookie rules before any handler runs."""
    state: UiServer = request.app["state"]
    if request.headers.get("Host", "") not in state.allowed_hosts:
        return _err("untrusted Host header; this UI only serves 127.0.0.1", status=403)
    if request.method != "GET":
        origin = request.headers.get("Origin")
        if origin and origin not in state.allowed_origins:
            return _err("cross-origin request refused", status=403)
    authed = request.cookies.get(COOKIE_NAME) == state.token
    if (request.path.startswith("/api/") or request.path.startswith("/static/")) and not authed:
        return _err("unauthorized: open the URL printed by `oi ui` once", status=401)
    return await handler(request)


async def index(request: web.Request) -> web.Response:
    """First URL carries ?t=<token> and exchanges it for the session cookie."""
    state: UiServer = request.app["state"]
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    token_param = request.query.get("t")
    if token_param is not None:
        if not _secrets.compare_digest(token_param, state.token):
            return web.Response(status=403, text="invalid setup token")
        response = web.Response(text=html, content_type="text/html")
        response.set_cookie(
            COOKIE_NAME, state.token, httponly=True, samesite="Strict",
            path="/", max_age=12 * 3600,
        )
        return response
    if request.cookies.get(COOKIE_NAME) == state.token:
        return web.Response(text=html, content_type="text/html")
    return web.Response(
        status=403, text="missing access token: open the URL printed by `oi ui`"
    )


async def static_file(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if name not in STATIC_FILES:
        raise web.HTTPNotFound
    return web.FileResponse(STATIC_DIR / name)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _require_oc_serve(request: web.Request) -> str | web.Response:
    state: UiServer = request.app["state"]
    if not state.oc_serve:
        return _err("no private OpenCode server running; start it first", status=409)
    return str(state.oc_serve["base"])


def _stop_oc_serve(state: UiServer) -> None:
    if state.oc_serve and state.oc_serve.get("proc"):
        proc: subprocess.Popen[bytes] = state.oc_serve["proc"]
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    state.oc_serve = None


def start_opencode_serve(binary: str, paths) -> tuple[subprocess.Popen | None, str]:
    """Start a private `opencode serve` on a free loopback port (seam for tests)."""
    port = free_port()
    proc = subprocess.Popen(
        [binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
        env=paths.environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc, f"http://127.0.0.1:{port}"


async def _wait_oc_ready(base: str, attempts: int = 24, delay: float = 0.25) -> bool:
    async with aiohttp.ClientSession() as session:
        for _ in range(attempts):
            try:
                async with session.get(
                    f"{base}/api/integration", timeout=aiohttp.ClientTimeout(total=2)
                ) as resp:
                    if resp.status == 200:
                        return True
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(delay)
    return False


async def _discord_get(token: str, path: str) -> tuple[int, Any]:
    """One authenticated GET against the (fake-able) Discord API."""
    headers = {"Authorization": f"Bot {token}"} if token else {}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{discord_api_base()}{path}", headers=headers, timeout=HTTP_TIMEOUT
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
                return resp.status, body
        except aiohttp.ClientError as exc:
            return 0, str(exc)


def scan_git_repos(root: Path, max_depth: int = 3) -> list[str]:
    """Git folders under root (depth <= max_depth, skip hidden/node_modules/.venv)."""
    skip = {"node_modules", ".venv", "__pycache__", ".sandbox", ".git"}
    found: list[str] = []

    def walk(directory: Path, depth: int) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith(".") or entry.name in skip:
                continue
            if (entry / ".git").exists():
                found.append(str(entry.resolve()))
                continue  # do not descend into a found clone
            if depth < max_depth:
                walk(entry, depth + 1)

    walk(root, 1)
    return found


# --------------------------------------------------------------------------
# config (de)serialization — every Config field, nothing secret
# --------------------------------------------------------------------------

_STR_FIELDS = (
    "discord_token_env", "db_path", "personality", "reply_delivery",
    "opencode_binary", "opencode_model", "environment_mode", "write_mode",
    "external_inputs", "image_inputs",
)
_INT_FIELDS = (
    "max_reply_chars", "opencode_steps", "opencode_timeout_seconds",
    "max_session_turns", "session_ttl_days", "max_context_messages",
    "max_context_age_days", "reminder_channel_id", "max_external_evidence_items",
    "max_attachments", "max_attachment_bytes",
)
_LIST_FIELDS = ("write_dirs",)


def config_to_dict(cfg: Config) -> dict[str, Any]:
    data: dict[str, Any] = {field: getattr(cfg, field) for field in _STR_FIELDS}
    data.update({field: getattr(cfg, field) for field in _INT_FIELDS})
    data.update({field: list(getattr(cfg, field)) for field in _LIST_FIELDS})
    data["watches"] = [watch_to_dict(wt) for wt in cfg.watches]
    return data


def watch_to_dict(wt) -> dict[str, Any]:
    return {
        "channel_id": wt.channel_id,
        "guild_id": wt.guild_id,
        "repos": list(wt.repo_paths),
        "ignored_channels": list(wt.ignored_channels),
        "allowed_channels": list(wt.allowed_channels),
        "bot_user_id": wt.bot_user_id,
        "post_hourly_cap": wt.post_hourly_cap,
    }


def _coerce_int(payload: dict[str, Any], key: str) -> int:
    try:
        return int(payload[key])
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer, got {payload[key]!r}") from None


def config_from_payload(payload: Any) -> Config:
    """Build a Config from JSON, rejecting unknown keys and wrong types.

    Value validation itself stays with save_config (single source of truth).
    """
    if not isinstance(payload, dict):
        raise ValueError("config payload must be a JSON object")
    known = set(_STR_FIELDS) | set(_INT_FIELDS) | set(_LIST_FIELDS) | {"watches"}
    unknown = set(payload) - known
    if unknown:
        raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
    cfg = Config()
    for key in _STR_FIELDS:
        if key in payload:
            if not isinstance(payload[key], str):
                raise ValueError(f"{key} must be a string, got {payload[key]!r}")
            setattr(cfg, key, payload[key])
    for key in _INT_FIELDS:
        if key in payload:
            setattr(cfg, key, _coerce_int(payload, key))
    for key in _LIST_FIELDS:
        if key in payload:
            value = payload[key]
            if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
                raise ValueError(f"{key} must be a list of strings")
            setattr(cfg, key, value)
    if "watches" in payload:
        watches = payload["watches"]
        if not isinstance(watches, list):
            raise ValueError("watches must be a list")
        cfg.watches = [watch_from_payload(item) for item in watches]
    return cfg


def watch_from_payload(payload: Any):

    if not isinstance(payload, dict):
        raise ValueError("each watch must be a JSON object")
    known = {
        "channel_id", "guild_id", "repos", "ignored_channels",
        "allowed_channels", "bot_user_id", "post_hourly_cap",
    }
    unknown = set(payload) - known
    if unknown:
        raise ValueError(f"unknown watch keys: {', '.join(sorted(unknown))}")
    ints: dict[str, int] = {}
    for key in ("channel_id", "guild_id", "bot_user_id"):
        ints[key] = _coerce_int(payload, key) if key in payload else 0
    ints["post_hourly_cap"] = _coerce_int(payload, "post_hourly_cap") if "post_hourly_cap" in payload else 3
    lists: dict[str, list[int]] = {}
    for key in ("ignored_channels", "allowed_channels"):
        raw = payload.get(key, [])
        if not isinstance(raw, list):
            raise ValueError(f"{key} must be a list of integers")
        lists[key] = [_coerce_int({key: v}, key) for v in raw]
    repos = payload.get("repos", [])
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        raise ValueError("watch repos must be a list of strings")
    return WatchTarget(repos=list(repos), **ints, **lists)


# --------------------------------------------------------------------------
# API handlers — P1 state
# --------------------------------------------------------------------------

async def api_state(request: web.Request) -> web.Response:
    state: UiServer = request.app["state"]
    mode = "dashboard" if state.has_config() else "wizard"
    return _json({"mode": mode, "config_path": str(state.config_path)})


# --------------------------------------------------------------------------
# API handlers — P2 wizard: OpenCode
# --------------------------------------------------------------------------

async def _integration_names(state: UiServer) -> dict[str, str]:
    """integration id -> human-readable name via the private serve ({} if none)."""
    base = (state.oc_serve or {}).get("base")
    if not base:
        return {}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{base}/api/integration", timeout=HTTP_TIMEOUT
            ) as resp:
                data = await resp.json(content_type=None)
        except aiohttp.ClientError:
            return {}
    entries = data.get("data", []) if isinstance(data, dict) else data
    return {
        str(entry["id"]): str(entry.get("name") or entry["id"])
        for entry in entries or []
        if isinstance(entry, dict) and entry.get("id")
    }


async def api_opencode_status(request: web.Request) -> web.Response:
    state: UiServer = request.app["state"]

    def probe() -> dict[str, Any]:
        info: dict[str, Any] = {
            "installed": False, "version": None, "authenticated": False,
            "integrations": [], "error": None,
        }
        paths = resolve_opencode_paths()
        try:
            version = oc.check_version(state.opencode_binary(), paths)
        except oc.OpenCodeBootstrapError as exc:
            info["error"] = str(exc)
            return info
        info["installed"] = True
        info["version"] = ".".join(map(str, version.version))
        info["authenticated"] = bool(oc.authenticated(paths))
        info["integrations"] = sorted(oc.credential_integrations(paths))
        return info

    info = await asyncio.to_thread(probe)
    if info["integrations"]:
        names = await _integration_names(state)
        info["integrations"] = [
            {"id": integration_id, "name": names.get(integration_id, integration_id)}
            for integration_id in info["integrations"]
        ]
    return _json(info)


async def api_opencode_install(request: web.Request) -> web.Response:
    state: UiServer = request.app["state"]

    def install() -> str:
        paths = resolve_opencode_paths()
        oc.install_opencode(paths)
        version = oc.check_version(state.opencode_binary(), paths)
        return ".".join(map(str, version.version))

    try:
        version = await asyncio.to_thread(install)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "version": version})


async def _ensure_oc_serve(state: UiServer) -> str | web.Response:
    """Start the private `opencode serve` once; return its base URL or an error."""
    if state.oc_serve:
        return str(state.oc_serve["base"])

    def start() -> tuple[subprocess.Popen | None, str]:
        paths = resolve_opencode_paths()
        version = oc.check_version(state.opencode_binary(), paths)
        return start_opencode_serve(version.binary, paths)

    try:
        proc, base = await asyncio.to_thread(start)
    except oc.OpenCodeBootstrapError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(str(exc))
    state.oc_serve = {"proc": proc, "base": base, "integration": None}
    if not await _wait_oc_ready(base):
        _stop_oc_serve(state)
        return _err("OpenCode did not start. Close this page, run `oi ui` again, and retry.", status=502)
    return base


async def api_oc_login_start(request: web.Request) -> web.Response:
    base = await _ensure_oc_serve(request.app["state"])
    if isinstance(base, web.Response):
        return base
    return _json({"ok": True, "base": base})


async def api_oc_login_stop(request: web.Request) -> web.Response:
    _stop_oc_serve(request.app["state"])
    return _json({"ok": True})


async def api_oc_integrations(request: web.Request) -> web.Response:
    base = _require_oc_serve(request)
    if isinstance(base, web.Response):
        return base
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{base}/api/integration", timeout=HTTP_TIMEOUT
            ) as resp:
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            return _err(f"private OpenCode server unreachable: {exc}", status=502)
    integrations = data.get("data", []) if isinstance(data, dict) else data
    return _json({"ok": True, "integrations": integrations})


async def api_oc_connect_key(request: web.Request) -> web.Response:
    base = _require_oc_serve(request)
    if isinstance(base, web.Response):
        return base
    body = await request.json()
    integration = str(body.get("integration") or "")
    key = str(body.get("key") or "")
    if not integration or not key:
        return _err("integration and key are required")
    request.app["state"].oc_serve["integration"] = integration
    url = f"{base}/api/integration/{quote(integration, safe='')}/connect/key"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                url, json={"key": key}, timeout=HTTP_TIMEOUT
            ) as resp:
                if resp.status in (200, 204):
                    return _json({"ok": True})
                return _json({"ok": False, "error": f"OpenCode rejected the key (HTTP {resp.status})"})
        except aiohttp.ClientError as exc:
            return _err(f"private OpenCode server unreachable: {exc}", status=502)


async def api_oc_connect_oauth(request: web.Request) -> web.Response:
    base = _require_oc_serve(request)
    if isinstance(base, web.Response):
        return base
    body = await request.json()
    integration = str(body.get("integration") or "")
    method_id = str(body.get("method_id") or body.get("methodID") or "oauth")
    if not integration:
        return _err("integration is required")
    request.app["state"].oc_serve["integration"] = integration
    url = f"{base}/api/integration/{quote(integration, safe='')}/connect/oauth"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                url, json={"methodID": method_id}, timeout=HTTP_TIMEOUT
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    return _json({"ok": False, "error": f"OAuth start failed (HTTP {resp.status})"})
        except aiohttp.ClientError as exc:
            return _err(f"private OpenCode server unreachable: {exc}", status=502)
    payload = (data or {}).get("data", {}) if isinstance(data, dict) else {}
    return _json({
        "ok": True,
        "attempt_id": payload.get("attemptID"),
        "url": payload.get("url"),
        "instructions": payload.get("instructions"),
    })


async def api_oc_oauth_status(request: web.Request) -> web.Response:
    base = _require_oc_serve(request)
    if isinstance(base, web.Response):
        return base
    integration = (request.app["state"].oc_serve or {}).get("integration")
    attempt_id = request.match_info["attempt_id"]
    if not integration:
        return _err("no OAuth attempt started")
    url = (f"{base}/api/integration/{quote(integration, safe='')}"
           f"/connect/oauth/{quote(attempt_id, safe='')}")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=HTTP_TIMEOUT) as resp:
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            return _err(f"private OpenCode server unreachable: {exc}", status=502)
    payload = (data or {}).get("data", {}) if isinstance(data, dict) else {}
    status = (payload or {}).get("status")
    done = status in ("ok", "complete", "success") or bool(
        isinstance(payload, dict) and payload.get("complete")
    )
    return _json({"ok": True, "status": status, "done": done})


async def api_oc_oauth_complete(request: web.Request) -> web.Response:
    base = _require_oc_serve(request)
    if isinstance(base, web.Response):
        return base
    integration = (request.app["state"].oc_serve or {}).get("integration")
    attempt_id = request.match_info["attempt_id"]
    if not integration:
        return _err("no OAuth attempt started")
    url = (f"{base}/api/integration/{quote(integration, safe='')}"
           f"/connect/oauth/{quote(attempt_id, safe='')}/complete")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json={}, timeout=HTTP_TIMEOUT) as resp:
                if resp.status in (200, 204):
                    return _json({"ok": True})
                return _json({"ok": False, "error": f"OAuth completion failed (HTTP {resp.status})"})
        except aiohttp.ClientError as exc:
            return _err(f"private OpenCode server unreachable: {exc}", status=502)


async def api_oc_models(request: web.Request) -> web.Response:
    """Model dropdown: providers (with model maps) plus exact model listings."""
    base = await _ensure_oc_serve(request.app["state"])
    if isinstance(base, web.Response):
        return base
    models: list[str] = []
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{base}/api/provider", timeout=HTTP_TIMEOUT) as resp:
                providers = (await resp.json(content_type=None) or {}).get("data", [])
            async with session.get(f"{base}/api/model", timeout=HTTP_TIMEOUT) as resp:
                listings = (await resp.json(content_type=None) or {}).get("data", [])
        except aiohttp.ClientError as exc:
            return _err(f"private OpenCode server unreachable: {exc}", status=502)
    for provider in providers or []:
        provider_id = provider.get("id") if isinstance(provider, dict) else None
        if not provider_id:
            continue
        for model_id in ((provider.get("models") or {}) if isinstance(provider, dict) else {}):
            models.append(f"{provider_id}/{model_id}")
    for model in listings or []:
        if not isinstance(model, dict):
            continue
        provider_id = model.get("providerID")
        model_id = model.get("modelID") or model.get("id")
        if provider_id and model_id:
            models.append(f"{provider_id}/{model_id}")
        elif model_id:
            models.append(str(model_id))
    if not models:
        models = [str(p.get("id")) for p in providers or [] if isinstance(p, dict) and p.get("id")]
    return _json({"ok": True, "models": sorted(set(models))})


# --------------------------------------------------------------------------
# API handlers — P2 wizard: Discord
# --------------------------------------------------------------------------

async def api_discord_validate(request: web.Request) -> web.Response:
    body = await request.json()
    token = str(body.get("token") or "")
    if not token:
        return _err("a bot token is required")
    status, me = await _discord_get(token, "/users/@me")
    if status != 200 or not isinstance(me, dict):
        detail = me if isinstance(me, str) and me else f"HTTP {status}"
        return _json({"ok": False, "error": f"Discord rejected the token ({detail})"})
    status, application = await _discord_get(token, "/applications/@me")
    flags = int(application.get("flags") or 0) if isinstance(application, dict) else 0
    intent_on = bool(flags & (FLAG_MESSAGE_CONTENT | FLAG_MESSAGE_CONTENT_LIMITED))
    application_id = me.get("id", "")
    invite_url = (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}&scope=bot&permissions={INVITE_PERMISSIONS}"
    )
    return _json({
        "ok": True,
        "application": {"id": application_id, "name": me.get("username") or me.get("name")},
        "message_content_enabled": intent_on,
        "invite_url": invite_url,
        "hint": None if intent_on else (
            "In the Discord Developer Portal open your application, choose Bot, "
            "switch on MESSAGE CONTENT INTENT, then Reset Token and paste the new token here."
        ),
    })


async def api_discord_guilds(request: web.Request) -> web.Response:
    body = await request.json()
    token = str(body.get("token") or "")
    status, guilds = await _discord_get(token, "/users/@me/guilds")
    if status != 200 or not isinstance(guilds, list):
        detail = guilds if isinstance(guilds, str) and guilds else f"HTTP {status}"
        return _json({"ok": False, "error": f"could not list servers ({detail})"})
    return _json({"ok": True, "guilds": [
        {"id": g.get("id"), "name": g.get("name")} for g in guilds if isinstance(g, dict)
    ]})


async def api_discord_channels(request: web.Request) -> web.Response:
    body = await request.json()
    token = str(body.get("token") or "")
    guild_id = str(body.get("guild_id") or "")
    if not guild_id.isdigit():
        return _err("guild_id must be numeric")
    status, channels = await _discord_get(token, f"/guilds/{guild_id}/channels")
    if status != 200 or not isinstance(channels, list):
        detail = channels if isinstance(channels, str) and channels else f"HTTP {status}"
        return _json({"ok": False, "error": f"could not list channels ({detail})"})
    return _json({"ok": True, "channels": [
        {"id": c.get("id"), "name": c.get("name"), "type": c.get("type")}
        for c in channels
        if isinstance(c, dict) and c.get("type") in DISCORD_TEXT_CHANNEL_TYPES
    ]})


# --------------------------------------------------------------------------
# API handlers — P2 wizard: repos, save, service
# --------------------------------------------------------------------------

async def api_repos_scan(request: web.Request) -> web.Response:
    body = await request.json()
    raw_root = str(body.get("root") or "").strip() or str(Path.home())
    root = Path(raw_root).expanduser()
    if not root.is_dir():
        return _err(f"{raw_root} is not a directory")
    repos = await asyncio.to_thread(scan_git_repos, root)
    return _json({"ok": True, "repos": repos})


async def api_repos_validate(request: web.Request) -> web.Response:
    body = await request.json()
    raw_path = str(body.get("path") or "").strip()
    if not raw_path:
        return _err("a repository path is required")
    resolved = Path(raw_path).expanduser().resolve()
    if not (resolved / ".git").exists():
        return _json({"ok": False, "error": f"{resolved} is not a git clone"})
    return _json({"ok": True, "path": str(resolved)})


async def api_save(request: web.Request) -> web.Response:
    state: UiServer = request.app["state"]
    body = await request.json()
    try:
        cfg = config_from_payload(body.get("config"))
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)})
    token = str(body.get("discord_token") or "")
    if not cfg.watches:
        return _json({"ok": False, "error": "no channels or guilds configured; add at least one watch"})
    try:
        await asyncio.to_thread(apply_setup, cfg, token, state.config_path, state.secrets_path)
    except SetupSaveError as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "config_path": str(state.config_path)})


async def api_service_install(request: web.Request) -> web.Response:
    state: UiServer = request.app["state"]

    def install() -> bool:
        cfg = load_config(state.config_path)
        user_mode = os.geteuid() != 0
        install_daemon(
            cfg, user_mode=user_mode,
            config_path=state.config_path, secrets_path=state.secrets_path,
        )
        return user_mode

    try:
        user_mode = await asyncio.to_thread(install)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "user_mode": user_mode})


# --------------------------------------------------------------------------
# API handlers — P3 dashboard
# --------------------------------------------------------------------------

def service_running() -> bool | None:
    """True/False when the service manager can answer, None when it cannot."""
    if is_darwin():
        command = ["launchctl", "print", f"gui/{os.getuid()}/{launchd_label(DEFAULT_SERVICE_NAME)}"]
    else:
        command = ["systemctl", "--user", "is-active", f"{DEFAULT_SERVICE_NAME}.service"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return None
    return result.returncode == 0


def read_daemon_logs(lines: int = 200) -> str:
    """Last N daemon log lines — same source as `oi daemon logs`, user mode."""
    if is_darwin():
        try:
            content = _launchd_log_path(DEFAULT_SERVICE_NAME).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return ""
        return "\n".join(content.splitlines()[-lines:])
    result = subprocess.run(
        ["journalctl", "--user", "-u", f"{DEFAULT_SERVICE_NAME}.service",
         "-n", str(lines), "--no-pager", "--quiet"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "journalctl failed")
    return result.stdout


async def api_status(request: web.Request) -> web.Response:
    state: UiServer = request.app["state"]

    def collect() -> dict[str, Any]:
        cfg = load_config(state.config_path)
        store = Store(Path(cfg.db_path).expanduser())
        try:
            paused = store.is_paused()
            last_activity = store.last_activity()
            installed_at = store.get_installed_at()
        finally:
            store.close()
        return {
            "ok": True,
            "paused": paused,
            "last_activity": last_activity,
            "installed_at": installed_at,
            "service_running": service_running(),
        }

    try:
        return _json(await asyncio.to_thread(collect))
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": str(exc)})


async def _set_paused(request: web.Request, paused: bool) -> web.Response:
    """Same store call `oi pause` / `oi resume` make (cli.py)."""
    state: UiServer = request.app["state"]

    def toggle() -> bool:
        cfg = load_config(state.config_path)
        store = Store(Path(cfg.db_path).expanduser())
        try:
            store.set_paused(paused)
            return store.is_paused()
        finally:
            store.close()

    try:
        value = await asyncio.to_thread(toggle)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "paused": value})


async def api_pause(request: web.Request) -> web.Response:
    return await _set_paused(request, True)


async def api_resume(request: web.Request) -> web.Response:
    return await _set_paused(request, False)


async def api_restart(request: web.Request) -> web.Response:
    def restart() -> None:
        daemon_lifecycle(
            "restart", service_name=DEFAULT_SERVICE_NAME,
            user_mode=os.geteuid() != 0,
        )

    try:
        await asyncio.to_thread(restart)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True})


async def api_logs(request: web.Request) -> web.Response:
    try:
        lines = int(request.query.get("lines", "200"))
    except ValueError:
        return _err("lines must be an integer")
    lines = max(1, min(lines, 2000))
    try:
        text = await asyncio.to_thread(read_daemon_logs, lines)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "lines": text.splitlines()[-lines:]})


async def api_settings_get(request: web.Request) -> web.Response:
    state: UiServer = request.app["state"]
    try:
        cfg = await asyncio.to_thread(load_config, state.config_path)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "config": config_to_dict(cfg)})


async def api_settings_save(request: web.Request) -> web.Response:
    state: UiServer = request.app["state"]
    body = await request.json()
    try:
        cfg = config_from_payload(body.get("config"))
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)})

    def save() -> None:
        save_config(cfg, state.config_path)  # validates; secrets file untouched

    try:
        await asyncio.to_thread(save)
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True, "restart_available": True})


async def api_secrets_token(request: web.Request) -> web.Response:
    state: UiServer = request.app["state"]
    body = await request.json()
    token = str(body.get("token") or "")
    if not token:
        return _err("a bot token is required")

    def apply() -> None:
        cfg = load_config(state.config_path)
        replace_discord_token(cfg, token, state.secrets_path)

    try:
        await asyncio.to_thread(apply)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": str(exc)})
    return _json({"ok": True})


# --------------------------------------------------------------------------
# app assembly + entry point
# --------------------------------------------------------------------------

def create_app(config_path: Path, secrets_path: Path, port: int) -> web.Application:
    app = web.Application(middlewares=[security_middleware])
    app["state"] = UiServer(config_path, secrets_path, port)
    app.add_routes([
        web.get("/", index),
        web.get("/static/{name}", static_file),
        web.get("/api/state", api_state),
        # wizard: OpenCode
        web.get("/api/opencode/status", api_opencode_status),
        web.post("/api/opencode/install", api_opencode_install),
        web.post("/api/opencode/login/start", api_oc_login_start),
        web.post("/api/opencode/login/stop", api_oc_login_stop),
        web.get("/api/opencode/login/integrations", api_oc_integrations),
        web.post("/api/opencode/login/key", api_oc_connect_key),
        web.post("/api/opencode/login/oauth", api_oc_connect_oauth),
        web.get("/api/opencode/login/oauth/{attempt_id}", api_oc_oauth_status),
        web.post("/api/opencode/login/oauth/{attempt_id}/complete", api_oc_oauth_complete),
        web.get("/api/opencode/models", api_oc_models),
        # wizard: Discord
        web.post("/api/discord/validate", api_discord_validate),
        web.post("/api/discord/guilds", api_discord_guilds),
        web.post("/api/discord/channels", api_discord_channels),
        # wizard: repos
        web.post("/api/repos/scan", api_repos_scan),
        web.post("/api/repos/validate", api_repos_validate),
        # save + service
        web.post("/api/save", api_save),
        web.post("/api/service/install", api_service_install),
        # dashboard
        web.get("/api/status", api_status),
        web.post("/api/pause", api_pause),
        web.post("/api/resume", api_resume),
        web.post("/api/restart", api_restart),
        web.get("/api/logs", api_logs),
        web.get("/api/settings", api_settings_get),
        web.post("/api/settings", api_settings_save),
        web.post("/api/secrets/token", api_secrets_token),
    ])
    return app


def _display_available() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _in_ssh_session() -> bool:
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def run_ui(
    config_path: Path,
    secrets_path: Path,
    *,
    port: int = 0,
    open_browser: bool = True,
    host: str = "127.0.0.1",
) -> int:
    """Run the UI server on 127.0.0.1 until Ctrl-C. Returns an exit code."""
    if host != "127.0.0.1":
        raise ValueError(
            f"refusing to bind {host!r}: the setup UI serves 127.0.0.1 only; "
            "use an SSH tunnel from other machines"
        )
    actual_port = port if port > 0 else free_port()
    app = create_app(config_path, secrets_path, actual_port)
    url = f"http://127.0.0.1:{actual_port}/?t={app['state'].token}"
    print(f"OI setup UI: {url}")
    if open_browser and _display_available() and not _in_ssh_session():
        webbrowser.open(url)
    else:
        print(
            "No local display detected. From your laptop run, then open the URL above:\n"
            f"  ssh -L {actual_port}:127.0.0.1:{actual_port} "
            f"{getpass.getuser()}@{socket.gethostname()}"
        )
    try:
        # access_log=None: the first URL carries the setup token — never log it.
        web.run_app(app, host="127.0.0.1", port=actual_port, print=None, access_log=None)
    except KeyboardInterrupt:
        pass
    finally:
        _stop_oc_serve(app["state"])
    return 0

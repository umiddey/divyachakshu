"""Fake Discord API and fake OpenCode serve for UI tests and the smoke run.

Endpoint shapes mirror tests/fixtures/opencode-openapi-2.0.5.json (copied from
`opencode serve`'s /openapi.json of the exact 2.0.5 release). No real network
calls, no real tokens: every value here is fixture material.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "opencode-openapi-2.0.5.json"

FLAG_MESSAGE_CONTENT = 1 << 15


def openapi_paths() -> set[str]:
    """Route table the fake serve implements (guards against fixture drift)."""
    spec = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return set(spec.get("paths", {}))


def build_fake_opencode_app() -> web.Application:
    """`opencode serve` stand-in: integration listing, key + OAuth connect."""
    app = web.Application()
    state: dict[str, Any] = {"oauth": {}}

    async def integration_list(request: web.Request) -> web.Response:
        return web.json_response({"location": {"directory": "/tmp/fake"}, "data": [{
            "id": "npm:@ai-sdk/openai-compatible",
            "name": "OpenAI-compatible",
            "metadata": {},
            "methods": [{"id": "api", "type": "api"}, {"id": "oauth", "type": "oauth"}],
            "connections": [],
        }]})

    async def connect_key(request: web.Request) -> web.Response:
        body = await request.json()
        if not body.get("key"):
            return web.json_response(
                {"_tag": "BadRequest", "message": "key required"}, status=400
            )
        state.setdefault("keys", []).append(body["key"])
        return web.Response(status=204)

    async def connect_oauth(request: web.Request) -> web.Response:
        await request.json()
        attempt = "attempt-1"
        state["oauth"][attempt] = {"status": "pending"}
        return web.json_response({"location": {"directory": "/tmp/fake"}, "data": {
            "attemptID": attempt,
            "url": "https://example.invalid/oauth/signin",
            "instructions": "Open the link and approve access.",
            "mode": "auto",
            "time": {"created": 0, "expires": 0},
        }})

    async def oauth_status(request: web.Request) -> web.Response:
        info = state["oauth"].get(request.match_info["attemptID"])
        if info is None:
            return web.json_response(
                {"_tag": "NotFound", "message": "unknown attempt"}, status=404
            )
        return web.json_response({
            "location": {"directory": "/tmp/fake"},
            "data": {"status": info["status"]},
        })

    async def oauth_complete(request: web.Request) -> web.Response:
        info = state["oauth"].get(request.match_info["attemptID"])
        if info is None:
            return web.json_response(
                {"_tag": "NotFound", "message": "unknown attempt"}, status=404
            )
        if info["status"] != "ok":
            return web.json_response(
                {"_tag": "BadRequest", "message": "sign-in not finished"}, status=400
            )
        return web.Response(status=204)

    async def provider_list(request: web.Request) -> web.Response:
        return web.json_response({"location": {"directory": "/tmp/fake"}, "data": [{
            "id": "stepfun",
            "canonical": "stepfun",
            "integrationID": "npm:@ai-sdk/openai-compatible",
            "name": "StepFun",
            "activation": "manual",
            "package": "@ai-sdk/openai-compatible",
            "models": {"gpt-test": {}},
            "settings": {},
            "headers": {},
            "body": {},
        }]})

    async def model_list(request: web.Request) -> web.Response:
        return web.json_response({"location": {"directory": "/tmp/fake"}, "data": [{
            "id": "gpt-test",
            "modelID": "gpt-test",
            "providerID": "stepfun",
            "name": "GPT Test",
            "capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
        }]})

    app.add_routes([
        web.get("/api/integration", integration_list),
        web.post("/api/integration/{integrationID}/connect/key", connect_key),
        web.post("/api/integration/{integrationID}/connect/oauth", connect_oauth),
        web.get("/api/integration/{integrationID}/connect/oauth/{attemptID}", oauth_status),
        web.post(
            "/api/integration/{integrationID}/connect/oauth/{attemptID}/complete",
            oauth_complete,
        ),
        web.get("/api/provider", provider_list),
        web.get("/api/model", model_list),
    ])
    app["oauth_state"] = state
    return app


def build_fake_discord_app(state: dict[str, Any] | None = None) -> web.Application:
    """Discord API v10 stand-in for the endpoints the wizard uses."""
    state = state if state is not None else {}
    state.setdefault("flags", FLAG_MESSAGE_CONTENT)
    state.setdefault("requests", [])
    state.setdefault("tokens", {"your-fake-bot-token"})
    app = web.Application()

    def note(request: web.Request) -> None:
        # Bounded metadata only: never record the Authorization value itself.
        state["requests"].append({
            "method": request.method,
            "path": request.path,
            "has_bot_auth": request.headers.get("Authorization", "").startswith("Bot "),
        })

    def authorized(request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        return any(header == f"Bot {token}" for token in state["tokens"])

    def unauthorized() -> web.Response:
        return web.json_response({"message": "401: Unauthorized"}, status=401)

    async def me(request: web.Request) -> web.Response:
        note(request)
        if not authorized(request):
            return unauthorized()
        return web.json_response({"id": "9001", "username": "fake-bot", "bot": True})

    async def application(request: web.Request) -> web.Response:
        note(request)
        if not authorized(request):
            return unauthorized()
        return web.json_response({"id": "9001", "name": "fake-bot", "flags": state["flags"]})

    async def guilds(request: web.Request) -> web.Response:
        note(request)
        if not authorized(request):
            return unauthorized()
        return web.json_response([
            {"id": "100", "name": "Guild One", "owner": True},
            {"id": "200", "name": "Guild Two", "owner": False},
        ])

    async def channels(request: web.Request) -> web.Response:
        note(request)
        if not authorized(request):
            return unauthorized()
        if request.match_info["guild_id"] not in ("100", "200"):
            return web.json_response({"message": "Unknown Guild"}, status=404)
        return web.json_response([
            {"id": "1001", "name": "general", "type": 0},
            {"id": "1002", "name": "help", "type": 0},
            {"id": "1003", "name": "announcements", "type": 5},
            {"id": "1004", "name": "voice-only", "type": 2},
        ])

    app.add_routes([
        web.get("/users/@me", me),
        web.get("/applications/@me", application),
        web.get("/users/@me/guilds", guilds),
        web.get("/guilds/{guild_id}/channels", channels),
    ])
    app["state"] = state
    return app


async def start_fake(app: web.Application) -> TestServer:
    server = TestServer(app)
    await server.start_server()
    return server


async def start_ui(config_path: Path, secrets_path: Path) -> tuple[TestClient, Any]:
    """Start the real UI app under a TestServer and exchange the token once."""
    from oi_agent.ui.server import create_app

    app = create_app(config_path, secrets_path, port=0)
    server = TestServer(app)
    await server.start_server()
    app["state"].port = server.port  # Host checks follow the real bound port
    client = TestClient(server)
    response = await client.get("/", params={"t": app["state"].token})
    assert response.status == 200, response.status
    return client, app

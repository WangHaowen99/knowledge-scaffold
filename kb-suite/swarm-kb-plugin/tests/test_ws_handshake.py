"""Plugin WebSockets follow Swarm's login: refused when Swarm's handshake check says no.

Only da-analyst still has its own route; kb-manager's calls go through Swarm's web channel
(register_method), which applies Swarm's login itself.

Runs inside the Swarm image or, without Swarm installed, against a stand-in for its plugin SDK
(the extensions import only that at module level). Synthetic data only.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

PLUGINS = Path(__file__).resolve().parents[1] / "plugin"


def _sdk_stand_in() -> dict[str, types.ModuleType]:
    if "jiuwenswarm.extensions.sdk" in sys.modules:
        return {}
    try:
        if importlib.util.find_spec("jiuwenswarm.extensions.sdk") is not None:
            return {}
    except ImportError:  # no jiuwenswarm at all
        pass
    sdk = types.ModuleType("jiuwenswarm.extensions.sdk")

    class Contribution:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    sdk.ApplicationPluginExtension = type("ApplicationPluginExtension", (), {})
    sdk.FrontendContribution = Contribution
    sdk.WebSocketRouteContribution = Contribution
    return {"jiuwenswarm": types.ModuleType("jiuwenswarm"),
            "jiuwenswarm.extensions": types.ModuleType("jiuwenswarm.extensions"), "jiuwenswarm.extensions.sdk": sdk}


def _load(plugin: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"{plugin.replace('-', '_')}_extension", PLUGINS / plugin / "extension.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, _sdk_stand_in()):
        spec.loader.exec_module(module)
    return module


class Channel:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[dict] = []

    async def authenticate_handshake(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Socket:
    """A handshake as Starlette hands it to a route: the ASGI scope, headers, accept/close."""

    def __init__(self, channel: Channel | None) -> None:
        app = types.SimpleNamespace(state=types.SimpleNamespace(web_channel=channel)) if channel else None
        self.scope = {"type": "websocket", "path": "/ws/plugins/x", "query_string": b"t=1", "client": ("192.0.2.10", 50000),
                      "app": app}
        self.headers = {"origin": "http://localhost:5199", "cookie": "session=synthetic"}
        self.accepted = False
        self.closed: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed = code

    async def receive_text(self) -> str:
        raise RuntimeError("disconnected")


def _serve(plugin: str, socket: Socket) -> None:
    module = _load(plugin)
    asyncio.run(module.DaAnalystPlugin()._serve(socket))


@pytest.mark.parametrize("plugin", ["da-analyst"])
@pytest.mark.parametrize("answer, allowed", [
    (None, True),  # Swarm login configured to let everyone in
    (types.SimpleNamespace(success=True, user_id="u1"), True),
    (types.SimpleNamespace(success=False), False),
    (RuntimeError("login service down"), False),
])
def test_plugin_socket_follows_swarm_login(plugin: str, answer: Any, allowed: bool) -> None:
    channel = Channel(answer)
    socket = Socket(channel)
    _serve(plugin, socket)
    assert socket.accepted is allowed and socket.closed == (None if allowed else 1008)
    assert channel.calls == [{"path": "/ws/plugins/x?t=1", "headers": socket.headers, "remote": "192.0.2.10:50000",
                              "channel": "web"}]


@pytest.mark.parametrize("plugin", ["da-analyst"])
def test_without_swarm_login_the_socket_opens(plugin: str) -> None:
    socket = Socket(None)
    _serve(plugin, socket)
    assert socket.accepted and socket.closed is None


def test_kb_manager_has_no_socket_of_its_own() -> None:
    module = _load("kb-manager")
    plugin = module.KbManagerPlugin()
    assert not hasattr(plugin, "_serve") and not hasattr(module, "handshake_denied")
    registered: list[tuple[str, bool]] = []
    channel = types.SimpleNamespace(register_method=lambda name, handler, local_only=False: registered.append((name, local_only)))
    plugin.bind_web_channel(channel, None)
    assert {name for name, _ in registered} == {f"plugin.kb_manager.{m}" for m in module.METHODS}
    assert all(local for _, local in registered)

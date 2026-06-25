#!/usr/bin/env python3
"""Shared helpers for the server input-surface tests.

These helpers intentionally do not modify server.py.  They load the server as a
module, create lightweight fake clients/websockets, and clean up game-loop tasks
that are started by normal game creation.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
SERVER_PATH = ROOT / "server.py"


def load_server(module_name: str = "snake_server_under_test"):
    spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {SERVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class DummyClient:
    """Minimal stand-in for ClientConn used by handle_message and Game methods."""

    def __init__(self, nickname: str = "") -> None:
        self.conn_id = f"test_conn_{id(self)}"
        self.nickname = nickname
        self.last_chat_time = 0
        self.game_id = None
        self.snake_id = None
        self.last_telemetry = None
        self.control_messages: list[dict[str, Any]] = []
        self.state_messages: list[dict[str, Any]] = []

    def send_control(self, payload: dict[str, Any]) -> None:
        # Assert the real server could serialize the payload to WebSocket JSON.
        self.control_messages.append(json.loads(json.dumps(payload, separators=(",", ":"))))

    def send_state(self, payload: dict[str, Any]) -> None:
        self.state_messages.append(json.loads(json.dumps(payload, separators=(",", ":"))))

    def last_control(self) -> dict[str, Any] | None:
        return self.control_messages[-1] if self.control_messages else None

    def control_types(self) -> list[str]:
        return [str(msg.get("type")) for msg in self.control_messages]

    def errors(self) -> list[dict[str, Any]]:
        return [msg for msg in self.control_messages if msg.get("type") == "error"]


class FakeWebSocket:
    """Async iterable WebSocket stand-in for websocket_handler tests."""

    def __init__(self, incoming: Iterable[str | bytes], *, yield_delay: float = 0.005) -> None:
        self.incoming = list(incoming)
        self.yield_delay = yield_delay
        self.sent_text: list[str] = []
        self.sent_json: list[dict[str, Any]] = []
        self.close_code = None
        self.close_reason = None

    def __aiter__(self):
        self._index = 0
        return self

    async def __anext__(self):
        if self._index > 0:
            await asyncio.sleep(self.yield_delay)
        if self._index >= len(self.incoming):
            await asyncio.sleep(self.yield_delay * 2)
            raise StopAsyncIteration
        item = self.incoming[self._index]
        self._index += 1
        return item

    async def send(self, message: str) -> None:
        self.sent_text.append(message)
        try:
            self.sent_json.append(json.loads(message))
        except json.JSONDecodeError:
            pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason


async def cleanup_server(server) -> None:
    """Cancel any Game.loop tasks and clear the global manager state."""

    games = list(getattr(server.manager, "games", {}).values())
    for game in games:
        task = getattr(game, "loop_task", None)
        if task is not None and not task.done():
            task.cancel()
    for game in games:
        task = getattr(game, "loop_task", None)
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
    server.manager.games.clear()
    if hasattr(server.manager, "all_time_high"):
        server.manager.all_time_high.clear()


async def reset_manager(server):
    await cleanup_server(server)
    server.manager = server.GameManager()
    return server.manager


async def create_via_message(server, nickname: str = "Noodle123") -> DummyClient:
    client = DummyClient()
    await server.handle_message(client, json.dumps({"type": "create_game", "nickname": nickname}))
    assert client.game_id is not None, client.control_messages
    assert any(msg.get("type") == "welcome" for msg in client.control_messages), client.control_messages
    return client


async def just_play_via_message(server, nickname: str = "Noodle123") -> DummyClient:
    client = DummyClient()
    await server.handle_message(client, json.dumps({"type": "just_play", "nickname": nickname}))
    assert client.game_id is not None, client.control_messages
    assert any(msg.get("type") == "welcome" for msg in client.control_messages), client.control_messages
    return client


def current_game(server, client: DummyClient):
    assert client.game_id is not None
    game = server.manager.games.get(client.game_id)
    assert game is not None
    return game


def current_snake(game, client: DummyClient):
    assert client.snake_id is not None
    snake = game.snakes.get(client.snake_id)
    assert snake is not None
    return snake


def set_snake_length(server, snake, length: int) -> None:
    """Replace the body with a safe straight body of requested length."""

    from collections import deque

    length = max(1, min(length, server.GRID_W - 8))
    snake.direction = "right"
    snake.pending_direction = "right"
    # Head at x=20, tail extends left. All cells are away from walls.
    snake.body = deque((20 - i, 20) for i in range(length))
    snake.grow = 0
    snake.alive = True
    snake.pending_sprint = False


def assert_error(client: DummyClient, code: str) -> None:
    assert any(msg.get("type") == "error" and msg.get("code") == code for msg in client.control_messages), client.control_messages


def run_import_subprocess(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    code = (
        "import importlib.util, sys; "
        f"spec=importlib.util.spec_from_file_location('s', {str(SERVER_PATH)!r}); "
        "m=importlib.util.module_from_spec(spec); sys.modules['s']=m; spec.loader.exec_module(m); "
        "print(f'{m.HOST}:{m.PORT}')"
    )
    env = os.environ.copy()
    env.update(extra_env)
    return subprocess.run([sys.executable, "-c", code], env=env, text=True, capture_output=True, timeout=10)

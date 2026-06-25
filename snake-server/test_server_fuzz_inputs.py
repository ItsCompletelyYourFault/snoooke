#!/usr/bin/env python3
"""Deterministic fuzz/regression tests for the server input surface.

This is deliberately lightweight and dependency-free so it can run anywhere the
server can run.  It sends random JSON envelopes, random message fields, random
transport frames, and random environment values.  The primary invariant is:
malformed/fuzzy input must not crash the server or create unbounded stored data.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import string
from typing import Any

from server_input_test_utils import (
    DummyClient,
    FakeWebSocket,
    cleanup_server,
    current_game,
    current_snake,
    just_play_via_message,
    load_server,
    reset_manager,
    set_snake_length,
)

server = load_server("snake_server_fuzz_inputs")
RNG = random.Random(0x51A4E)

MESSAGE_TYPES = [
    "just_play",
    "create_game",
    "join_game",
    "leave_game",
    "all_time_high",
    "input",
    "sprint",
    "telemetry",
    "chat",
    "unknown_type",
    "DROP TABLE snakes;",
    "",
    None,
]

FUZZ_STRINGS = [
    "",
    "abcd",
    "Valid123",
    "Noodle123",
    "ABCDE",
    "abcde",
    "A1B2C",
    "../etc/passwd",
    "<script>alert(1)</script>",
    "\ud800",
    "😀" * 20,
    "x" * 300,
    "\n\r\t spaced noodles \n",
]


def random_jsonable(depth: int = 0) -> Any:
    if depth > 3:
        return RNG.choice([None, True, False, RNG.randint(-10**6, 10**6), RNG.choice(FUZZ_STRINGS)])
    choice = RNG.randrange(9)
    if choice == 0:
        return None
    if choice == 1:
        return RNG.choice([True, False])
    if choice == 2:
        return RNG.randint(-10**9, 10**9)
    if choice == 3:
        return RNG.random() * RNG.choice([-1, 1]) * 10**6
    if choice == 4:
        return RNG.choice(FUZZ_STRINGS)
    if choice == 5:
        return "".join(RNG.choice(string.printable) for _ in range(RNG.randrange(0, 120)))
    if choice == 6:
        return [random_jsonable(depth + 1) for _ in range(RNG.randrange(0, 20))]
    if choice == 7:
        return {str(random_jsonable(depth + 1))[:20]: random_jsonable(depth + 1) for _ in range(RNG.randrange(0, 10))}
    return [[RNG.randint(-100, 200), RNG.randint(-100, 200)] for _ in range(RNG.randrange(0, 250))]


def random_payload(forced_type: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if forced_type is not False:
        payload["type"] = forced_type if forced_type is not None else RNG.choice(MESSAGE_TYPES)
    for key in ["nickname", "gameId", "dir", "seq", "clientTime", "length", "segments", "text", "unexpected"]:
        if RNG.random() < 0.78:
            payload[key] = random_jsonable()
    # Sometimes provide valid-looking values to make sure fuzz covers success paths too.
    if RNG.random() < 0.18:
        payload["nickname"] = RNG.choice(["Valid123", "Noodle123", "Snake999"])
    if RNG.random() < 0.18:
        payload["gameId"] = RNG.choice(["ABCDE", "A1B2C", "ZZZZZ"])
    if RNG.random() < 0.22:
        payload["dir"] = RNG.choice(["up", "down", "left", "right"])
    if RNG.random() < 0.22:
        payload["seq"] = RNG.randint(-10, 2_147_483_700)
    return payload


def raw_fuzz_message() -> str:
    choice = RNG.randrange(7)
    if choice == 0:
        return "{not json"
    if choice == 1:
        return RNG.choice(FUZZ_STRINGS)
    if choice == 2:
        return json.dumps(random_jsonable(), ensure_ascii=False)
    return json.dumps(random_payload(), ensure_ascii=False, separators=(",", ":"))


def env_safe(value: Any, max_len: int = 40) -> str:
    text = str(value).encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
    text = text.replace("/", "_").replace("\x00", "_")
    return text[:max_len] or "empty"


def assert_client_invariants(client: DummyClient) -> None:
    # Every queued response must stay JSON-serializable.
    json.dumps(client.control_messages)
    json.dumps(client.state_messages)
    if client.last_telemetry is not None:
        assert len(client.last_telemetry.get("segments", [])) <= server.MAX_ACTIVE_SNAKES * 8
        assert 0 <= int(client.last_telemetry.get("length", 0)) <= server.GRID_W * server.GRID_H


def assert_server_invariants() -> None:
    if hasattr(server.manager, "all_time_high"):
        assert len(server.manager.all_time_high) <= getattr(server, "ALL_TIME_HIGH_LIMIT", 10)
    for game in server.manager.games.values():
        assert len(game.players) <= server.MAX_HUMAN_PLAYERS
        assert len([s for s in game.snakes.values() if s.alive]) <= server.MAX_ACTIVE_SNAKES
        assert len(game.chat_history) <= server.CHAT_HISTORY_LIMIT


async def test_fuzz_environment_inputs() -> None:
    old = {name: os.environ.get(name) for name in ["SNAKE_SSL", "SNAKE_SSL_CERT", "SNAKE_SSL_KEY", "SNAKE_DEBUG", "PYCHARM_HOSTED"]}
    try:
        for _ in range(80):
            os.environ["SNAKE_SSL"] = env_safe(random_jsonable(), 80)
            os.environ["SNAKE_SSL_CERT"] = "/tmp/" + env_safe(random_jsonable(), 40)
            os.environ["SNAKE_SSL_KEY"] = "/tmp/" + env_safe(random_jsonable(), 40)
            os.environ["SNAKE_DEBUG"] = env_safe(random_jsonable(), 20)
            try:
                result = server.build_ssl_context()
                assert isinstance(result, tuple) and len(result) == 2
            except FileNotFoundError:
                # Expected when the fuzzer hits a truthy SNAKE_SSL with missing files.
                pass
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def test_fuzz_raw_handle_message_envelopes() -> None:
    await reset_manager(server)
    client = DummyClient()
    for _ in range(180):
        await server.handle_message(client, raw_fuzz_message())
        assert_client_invariants(client)
        assert_server_invariants()
    await cleanup_server(server)


async def test_fuzz_public_message_types() -> None:
    await reset_manager(server)
    for msg_type in ["just_play", "create_game", "join_game", "leave_game", "all_time_high"]:
        for _ in range(45):
            client = DummyClient()
            payload = random_payload(msg_type)
            await server.handle_message(client, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            assert_client_invariants(client)
            assert_server_invariants()
    await cleanup_server(server)


async def test_fuzz_in_game_message_types() -> None:
    await reset_manager(server)
    client = await just_play_via_message(server, "Fuzzer123")
    game = current_game(server, client)
    game.phase = "running"
    snake = current_snake(game, client)
    set_snake_length(server, snake, 8)

    for msg_type in ["input", "sprint", "telemetry", "chat"]:
        for _ in range(90):
            # Keep the game alive and the client inside the game while field fuzzing.
            client.game_id = game.game_id
            client.snake_id = snake.snake_id
            snake.alive = True
            game.phase = "running"
            if msg_type == "chat":
                client.last_chat_time = 0
            payload = random_payload(msg_type)
            await server.handle_message(client, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            assert_client_invariants(client)
            assert_server_invariants()

    # Fuzz leave_game separately because it intentionally removes the client.
    for _ in range(20):
        client.game_id = game.game_id
        client.snake_id = snake.snake_id if snake.snake_id in game.snakes else None
        await server.handle_message(client, json.dumps(random_payload("leave_game"), ensure_ascii=False, separators=(",", ":")))
        assert client.game_id is None
        assert_client_invariants(client)
        assert_server_invariants()

    await cleanup_server(server)


async def test_fuzz_websocket_transport_frames() -> None:
    await reset_manager(server)
    frames: list[str | bytes] = []
    for _ in range(80):
        raw = raw_fuzz_message()
        if RNG.random() < 0.45:
            frames.append(raw.encode("utf-8", errors="surrogatepass"))
        else:
            frames.append(raw)
    frames.extend([b"x" * (64 * 1024 + 1), "x" * (128 * 1024 + 1), b"\xff\xfe\x00"])
    ws = FakeWebSocket(frames, yield_delay=0.001)
    await server.websocket_handler(ws)
    # Responses may be sparse because many fuzzy inputs are silently ignored, but all emitted
    # responses must be JSON objects and known guard errors should appear for oversized frames.
    for item in ws.sent_json:
        assert isinstance(item, dict)
    codes = [msg.get("code") for msg in ws.sent_json if msg.get("type") == "error"]
    assert "TOO_LARGE" in codes
    assert_server_invariants()
    await cleanup_server(server)


async def main() -> None:
    tests = [
        test_fuzz_environment_inputs,
        test_fuzz_raw_handle_message_envelopes,
        test_fuzz_public_message_types,
        test_fuzz_in_game_message_types,
        test_fuzz_websocket_transport_frames,
    ]
    failures = []
    for test in tests:
        try:
            await test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - fuzz runner reports all crashed locations
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        finally:
            await cleanup_server(server)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

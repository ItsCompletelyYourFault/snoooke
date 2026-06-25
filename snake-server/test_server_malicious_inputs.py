#!/usr/bin/env python3
"""Negative/regression tests for malicious or malformed server input.

These tests intentionally preserve current server behavior.  They do not claim
all behavior is secure; they only lock in that malformed input is rejected,
ignored, bounded, or handled without crashing.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from server_input_test_utils import (
    DummyClient,
    FakeWebSocket,
    assert_error,
    cleanup_server,
    current_game,
    current_snake,
    just_play_via_message,
    load_server,
    reset_manager,
    run_import_subprocess,
    set_snake_length,
)

server = load_server("snake_server_malicious_inputs")


def json_msg(payload) -> str:
    return json.dumps(payload, separators=(",", ":"))


async def test_malicious_environment_inputs() -> None:
    bad_port = run_import_subprocess({"SNAKE_PORT": "not-a-port"})
    assert bad_port.returncode != 0, "import should fail fast for a non-integer SNAKE_PORT"

    old = {name: os.environ.get(name) for name in ["SNAKE_SSL", "SNAKE_SSL_CERT", "SNAKE_SSL_KEY", "SNAKE_DEBUG"]}
    try:
        os.environ["SNAKE_SSL"] = "1"
        os.environ["SNAKE_SSL_CERT"] = "/tmp/definitely_missing_snake_cert.pem"
        os.environ["SNAKE_SSL_KEY"] = "/tmp/definitely_missing_snake_key.pem"
        os.environ["SNAKE_DEBUG"] = "0"
        try:
            server.build_ssl_context()
            raise AssertionError("forced missing SSL files should raise FileNotFoundError")
        except FileNotFoundError:
            pass

        os.environ["SNAKE_SSL"] = "surprising-value"
        ctx, reason = server.build_ssl_context()
        assert ctx is None
        assert "not found" in reason
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def test_transport_malicious_frames() -> None:
    await reset_manager(server)
    ws = FakeWebSocket([
        b"x" * (64 * 1024 + 1),
        "x" * (128 * 1024 + 1),
        b"\xff\xfe\xff",
    ])
    await server.websocket_handler(ws)
    codes = [msg.get("code") for msg in ws.sent_json if msg.get("type") == "error"]
    assert codes.count("TOO_LARGE") == 2, ws.sent_json
    assert "BAD_JSON" in codes, ws.sent_json
    await cleanup_server(server)


async def test_bad_json_payload_and_unknown_types() -> None:
    await reset_manager(server)
    client = DummyClient()

    await server.handle_message(client, "{{not-json")
    assert_error(client, "BAD_JSON")

    await server.handle_message(client, json_msg(["array", "payload"]))
    assert_error(client, "BAD_PAYLOAD")

    await server.handle_message(client, json_msg({"type": None}))
    assert_error(client, "NOT_IN_GAME")

    # Unknown type becomes UNKNOWN_TYPE once the client is actually in a game.
    joined = await just_play_via_message(server, "BadGuy999")
    await server.handle_message(joined, json_msg({"type": "drop_database"}))
    assert_error(joined, "UNKNOWN_TYPE")
    await cleanup_server(server)


async def test_join_create_malicious_fields() -> None:
    await reset_manager(server)

    bad_nicknames = ["", "abcd", "a" * 16, "<script>", "ümlaut", {"x": 1}, None, "semi;colon"]
    for msg_type in ["just_play", "create_game", "join_game"]:
        for nickname in bad_nicknames:
            client = DummyClient()
            payload = {"type": msg_type, "nickname": nickname, "gameId": "ABCDE"}
            await server.handle_message(client, json_msg(payload))
            assert_error(client, "NICKNAME_INVALID")

    valid = DummyClient()
    for bad_game_id in ["", "ABCD", "ABCDEF", "AB!12", "../..", {"x": 1}, None]:
        valid.control_messages.clear()
        await server.handle_message(valid, json_msg({"type": "join_game", "nickname": "Valid123", "gameId": bad_game_id}))
        assert_error(valid, "GAME_ID_INVALID")

    valid.control_messages.clear()
    await server.handle_message(valid, json_msg({"type": "join_game", "nickname": "Valid123", "gameId": "ZZZZZ"}))
    assert_error(valid, "GAME_NOT_FOUND")

    already = await just_play_via_message(server, "Joined123")
    await server.handle_message(already, json_msg({"type": "create_game", "nickname": "Again123"}))
    assert_error(already, "ALREADY_JOINED")
    await cleanup_server(server)


async def test_in_game_malicious_direction_sprint_and_telemetry_fields() -> None:
    await reset_manager(server)
    client = await just_play_via_message(server, "Fields123")
    game = current_game(server, client)
    game.phase = "running"
    snake = current_snake(game, client)

    original_direction = snake.direction
    original_pending = snake.pending_direction
    await server.handle_message(client, json_msg({"type": "input", "dir": "northwest", "seq": 99}))
    assert snake.direction == original_direction
    assert snake.pending_direction == original_pending
    assert snake.last_input_seq == 0

    reverse = server.OPPOSITE[snake.direction]
    await server.handle_message(client, json_msg({"type": "input", "dir": reverse, "seq": 100}))
    assert snake.pending_direction == original_pending, "reverse input must not be accepted"
    assert snake.last_input_seq == 100, "current behavior records the accepted sequence before reverse rejection"

    non_reverse = next(d for d in server.DIRS if d != snake.direction and server.OPPOSITE[d] != snake.direction)
    await server.handle_message(client, json_msg({"type": "input", "dir": non_reverse, "seq": 50}))
    assert snake.pending_direction == original_pending, "stale lower seq should be ignored"

    set_snake_length(server, snake, 5)
    await server.handle_message(client, json_msg({"type": "sprint", "seq": 1, "dir": "right"}))
    assert snake.pending_sprint is False, "length <= 5 cannot sprint"

    set_snake_length(server, snake, 7)
    game.phase = "warmup"
    await server.handle_message(client, json_msg({"type": "sprint", "seq": 2, "dir": "right"}))
    assert snake.pending_sprint is False, "warmup phase cannot sprint"
    game.phase = "running"

    big_segments = [[i, i] for i in range(1000)]
    await server.handle_message(client, json_msg({
        "type": "telemetry",
        "seq": {"bad": "seq"},
        "dir": "sideways",
        "length": 10 ** 9,
        "segments": big_segments,
    }))
    assert client.last_telemetry["seq"] == 0
    assert client.last_telemetry["dir"] is None
    assert client.last_telemetry["length"] == server.GRID_W * server.GRID_H
    assert len(client.last_telemetry["segments"]) == server.MAX_ACTIVE_SNAKES * 8

    await server.handle_message(client, json_msg({"type": "telemetry", "segments": {"not": "a list"}}))
    assert client.last_telemetry["segments"] == []

    await cleanup_server(server)


async def test_chat_malicious_fields() -> None:
    await reset_manager(server)
    client = await just_play_via_message(server, "ChatGuy1")
    game = current_game(server, client)

    await server.handle_message(client, json_msg({"type": "chat", "text": ""}))
    assert_error(client, "CHAT_INVALID")

    before = len(game.chat_history)
    await server.handle_message(client, json_msg({"type": "chat", "text": {"html": "<img onerror=alert(1)>"}}))
    assert_error(client, "CHAT_INVALID")
    assert len(game.chat_history) == before

    client.last_chat_time = 0
    very_long = "<script>alert(1)</script>" + "x" * 1000
    await server.handle_message(client, json_msg({"type": "chat", "text": very_long}))
    last_chat = game.chat_history[-1]
    assert last_chat["kind"] == "player"
    assert len(last_chat["text"]) == server.CHAT_MAX_LENGTH
    assert last_chat["text"].startswith("<script>")

    # Flooded chat is silently ignored by current server behavior.
    history_len = len(game.chat_history)
    await server.handle_message(client, json_msg({"type": "chat", "text": "too soon"}))
    assert len(game.chat_history) == history_len

    await cleanup_server(server)


async def test_game_gone_and_leave_without_game() -> None:
    await reset_manager(server)

    client = DummyClient()
    await server.handle_message(client, json_msg({"type": "leave_game"}))
    assert client.last_control()["type"] == "left_game"

    ghost = DummyClient()
    ghost.game_id = "ABCDE"
    await server.handle_message(ghost, json_msg({"type": "input", "dir": "up", "seq": 1}))
    assert_error(ghost, "GAME_GONE")
    await cleanup_server(server)


async def test_unhashable_message_type_does_not_crash() -> None:
    """Security regression target: currently fails until server validates type as a string."""
    await reset_manager(server)
    client = DummyClient()
    try:
        await server.handle_message(client, json_msg({"type": {"not": "hashable"}}))
    except Exception as exc:  # noqa: BLE001 - this test reports any server crash
        raise AssertionError("handle_message crashed when payload.type was an object") from exc


async def test_unhashable_input_dir_does_not_crash() -> None:
    """Security regression target: currently fails until input.dir is type-checked."""
    await reset_manager(server)
    joined = await just_play_via_message(server, "HashGuy1")
    game = current_game(server, joined)
    game.phase = "running"
    try:
        await server.handle_message(joined, json_msg({"type": "input", "dir": {"not": "hashable"}, "seq": 1}))
    except Exception as exc:  # noqa: BLE001
        raise AssertionError("receive_input crashed when dir was an object") from exc


async def test_unhashable_telemetry_dir_does_not_crash() -> None:
    """Security regression target: currently fails until telemetry.dir is type-checked."""
    await reset_manager(server)
    joined = await just_play_via_message(server, "HashGuy2")
    game = current_game(server, joined)
    game.phase = "running"
    try:
        await server.handle_message(joined, json_msg({"type": "telemetry", "dir": ["not", "hashable"], "seq": 1, "segments": []}))
    except Exception as exc:  # noqa: BLE001
        raise AssertionError("receive_telemetry crashed when dir was a list") from exc


async def main() -> None:
    tests = [
        test_malicious_environment_inputs,
        test_transport_malicious_frames,
        test_bad_json_payload_and_unknown_types,
        test_join_create_malicious_fields,
        test_in_game_malicious_direction_sprint_and_telemetry_fields,
        test_chat_malicious_fields,
        test_game_gone_and_leave_without_game,
        test_unhashable_message_type_does_not_crash,
        test_unhashable_input_dir_does_not_crash,
        test_unhashable_telemetry_dir_does_not_crash,
    ]
    failures = []
    for test in tests:
        try:
            await test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - test runner should report all failures
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")
        finally:
            await cleanup_server(server)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Regression tests for every expected server input location.

Input surface covered:
- process environment at import/build_ssl_context: SNAKE_HOST, SNAKE_PORT,
  SNAKE_SSL, SNAKE_DEBUG, SNAKE_SSL_CERT, SNAKE_SSL_KEY, PYCHARM_HOSTED
- websocket_handler transport: text frames, binary frames, frame length guards
- handle_message JSON envelope: raw JSON, object payload, payload.type
- public message types: just_play, create_game, join_game, leave_game,
  all_time_high
- in-game message types and fields: input.dir/input.seq,
  sprint.seq/sprint.dir telemetry, telemetry.seq/dir/length/segments,
  chat.text
"""

from __future__ import annotations

import asyncio
import json
import os

from server_input_test_utils import (
    DummyClient,
    FakeWebSocket,
    assert_error,
    cleanup_server,
    create_via_message,
    current_game,
    current_snake,
    just_play_via_message,
    load_server,
    reset_manager,
    run_import_subprocess,
    set_snake_length,
)

server = load_server("snake_server_expected_inputs")


def json_msg(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


async def test_environment_expected_inputs() -> None:
    proc = run_import_subprocess({"SNAKE_HOST": "127.0.0.1", "SNAKE_PORT": "9876"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "127.0.0.1:9876"

    old = {name: os.environ.get(name) for name in ["SNAKE_SSL", "SNAKE_DEBUG", "PYCHARM_HOSTED"]}
    try:
        os.environ["SNAKE_SSL"] = "0"
        os.environ.pop("SNAKE_DEBUG", None)
        os.environ.pop("PYCHARM_HOSTED", None)
        ctx, reason = server.build_ssl_context()
        assert ctx is None
        assert "disabled" in reason

        os.environ.pop("SNAKE_SSL", None)
        os.environ["SNAKE_DEBUG"] = "1"
        ctx, reason = server.build_ssl_context()
        assert ctx is None
        assert "debug mode" in reason
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def test_websocket_transport_expected_inputs() -> None:
    await reset_manager(server)
    ws = FakeWebSocket([
        json_msg({"type": "all_time_high"}),
        json_msg({"type": "all_time_high"}).encode("utf-8"),
    ])
    await server.websocket_handler(ws)
    types = [msg.get("type") for msg in ws.sent_json]
    assert types.count("all_time_high") == 2, ws.sent_json
    await cleanup_server(server)


async def test_public_game_lifecycle_messages() -> None:
    await reset_manager(server)

    create_client = await create_via_message(server, "Noodle123")
    create_game = current_game(server, create_client)
    assert create_game.phase == "warmup"
    assert len(create_client.game_id) == server.GAME_ID_LENGTH
    assert any(msg.get("type") == "welcome" and msg.get("mode") == "create_game" for msg in create_client.control_messages)

    join_client = DummyClient()
    await server.handle_message(join_client, json_msg({
        "type": "join_game",
        "nickname": "Noodle456",
        "gameId": create_client.game_id,
    }))
    assert join_client.game_id == create_client.game_id
    assert any(msg.get("type") == "welcome" and msg.get("mode") == "join_game" for msg in join_client.control_messages)

    await server.handle_message(join_client, json_msg({"type": "leave_game"}))
    assert join_client.game_id is None
    assert join_client.snake_id is None
    assert join_client.last_control()["type"] == "left_game"

    await cleanup_server(server)


async def test_just_play_creates_running_bot_game() -> None:
    await reset_manager(server)
    client = await just_play_via_message(server, "SnackGPT1")
    game = current_game(server, client)
    assert game.phase == "running"
    assert game.desired_bot_count <= 4
    assert any(s.bot for s in game.snakes.values()), "just_play fallback should create bots"
    await cleanup_server(server)


async def test_all_time_high_request_before_join() -> None:
    await reset_manager(server)
    server.manager.record_all_time_high("NoodleHero", 21)
    client = DummyClient()
    await server.handle_message(client, json_msg({"type": "all_time_high"}))
    response = client.last_control()
    assert response is not None
    assert response["type"] == "all_time_high"
    assert response["scores"][0]["nickname"] == "NoodleHero"
    assert response["scores"][0]["length"] == 21
    await cleanup_server(server)


async def test_in_game_expected_inputs() -> None:
    await reset_manager(server)
    client = await just_play_via_message(server, "Wiggle123")
    game = current_game(server, client)
    game.phase = "running"
    snake = current_snake(game, client)

    old_direction = snake.direction
    allowed_direction = next(d for d in server.DIRS if d != old_direction and server.OPPOSITE.get(d) != old_direction)
    await server.handle_message(client, json_msg({"type": "input", "dir": allowed_direction, "seq": 10, "clientTime": 12345}))
    assert snake.pending_direction == allowed_direction
    assert snake.last_input_seq == 10

    set_snake_length(server, snake, 7)
    await server.handle_message(client, json_msg({"type": "sprint", "dir": snake.direction, "seq": 3, "clientTime": 12346}))
    assert snake.pending_sprint is True
    assert snake.last_sprint_seq == 3

    segments = [[10, 10], [9, 10], [8, 10]]
    await server.handle_message(client, json_msg({
        "type": "telemetry",
        "seq": 12,
        "dir": "left",
        "length": 7,
        "segments": segments,
        "clientTime": 12347,
    }))
    assert client.last_telemetry is not None
    assert client.last_telemetry["seq"] == 12
    assert client.last_telemetry["dir"] == "left"
    assert client.last_telemetry["length"] == 7
    assert client.last_telemetry["segments"] == segments

    client.last_chat_time = 0
    await server.handle_message(client, json_msg({"type": "chat", "text": "hello noodles"}))
    assert any(msg.get("type") == "chat" and msg.get("text") == "hello noodles" for msg in game.chat_history)

    await cleanup_server(server)


async def test_expected_error_paths_do_not_block_valid_inputs() -> None:
    await reset_manager(server)
    client = DummyClient()
    await server.handle_message(client, "not json")
    assert_error(client, "BAD_JSON")

    await server.handle_message(client, json.dumps(["not", "an", "object"]))
    assert_error(client, "BAD_PAYLOAD")

    await server.handle_message(client, json_msg({"type": "input", "dir": "up", "seq": 1}))
    assert_error(client, "NOT_IN_GAME")

    await cleanup_server(server)


async def main() -> None:
    tests = [
        test_environment_expected_inputs,
        test_websocket_transport_expected_inputs,
        test_public_game_lifecycle_messages,
        test_just_play_creates_running_bot_game,
        test_all_time_high_request_before_join,
        test_in_game_expected_inputs,
        test_expected_error_paths_do_not_block_valid_inputs,
    ]
    for test in tests:
        await test()
        print(f"PASS {test.__name__}")
    await cleanup_server(server)


if __name__ == "__main__":
    asyncio.run(main())

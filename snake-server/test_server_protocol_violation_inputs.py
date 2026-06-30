#!/usr/bin/env python3
"""Protocol/readme violation tests for message types, case, and duplicates.

These tests exercise clients that do not follow the documented protocol. They do
not add new server features; they lock in that unexpected case variants,
unknown in-game message types, duplicate JSON keys, duplicate nicknames, and
case variants of Game-IDs do not crash or corrupt server state.
"""

from __future__ import annotations

import asyncio
import json

from server_input_test_utils import (
    DummyClient,
    assert_error,
    cleanup_server,
    create_via_message,
    current_game,
    current_snake,
    load_server,
    reset_manager,
    set_snake_length,
    temporary_env,
)

server = load_server("snake_server_protocol_violation_inputs")


def json_msg(payload) -> str:
    return json.dumps(payload, separators=(",", ":"))


def last_error_code(client: DummyClient) -> str | None:
    errors = client.errors()
    if not errors:
        return None
    code = errors[-1].get("code")
    return str(code) if code is not None else None


async def test_unknown_in_game_message_types_are_rejected() -> None:
    await reset_manager(server)
    client = await create_via_message(server, "Proto1")
    game = current_game(server, client)
    game.phase = "running"
    for msg_type in ["move", "turn", "say", "ping", "state", "join_game", "create_game", "just_play", "", "chat "]:
        await server.handle_message(client, json_msg({"type": msg_type, "dir": "left", "text": "hello"}))
        assert last_error_code(client) in {"UNKNOWN_TYPE", "ALREADY_JOINED"}, client.control_messages
    await cleanup_server(server)


async def test_message_type_and_direction_case_sensitivity() -> None:
    await reset_manager(server)
    public_client = DummyClient()
    for msg_type in ["Just_Play", "CREATE_GAME", "Join_Game", "ALL_TIME_HIGH", "Chat"]:
        await server.handle_message(public_client, json_msg({"type": msg_type, "nickname": "Proto2"}))
        assert last_error_code(public_client) in {"NOT_IN_GAME", "UNKNOWN_TYPE"}, public_client.control_messages

    client = await create_via_message(server, "Proto3")
    game = current_game(server, client)
    game.phase = "running"
    snake = current_snake(game, client)
    set_snake_length(server, snake, 8)
    snake.direction = "right"
    snake.pending_direction = "right"
    for bad_type in ["Input", "INPUT", "Sprint", "Telemetry", "CHAT"]:
        await server.handle_message(client, json_msg({"type": bad_type, "dir": "left", "text": "hello"}))
        assert last_error_code(client) == "UNKNOWN_TYPE", client.control_messages
    for bad_dir in ["Left", "LEFT", " left", "left ", "up\n", "RIGHT"]:
        await server.handle_message(client, json_msg({"type": "input", "dir": bad_dir}))
        assert snake.pending_direction == "right"
    await cleanup_server(server)


async def test_duplicate_json_keys_follow_json_parser_without_corrupting_state() -> None:
    await reset_manager(server)
    client = await create_via_message(server, "Proto4")
    game = current_game(server, client)
    game.phase = "running"
    snake = current_snake(game, client)
    set_snake_length(server, snake, 8)
    snake.direction = "right"
    snake.pending_direction = "right"

    # Python json keeps the last duplicate key. The server must remain stable
    # and bounded even when a client sends duplicate fields.
    await server.handle_message(client, '{"type":"chat","type":"input","dir":"up","text":"hello"}')
    assert snake.pending_direction == "up"
    assert len(game.chat_history) <= server.CHAT_HISTORY_LIMIT

    before = len(game.chat_history)
    with temporary_env(SNAKE_DEBUG="1"):
        await server.handle_message(client, '{"type":"input","type":"chat","dir":"right","text":"duplicate wins"}')
    assert len(game.chat_history) == before + 1
    assert game.chat_history[-1]["text"] == "duplicate wins"
    await cleanup_server(server)


async def test_game_id_case_variants_join_same_game_not_duplicate_game() -> None:
    await reset_manager(server)
    owner = await create_via_message(server, "Proto5")
    game_id = owner.game_id
    assert game_id is not None
    before_ids = set(server.manager.games.keys())

    joiner = DummyClient()
    await server.handle_message(joiner, json_msg({"type": "join_game", "nickname": "Proto6", "gameId": game_id.lower()}))
    assert joiner.game_id == game_id
    assert set(server.manager.games.keys()) == before_ids
    assert len(server.manager.games) == len(before_ids)
    await cleanup_server(server)


async def test_duplicate_nicknames_get_distinct_connections_and_snakes() -> None:
    await reset_manager(server)
    owner = await create_via_message(server, "SameNick")
    game_id = owner.game_id
    assert game_id is not None
    joiner = DummyClient()
    await server.handle_message(joiner, json_msg({"type": "join_game", "nickname": "SameNick", "gameId": game_id}))
    assert joiner.game_id == game_id
    assert owner.conn_id != joiner.conn_id
    assert owner.snake_id != joiner.snake_id
    game = current_game(server, owner)
    assert owner.conn_id in game.players
    assert joiner.conn_id in game.players
    await cleanup_server(server)


async def main() -> None:
    tests = [
        test_unknown_in_game_message_types_are_rejected,
        test_message_type_and_direction_case_sensitivity,
        test_duplicate_json_keys_follow_json_parser_without_corrupting_state,
        test_game_id_case_variants_join_same_game_not_duplicate_game,
        test_duplicate_nicknames_get_distinct_connections_and_snakes,
    ]
    failures = []
    for test in tests:
        try:
            await test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        finally:
            await cleanup_server(server)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

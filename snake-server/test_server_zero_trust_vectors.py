#!/usr/bin/env python3
"""Additional zero-trust regression tests for hostile-but-valid client input.

These tests intentionally do not modify server.py.  They lock in that common
attack strings, empty/missing fields, overlong transport frames, and client
lifecycle events are rejected, ignored, bounded, or cleaned up without changing
server-authoritative state in unexpected ways.
"""

from __future__ import annotations

import asyncio
import json

from server_input_test_utils import (
    DummyClient,
    FakeWebSocket,
    assert_error,
    cleanup_server,
    current_game,
    just_play_via_message,
    load_server,
    reset_manager,
)

server = load_server("snake_server_zero_trust_vectors")


def json_msg(payload) -> str:
    return json.dumps(payload, separators=(",", ":"))


INJECTION_STRINGS = [
    "<script>alert(1)</script>",
    "\"; DROP TABLE snakes; --",
    "../../../../etc/passwd",
    "${jndi:ldap://127.0.0.1/a}",
    "{{constructor.constructor('alert(1)')()}}",
    "__proto__",
    "admin\x00Noodle",
    "line1\nline2\r\tline3",
    "A" * 4096,
]


async def test_public_lifecycle_rejects_injection_nicknames() -> None:
    await reset_manager(server)
    for msg_type in ["just_play", "create_game", "join_game"]:
        for nickname in INJECTION_STRINGS:
            client = DummyClient()
            payload = {"type": msg_type, "nickname": nickname, "gameId": "ABCDE"}
            await server.handle_message(client, json_msg(payload))
            assert_error(client, "NICKNAME_INVALID")
            assert client.game_id is None
            assert client.snake_id is None
    assert server.manager.games == {}, "invalid nicknames must not create games"
    await cleanup_server(server)


async def test_join_game_rejects_injection_game_ids_without_side_effects() -> None:
    await reset_manager(server)
    creator = DummyClient()
    await server.handle_message(creator, json_msg({"type": "create_game", "nickname": "Valid123"}))
    assert creator.game_id is not None
    game_count = len(server.manager.games)

    for game_id in INJECTION_STRINGS + ["", "A", "ABCDEF", "abcde", "!!!!!", "A1B2\x00"]:
        client = DummyClient()
        await server.handle_message(client, json_msg({"type": "join_game", "nickname": "Joiner1", "gameId": game_id}))
        assert client.game_id is None
        assert any(msg.get("type") == "error" for msg in client.control_messages), client.control_messages
        assert len(server.manager.games) == game_count

    await cleanup_server(server)


async def test_missing_empty_and_extra_fields_are_bounded() -> None:
    await reset_manager(server)
    client = DummyClient()
    for payload in [
        {},
        {"type": ""},
        {"type": "input"},
        {"type": "chat"},
        {"type": "all_time_high", "nickname": "Ignored123", "segments": [[1, 2]] * 1000},
    ]:
        await server.handle_message(client, json_msg(payload))
        assert client.control_messages, f"expected a bounded response for {payload!r}"

    # all_time_high is public and should ignore extra fields rather than joining a game.
    assert client.game_id is None
    assert client.snake_id is None
    await cleanup_server(server)


async def test_chat_injection_is_data_only_and_history_is_bounded() -> None:
    await reset_manager(server)
    client = await just_play_via_message(server, "ChatSafe1")
    game = current_game(server, client)

    for index, text in enumerate(INJECTION_STRINGS * 6):
        client.last_chat_time = 0  # isolate sanitization/history behavior from flood behavior
        await server.handle_message(client, json_msg({"type": "chat", "text": f"{index}:{text}"}))
        assert len(game.chat_history) <= server.CHAT_HISTORY_LIMIT
        if game.chat_history:
            last = game.chat_history[-1]
            assert last["kind"] in {"player", "system"}
            if last["kind"] == "player":
                assert len(last["text"]) <= server.CHAT_MAX_LENGTH
                assert "\n" not in last["text"] and "\r" not in last["text"]

    await cleanup_server(server)


async def test_telemetry_cannot_create_unbounded_storage_or_authoritative_movement() -> None:
    await reset_manager(server)
    client = await just_play_via_message(server, "TeleSafe1")
    game = current_game(server, client)
    game.phase = "running"
    snake = game.snakes[client.snake_id]
    original_head = snake.head
    huge_segments = [[i, -i, "extra", {"nested": i}] for i in range(5000)]

    await server.handle_message(client, json_msg({
        "type": "telemetry",
        "seq": 123,
        "dir": "right",
        "length": 10 ** 12,
        "segments": huge_segments,
        "clientTime": "not-a-time",
        "__proto__": {"polluted": True},
    }))

    assert client.last_telemetry is not None
    assert len(client.last_telemetry["segments"]) == server.MAX_ACTIVE_SNAKES * 8
    assert client.last_telemetry["length"] == server.GRID_W * server.GRID_H
    assert snake.head == original_head, "client telemetry must not move the authoritative snake"
    await cleanup_server(server)


async def test_websocket_disconnect_cleans_up_joined_player() -> None:
    await reset_manager(server)
    ws = FakeWebSocket([
        json_msg({"type": "just_play", "nickname": "CloseMe1"}),
    ])
    await server.websocket_handler(ws)

    welcomes = [msg for msg in ws.sent_json if msg.get("type") == "welcome"]
    assert welcomes, ws.sent_json
    game_id = welcomes[-1]["gameId"]
    game = server.manager.games.get(game_id)
    if game is not None:
        assert game.human_count() == 0, "disconnect/end-of-stream must remove the joined human"
    await cleanup_server(server)


async def main() -> None:
    tests = [
        test_public_lifecycle_rejects_injection_nicknames,
        test_join_game_rejects_injection_game_ids_without_side_effects,
        test_missing_empty_and_extra_fields_are_bounded,
        test_chat_injection_is_data_only_and_history_is_bounded,
        test_telemetry_cannot_create_unbounded_storage_or_authoritative_movement,
        test_websocket_disconnect_cleans_up_joined_player,
    ]
    try:
        for test in tests:
            await test()
            print(f"PASS {test.__name__}")
    finally:
        await cleanup_server(server)


if __name__ == "__main__":
    asyncio.run(main())

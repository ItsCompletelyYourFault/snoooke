#!/usr/bin/env python3
"""Unicode and UTF edge-case regression tests for the server input surface."""

from __future__ import annotations

import asyncio
import json

from server_input_test_utils import (
    DummyClient,
    assert_error,
    cleanup_server,
    current_game,
    just_play_via_message,
    load_server,
    reset_manager,
)

server = load_server("snake_server_unicode_inputs")


def json_msg(payload) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


UNICODE_NICKNAMES = [
    "Noodle\U0001f40d",
    "Cafe\u0301Snake",
    "\u00c5Snake123",
    "\u03a3nake123",
    "\u202eNoodle1",
    "Zero\u200bWidth1",
    "Fullwidth\uff11",
    "\ud800Bad",
]

UNICODE_CHAT_STRINGS = [
    "hello \U0001f40d noodles",
    "family emoji \U0001f469\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
    "combining Cafe\u0301 and A\u030a",
    "rtl \u202enoitcerid",
    "zero\u200bwidth\u200djoiners",
    "lone surrogate \ud800 kept escaped by json",
    "null inside A\x00B and char255 \xff",
    "tabs\tnewlines\ncarriage\rreturns",
]


async def test_unicode_nicknames_are_rejected_by_ascii_nickname_policy() -> None:
    await reset_manager(server)
    for nickname in UNICODE_NICKNAMES:
        client = DummyClient()
        await server.handle_message(client, json_msg({"type": "just_play", "nickname": nickname}))
        assert_error(client, "NICKNAME_INVALID")
        assert client.game_id is None
    assert server.manager.games == {}
    await cleanup_server(server)


async def test_unicode_chat_is_serializable_sanitized_and_bounded() -> None:
    await reset_manager(server)
    client = await just_play_via_message(server, "UniChat1")
    game = current_game(server, client)

    for text in UNICODE_CHAT_STRINGS:
        client.last_chat_time = 0
        await server.handle_message(client, json_msg({"type": "chat", "text": text}))
        last = game.chat_history[-1]
        assert last["kind"] == "player"
        assert isinstance(last["text"], str)
        assert len(last["text"]) <= server.CHAT_MAX_LENGTH
        assert "\n" not in last["text"] and "\r" not in last["text"] and "\t" not in last["text"]
        # Ensure the message can be serialized like a real WebSocket send payload.
        json.dumps(last, ensure_ascii=True)

    await cleanup_server(server)


async def test_unicode_and_control_strings_in_non_chat_fields_do_not_crash() -> None:
    await reset_manager(server)
    client = await just_play_via_message(server, "UniCtrl1")
    game = current_game(server, client)
    game.phase = "running"

    for value in UNICODE_CHAT_STRINGS + UNICODE_NICKNAMES:
        await server.handle_message(client, json_msg({"type": "input", "dir": value, "seq": 1}))
        await server.handle_message(client, json_msg({"type": "telemetry", "dir": value, "length": value, "segments": [value]}))
        assert client.last_telemetry is not None
        assert client.last_telemetry["dir"] is None
        assert len(client.last_telemetry["segments"]) <= server.MAX_ACTIVE_SNAKES * 8

    await cleanup_server(server)


async def main() -> None:
    tests = [
        test_unicode_nicknames_are_rejected_by_ascii_nickname_policy,
        test_unicode_chat_is_serializable_sanitized_and_bounded,
        test_unicode_and_control_strings_in_non_chat_fields_do_not_crash,
    ]
    try:
        for test in tests:
            await test()
            print(f"PASS {test.__name__}")
    finally:
        await cleanup_server(server)


if __name__ == "__main__":
    asyncio.run(main())

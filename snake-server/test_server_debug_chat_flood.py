#!/usr/bin/env python3
"""Regression tests for debug-mode chat flood behavior.

Production/local-default behavior keeps the existing simple chat flood guard.
Debug/test mode disables that guard through SNAKE_DEBUG=1 so fuzzing can send
many chat messages without being rate-limited before the sanitizer is exercised.
"""

from __future__ import annotations

import asyncio
import json

from server_input_test_utils import (
    cleanup_server,
    current_game,
    just_play_via_message,
    load_server,
    reset_manager,
    temporary_env,
)

server = load_server("snake_server_debug_chat_flood")


def json_msg(payload) -> str:
    return json.dumps(payload, separators=(",", ":"))


def player_chat_count(game) -> int:
    return sum(1 for msg in game.chat_history if msg.get("kind") == "player")


async def test_chat_flood_guard_still_blocks_in_non_debug_mode() -> None:
    await reset_manager(server)
    with temporary_env(SNAKE_DEBUG="0"):
        assert not server.debug_mode_enabled()
        client = await just_play_via_message(server, "FloodA1")
        game = current_game(server, client)
        before = player_chat_count(game)
        await server.handle_message(client, json_msg({"type": "chat", "text": "first"}))
        await server.handle_message(client, json_msg({"type": "chat", "text": "second too fast"}))
        assert player_chat_count(game) == before + 1
    await cleanup_server(server)


async def test_chat_flood_guard_is_disabled_in_debug_mode() -> None:
    await reset_manager(server)
    with temporary_env(SNAKE_DEBUG="1"):
        assert server.debug_mode_enabled()
        client = await just_play_via_message(server, "FloodB1")
        game = current_game(server, client)
        before = player_chat_count(game)
        for idx in range(8):
            await server.handle_message(client, json_msg({"type": "chat", "text": f"debug fuzz chat {idx}"}))
        assert player_chat_count(game) == before + 8
    await cleanup_server(server)


async def main() -> None:
    tests = [
        test_chat_flood_guard_still_blocks_in_non_debug_mode,
        test_chat_flood_guard_is_disabled_in_debug_mode,
    ]
    try:
        for test in tests:
            await test()
            print(f"PASS {test.__name__}")
    finally:
        await cleanup_server(server)


if __name__ == "__main__":
    asyncio.run(main())

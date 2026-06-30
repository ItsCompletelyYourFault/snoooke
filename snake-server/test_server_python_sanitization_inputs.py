#!/usr/bin/env python3
"""Regression tests for Python-specific input-sanitization edge cases.

These tests intentionally exercise cases that are easy to miss in Python:
non-standard JSON NaN/Infinity constants accepted by json.loads, massive integer
literals that can raise ValueError during JSON parsing, and deeply nested JSON
that can raise RecursionError.  server.py is not modified by this test.
"""

from __future__ import annotations

import asyncio
import json

from server_input_test_utils import (
    DummyClient,
    cleanup_server,
    current_game,
    just_play_via_message,
    load_server,
    reset_manager,
    set_snake_length,
)

server = load_server("snake_server_python_sanitization_inputs")


def json_msg(payload) -> str:
    return json.dumps(payload, separators=(",", ":"))


async def assert_handle_does_not_raise(client, raw: str, label: str) -> None:
    try:
        await server.handle_message(client, raw)
    except Exception as exc:  # noqa: BLE001 - this is a crash-regression assertion
        raise AssertionError(f"handle_message crashed for {label}: {type(exc).__name__}: {exc}") from exc


async def test_non_finite_json_numbers_do_not_crash_numeric_fields() -> None:
    await reset_manager(server)
    client = await just_play_via_message(server, "PySafe01")
    game = current_game(server, client)
    game.phase = "running"
    snake = game.snakes[client.snake_id]
    set_snake_length(server, snake, 8)

    raw_messages = [
        '{"type":"input","dir":"left","seq":Infinity}',
        '{"type":"input","dir":"left","seq":-Infinity}',
        '{"type":"input","dir":"left","seq":NaN}',
        '{"type":"sprint","seq":Infinity}',
        '{"type":"sprint","seq":-Infinity}',
        '{"type":"telemetry","seq":Infinity,"length":Infinity,"segments":[]}',
        '{"type":"telemetry","seq":-Infinity,"length":-Infinity,"segments":[]}',
        '{"type":"telemetry","seq":NaN,"length":NaN,"segments":[]}',
    ]
    for raw in raw_messages:
        await assert_handle_does_not_raise(client, raw, raw)

    await cleanup_server(server)


async def test_massive_integer_literals_are_handled_as_bad_or_bounded_input() -> None:
    await reset_manager(server)
    client = DummyClient()
    huge_digits = "9" * 6000
    raw_messages = [
        '{"type":"just_play","nickname":"BigInt1","seq":' + huge_digits + '}',
        '{"type":"all_time_high","clientTime":' + huge_digits + '}',
    ]
    for raw in raw_messages:
        await assert_handle_does_not_raise(client, raw, "massive integer literal")
    await cleanup_server(server)


async def test_deeply_nested_json_is_rejected_without_crashing() -> None:
    await reset_manager(server)
    client = DummyClient()
    nested = "[" * 1400 + "0" + "]" * 1400
    raw = '{"type":"all_time_high","segments":' + nested + '}'
    await assert_handle_does_not_raise(client, raw, "deeply nested JSON")
    await cleanup_server(server)


async def test_python_dunder_and_format_strings_are_data_only() -> None:
    await reset_manager(server)
    client = await just_play_via_message(server, "Dunder1")
    game = current_game(server, client)
    game.phase = "running"

    payloads = [
        {"type": "chat", "text": "{.__class__.__mro__[1].__subclasses__()}"},
        {"type": "chat", "text": "%(asctime)s %(message)s"},
        {"type": "telemetry", "segments": [{"__class__": "Snake", "__dict__": {"alive": False}}]},
        {"type": "input", "dir": "__getattribute__", "seq": 10},
    ]
    for payload in payloads:
        client.last_chat_time = 0
        await assert_handle_does_not_raise(client, json_msg(payload), repr(payload))

    snake = game.snakes[client.snake_id]
    assert snake.alive is True
    if game.chat_history and game.chat_history[-1].get("kind") == "player":
        assert "__subclasses__" in game.chat_history[-1]["text"] or "%(" in game.chat_history[-1]["text"]
    await cleanup_server(server)


async def main() -> None:
    tests = [
        test_non_finite_json_numbers_do_not_crash_numeric_fields,
        test_massive_integer_literals_are_handled_as_bad_or_bounded_input,
        test_deeply_nested_json_is_rejected_without_crashing,
        test_python_dunder_and_format_strings_are_data_only,
    ]
    try:
        for test in tests:
            await test()
            print(f"PASS {test.__name__}")
    finally:
        await cleanup_server(server)


if __name__ == "__main__":
    asyncio.run(main())

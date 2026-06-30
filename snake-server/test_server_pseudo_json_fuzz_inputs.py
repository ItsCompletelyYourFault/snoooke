#!/usr/bin/env python3
"""Fuzz tests for pseudo-JSON and hand-crafted unsafe WebSocket payloads.

Unlike json.dumps/json_msg based tests, these cases deliberately build raw JSON-
looking strings with unescaped payload fragments. This simulates attackers that
send broken, partially valid, duplicate-key, or structurally confusing packets.
The invariant is stability: the server must reject, ignore, clamp, or treat data
as data without crashing or growing unbounded state.
"""

from __future__ import annotations

import asyncio
import random
import string
from typing import Iterable

from server_input_test_utils import (
    DummyClient,
    cleanup_server,
    current_game,
    just_play_via_message,
    load_server,
    reset_manager,
    temporary_env,
)

server = load_server("snake_server_pseudo_json_fuzz_inputs")
RNG = random.Random(0xBADC0DE)

UNSAFE_FRAGMENTS = [
    "plain",
    'quote"break',
    "brace}break",
    "bracket]break",
    "comma,break",
    "colon:break",
    "nul\x00byte",
    "line\nbreak",
    "tab\tbreak",
    "slash\\break",
    "</script><script>alert(1)</script>",
    "${jndi:ldap://127.0.0.1/a}",
    "__proto__",
    "constructor.prototype.polluted",
    "\xff" * 8,
    "😀" * 8,
    "A" * 4096,
]


def pseudo_json_message(msg_type_fragment: str, field_fragment: str = "") -> str:
    """Return JSON-shaped data without escaping caller-controlled fragments."""
    return '{"type":"' + msg_type_fragment + '",' + field_fragment + "}"


def random_ascii_fragment(max_len: int = 256) -> str:
    alphabet = string.ascii_letters + string.digits + string.punctuation + " \t\n\r\x00"
    return "".join(RNG.choice(alphabet) for _ in range(RNG.randint(0, max_len)))


async def assert_no_crash(client: DummyClient, raw: str | bytes, label: str) -> None:
    try:
        await server.handle_message(client, raw)
    except Exception as exc:  # noqa: BLE001 - this is a crash-regression test
        raise AssertionError(f"handle_message crashed for {label}: {type(exc).__name__}: {exc}") from exc


async def test_pseudo_json_public_messages_do_not_crash_or_create_unbounded_games() -> None:
    await reset_manager(server)
    with temporary_env(SNAKE_DEBUG="1"):
        for idx, fragment in enumerate(UNSAFE_FRAGMENTS):
            client = DummyClient()
            raw = pseudo_json_message(fragment, '"nickname":"Noodle1"')
            await assert_no_crash(client, raw, f"public pseudo type {idx}")
            assert len(server.manager.games) <= 1

        for _ in range(160):
            client = DummyClient()
            raw = pseudo_json_message(random_ascii_fragment(64), '"nickname":"' + random_ascii_fragment(64) + '"')
            await assert_no_crash(client, raw, "random public pseudo-json")
            assert len(server.manager.games) < 20
    await cleanup_server(server)


async def test_pseudo_json_in_game_messages_do_not_crash_or_grow_unbounded() -> None:
    await reset_manager(server)
    with temporary_env(SNAKE_DEBUG="1"):
        client = await just_play_via_message(server, "Pseudo1")
        game = current_game(server, client)
        game.phase = "running"
        cases: Iterable[str] = [
            pseudo_json_message("chat", '"text":"hello"'),
            pseudo_json_message("chat", '"text":"quote"break"'),
            pseudo_json_message("chat", '"text":"nul\x00inside"'),
            pseudo_json_message("input", '"dir":"left"'),
            pseudo_json_message("input", '"dir":"Left"'),
            pseudo_json_message("sprint", '"seq":1,"dir":"right"'),
            pseudo_json_message("telemetry", '"segments":[[1,2]],"length":5,"dir":"up"'),
            '{"type":"chat","text":"first","text":"second"}',
            '{"type":"chat","type":"input","dir":"left","text":"duplicate type"}',
            '{"type":"chat","text":' + ('"A"' * 32) + "}",
        ]
        for raw in cases:
            await assert_no_crash(client, raw, raw[:80])
            assert len(game.chat_history) <= server.CHAT_HISTORY_LIMIT
            if client.last_telemetry is not None:
                assert len(client.last_telemetry.get("segments", [])) <= server.MAX_ACTIVE_SNAKES * 8

        for _ in range(220):
            msg_type = RNG.choice(["input", "sprint", "telemetry", "chat", random_ascii_fragment(20)])
            field = RNG.choice([
                '"dir":"' + random_ascii_fragment(32) + '"',
                '"text":"' + random_ascii_fragment(256) + '"',
                '"segments":[' + random_ascii_fragment(128) + ']',
                random_ascii_fragment(128),
            ])
            await assert_no_crash(client, pseudo_json_message(msg_type, field), "random in-game pseudo-json")
            assert len(game.chat_history) <= server.CHAT_HISTORY_LIMIT
    await cleanup_server(server)


async def main() -> None:
    tests = [
        test_pseudo_json_public_messages_do_not_crash_or_create_unbounded_games,
        test_pseudo_json_in_game_messages_do_not_crash_or_grow_unbounded,
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

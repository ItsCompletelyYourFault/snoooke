#!/usr/bin/env python3
"""Heavier deterministic fuzzing for transport and message fields.

Focuses on random binary data, NUL/control characters, char(255)-style bytes,
long strings, tabs/newlines, unexpected frame objects, and repeated randomized
messages.  The invariant is stability and bounded storage; exact error codes are
less important for fuzz input.
"""

from __future__ import annotations

import asyncio
import json
import random
import string
from typing import Any

from server_input_test_utils import (
    DummyClient,
    FakeWebSocket,
    cleanup_server,
    current_game,
    just_play_via_message,
    load_server,
    reset_manager,
)

server = load_server("snake_server_deep_fuzz_inputs")
RNG = random.Random(0x5A4C0DE)

CONTROL_STRINGS = [
    "",
    "\x00",
    "\x00" * 32,
    "\xff",
    "\t\n\r",
    "left\x00right",
    "A" * 1,
    "B" * 15,
    "C" * 256,
    "D" * 4096,
    "E" * (130 * 1024),
]

MESSAGE_TYPES: list[Any] = [
    "just_play", "create_game", "join_game", "leave_game", "all_time_high",
    "input", "sprint", "telemetry", "chat", "", None, 0, True, [], {}, "\x00", "chat\xff",
]


def random_bytes() -> bytes:
    size = RNG.choice([0, 1, 2, 3, 8, 64, 1024, 8192, 65 * 1024, 130 * 1024])
    return bytes(RNG.randrange(0, 256) for _ in range(size))


def random_string(max_len: int = 4096) -> str:
    alphabet = string.ascii_letters + string.digits + string.punctuation + " \t\n\r\x00\xff"
    size = RNG.choice([0, 1, 2, 5, 15, 16, 64, 255, 256, 1024, max_len])
    return "".join(RNG.choice(alphabet) for _ in range(size))


def random_json_value(depth: int = 0) -> Any:
    if depth >= 4:
        return RNG.choice([None, True, False, RNG.randint(-10**9, 10**9), random_string(256)])
    choice = RNG.randrange(10)
    if choice == 0:
        return None
    if choice == 1:
        return RNG.choice([True, False])
    if choice == 2:
        return RNG.randint(-10**18, 10**18)
    if choice == 3:
        return RNG.uniform(-10**6, 10**6)
    if choice == 4:
        return random_string()
    if choice == 5:
        return RNG.choice(CONTROL_STRINGS)
    if choice == 6:
        return [random_json_value(depth + 1) for _ in range(RNG.randrange(0, 30))]
    if choice == 7:
        return {random_string(32): random_json_value(depth + 1) for _ in range(RNG.randrange(0, 12))}
    if choice == 8:
        return [[RNG.randint(-9999, 9999), RNG.randint(-9999, 9999)] for _ in range(RNG.randrange(0, 500))]
    return RNG.choice(MESSAGE_TYPES)


def random_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": RNG.choice(MESSAGE_TYPES),
        "nickname": RNG.choice(["Valid123", random_string(64), random_json_value(1)]),
        "gameId": RNG.choice(["ABCDE", "abcde", random_string(32), random_json_value(1)]),
        "dir": RNG.choice(["up", "down", "left", "right", random_string(64), random_json_value(1)]),
        "seq": random_json_value(1),
        "length": random_json_value(1),
        "segments": random_json_value(1),
        "clientTime": random_json_value(1),
        "text": RNG.choice(["hello", random_string(2048), random_json_value(1)]),
    }
    for _ in range(RNG.randrange(0, 8)):
        payload[random_string(24)] = random_json_value(1)
    return payload


async def test_random_transport_frames_and_weird_frame_types_do_not_crash() -> None:
    await reset_manager(server)
    frames: list[Any] = []
    for _ in range(180):
        kind = RNG.randrange(5)
        if kind == 0:
            frames.append(random_bytes())
        elif kind == 1:
            frames.append(random_string(2048))
        elif kind == 2:
            frames.append(json.dumps(random_payload(), separators=(",", ":")))
        elif kind == 3:
            frames.append("{" + random_string(512))
        else:
            frames.append(memoryview(random_bytes()))  # unexpected non-str/non-bytes frame object
    ws = FakeWebSocket(frames, yield_delay=0)
    await server.websocket_handler(ws)
    assert len(ws.sent_json) < 1000, "fuzz input should not create unbounded control output"
    assert len(server.manager.games) < 80, "fuzz input should not create unbounded games"
    await cleanup_server(server)


async def test_random_public_and_in_game_payloads_do_not_crash_or_grow_unbounded() -> None:
    await reset_manager(server)
    public_client = DummyClient()
    for _ in range(250):
        payload = random_payload()
        await server.handle_message(public_client, json.dumps(payload, separators=(",", ":")))
        assert len(public_client.control_messages) < 1000

    joined = await just_play_via_message(server, "FuzzGuy1")
    game = current_game(server, joined)
    game.phase = "running"
    for _ in range(350):
        payload = random_payload()
        payload["type"] = RNG.choice(["input", "sprint", "telemetry", "chat", random_json_value(1)])
        await server.handle_message(joined, json.dumps(payload, separators=(",", ":")))
        assert len(game.chat_history) <= server.CHAT_HISTORY_LIMIT
        if joined.last_telemetry is not None:
            assert len(joined.last_telemetry.get("segments", [])) <= server.MAX_ACTIVE_SNAKES * 8
    await cleanup_server(server)


async def main() -> None:
    tests = [
        test_random_transport_frames_and_weird_frame_types_do_not_crash,
        test_random_public_and_in_game_payloads_do_not_crash_or_grow_unbounded,
    ]
    try:
        for test in tests:
            await test()
            print(f"PASS {test.__name__}")
    finally:
        await cleanup_server(server)


if __name__ == "__main__":
    asyncio.run(main())

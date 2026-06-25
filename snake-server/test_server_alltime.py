#!/usr/bin/env python3
"""Small regression test for the volatile all-time highscore feature."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER_PATH = ROOT / "server.py"

spec = importlib.util.spec_from_file_location("snake_server_under_test", SERVER_PATH)
server = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = server
spec.loader.exec_module(server)


class DummyClient:
    def __init__(self) -> None:
        self.game_id = None
        self.sent: list[dict] = []

    def send_control(self, payload: dict) -> None:
        self.sent.append(payload)


async def main() -> None:
    server.manager = server.GameManager()
    manager = server.manager

    manager.record_all_time_high("NoodleNinja", 12)
    manager.record_all_time_high("SirNoodle", 8)
    manager.record_all_time_high("NoodleNinja", 10)  # lower duplicate is ignored
    manager.record_all_time_high("NoodleNinja", 18)  # higher duplicate updates

    snapshot = manager.all_time_high_snapshot()
    assert snapshot[0]["nickname"] == "NoodleNinja"
    assert snapshot[0]["length"] == 18
    assert snapshot[1]["nickname"] == "SirNoodle"
    assert len(snapshot) == 2
    assert "datetime" in snapshot[0] and snapshot[0]["datetime"].endswith("Z")

    client = DummyClient()
    await server.handle_message(client, json.dumps({"type": "all_time_high"}))
    assert client.sent, "server should answer all_time_high request before a game is joined"
    response = client.sent[-1]
    assert response["type"] == "all_time_high"
    assert response["scores"][0]["nickname"] == "NoodleNinja"
    assert response["scores"][0]["length"] == 18

    print("PASS server all-time highscore test")


if __name__ == "__main__":
    asyncio.run(main())

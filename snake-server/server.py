#!/usr/bin/env python3
"""
Multiplayer Snake WebSocket server.

Rules implemented here are intentionally server-authoritative. Clients may send
telemetry with their local coordinates as often as they like, but the server is
still the source of truth for movement, collisions, food, score, levels, chat,
bots, wins and losses.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import secrets
import signal
import string
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

HOST = os.environ.get("SNAKE_HOST", "0.0.0.0")
PORT = int(os.environ.get("SNAKE_PORT", "8765"))

GAME_ID_ALPHABET = string.ascii_uppercase + string.digits
GAME_ID_LENGTH = 5
NICKNAME_RE = re.compile(r"^[A-Za-z0-9]{5,15}$")

GRID_W = 64
GRID_H = 38
MAX_HUMAN_PLAYERS = 16
MAX_ACTIVE_SNAKES = 16
CREATE_GAME_WARMUP_SECONDS = 30.0

BASE_TICK_HZ = 10.0
CHICKEN_TICK_HZ = BASE_TICK_HZ * 0.50
NORMAL_TICK_HZ = BASE_TICK_HZ
NOODLE_TICK_HZ = BASE_TICK_HZ * 1.20
TICK_SMOOTHING = 0.06
FOOD_ADJUST_PERIOD = 1.20
BOT_RESPAWN_DELAY = 2.0
GAME_EMPTY_TTL = 60.0

CHAT_MAX_LENGTH = 255
CHAT_HISTORY_LIMIT = 40
STATE_SEND_LIMIT_HZ = 30.0

DIRS: dict[str, tuple[int, int]] = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}

COLORS = [
    "#ff5c8a", "#6c63ff", "#00c2a8", "#ffb703", "#8ac926", "#1982c4",
    "#ff6d00", "#8338ec", "#06d6a0", "#ef476f", "#ffd166", "#118ab2",
    "#f72585", "#4cc9f0", "#b5179e", "#80ed99", "#ff9770", "#cdb4db",
]
BOT_NAMES = [
    "BotBento", "SirNoodle", "ByteBoa", "MunchAI", "WiggleBot", "PastaBot",
    "Sneki", "NoodleOS", "SnackGPT", "CurlBot", "ZigZag", "PixelPython",
]


def unix_ms() -> int:
    return int(time.time() * 1000)


def mono() -> float:
    return time.monotonic()


def clamp_int(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def sanitize_chat(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    clean = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return clean[:CHAT_MAX_LENGTH]


def valid_nickname(nickname: Any) -> bool:
    return isinstance(nickname, str) and bool(NICKNAME_RE.fullmatch(nickname))


def point_to_json(p: tuple[int, int]) -> list[int]:
    return [p[0], p[1]]


def random_id(prefix: str = "") -> str:
    return prefix + secrets.token_hex(8)


@dataclass
class ClientConn:
    ws: ServerConnection
    conn_id: str = field(default_factory=lambda: random_id("c_"))
    nickname: str = ""
    game_id: Optional[str] = None
    snake_id: Optional[str] = None
    control_queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    latest_state: Optional[str] = None
    send_event: asyncio.Event = field(default_factory=asyncio.Event)
    send_task: Optional[asyncio.Task[None]] = None
    last_telemetry: Optional[dict[str, Any]] = None
    connected_at: float = field(default_factory=mono)

    def send_control(self, payload: dict[str, Any]) -> None:
        message = json.dumps(payload, separators=(",", ":"))
        if self.control_queue.full():
            # Prefer dropping one old control message over creating unbounded latency.
            try:
                self.control_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self.control_queue.put_nowait(message)
            self.send_event.set()
        except asyncio.QueueFull:
            # Extremely slow client; ignore this non-state message.
            pass

    def send_state(self, payload: dict[str, Any]) -> None:
        # Latest-state-wins. Slow clients skip stale snapshots instead of building lag.
        self.latest_state = json.dumps(payload, separators=(",", ":"))
        self.send_event.set()

    async def sender(self) -> None:
        try:
            while True:
                await self.send_event.wait()
                self.send_event.clear()

                while True:
                    try:
                        message = self.control_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self.ws.send(message)

                if self.latest_state is not None:
                    message = self.latest_state
                    self.latest_state = None
                    await self.ws.send(message)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            return


@dataclass
class Snake:
    snake_id: str
    nickname: str
    color: str
    body: Deque[tuple[int, int]]
    direction: str
    pending_direction: str
    bot: bool = False
    client: Optional[ClientConn] = None
    alive: bool = True
    grow: int = 0
    last_input_seq: int = 0
    death_reason: str = ""
    killed_by: Optional[str] = None
    joined_at: float = field(default_factory=mono)
    last_bot_turn_at: float = 0.0

    @property
    def length(self) -> int:
        return len(self.body)

    @property
    def head(self) -> tuple[int, int]:
        return self.body[0]

    def display_name(self) -> str:
        return f"{self.nickname}*" if self.bot else self.nickname


class Game:
    def __init__(
        self,
        manager: "GameManager",
        game_id: str,
        warmup_seconds: float,
        desired_bots: int,
    ) -> None:
        self.manager = manager
        self.game_id = game_id
        self.created_at = mono()
        self.start_at = self.created_at + max(0.0, warmup_seconds)
        self.phase = "warmup" if warmup_seconds > 0 else "running"
        self.desired_bot_count = min(max(0, desired_bots), MAX_ACTIVE_SNAKES)
        self.players: dict[str, ClientConn] = {}
        self.snakes: dict[str, Snake] = {}
        self.food: set[tuple[int, int]] = set()
        self.chat_history: list[dict[str, Any]] = []
        self.lock = asyncio.Lock()
        self.tick_hz = NORMAL_TICK_HZ
        self.target_tick_hz = NORMAL_TICK_HZ
        self.level = "Chicken"
        self.target_food_count = 5
        self.last_food_adjust = 0.0
        self.last_state_sent = 0.0
        self.next_bot_spawn_at = self.created_at
        self.last_winner_announcement_at = 0.0
        self.loop_task = asyncio.create_task(self.loop(), name=f"game-{game_id}")

    def human_count(self) -> int:
        return len(self.players)

    def active_snakes(self) -> list[Snake]:
        return [s for s in self.snakes.values() if s.alive]

    def active_count(self) -> int:
        return len(self.active_snakes())

    def is_valid_for_join(self) -> bool:
        if self.phase not in {"warmup", "running"}:
            return False
        if self.human_count() >= MAX_HUMAN_PLAYERS:
            return False
        if self.active_count() < MAX_ACTIVE_SNAKES:
            return True
        return any(s.bot and s.alive for s in self.snakes.values())

    def leader(self) -> Optional[Snake]:
        alive = self.active_snakes()
        if not alive:
            return None
        return max(alive, key=lambda s: (s.length, not s.bot, -s.joined_at))

    def _next_color(self) -> str:
        used = {s.color for s in self.snakes.values()}
        for color in COLORS:
            if color not in used:
                return color
        hue = secrets.randbelow(360)
        return f"hsl({hue} 90% 62%)"

    def _occupied_cells(self, include_dead: bool = False) -> set[tuple[int, int]]:
        cells: set[tuple[int, int]] = set(self.food)
        for snake in self.snakes.values():
            if include_dead or snake.alive:
                cells.update(snake.body)
        return cells

    def _find_spawn_cell(self) -> Optional[tuple[int, int]]:
        occupied = self._occupied_cells(include_dead=False)
        for _ in range(500):
            x = random.randint(3, GRID_W - 4)
            y = random.randint(3, GRID_H - 4)
            cell = (x, y)
            if cell in occupied:
                continue
            # Leave a small comfort bubble so new snakes do not appear inside chaos.
            too_close = False
            for ox, oy in occupied:
                if abs(ox - x) <= 2 and abs(oy - y) <= 2:
                    too_close = True
                    break
            if not too_close:
                return cell
        for y in range(1, GRID_H - 1):
            for x in range(1, GRID_W - 1):
                if (x, y) not in occupied:
                    return (x, y)
        return None

    def _make_snake(self, nickname: str, bot: bool, client: Optional[ClientConn]) -> Optional[Snake]:
        cell = self._find_spawn_cell()
        if cell is None:
            return None
        direction = random.choice(list(DIRS.keys()))
        snake = Snake(
            snake_id=random_id("s_"),
            nickname=nickname,
            color=self._next_color(),
            body=deque([cell]),
            direction=direction,
            pending_direction=direction,
            bot=bot,
            client=client,
        )
        self.snakes[snake.snake_id] = snake
        if client is not None:
            client.snake_id = snake.snake_id
        return snake

    def _kick_lowest_ranked_bot(self) -> bool:
        bots = [s for s in self.snakes.values() if s.bot and s.alive]
        if not bots:
            return False
        victim = min(bots, key=lambda s: (s.length, s.joined_at))
        self.snakes.pop(victim.snake_id, None)
        self.desired_bot_count = max(0, self.desired_bot_count - 1)
        self.broadcast_control({
            "type": "toast",
            "message": f"{victim.display_name()} made room for a human player.",
        })
        return True

    async def add_human(self, client: ClientConn, nickname: str, mode: str) -> tuple[bool, str]:
        async with self.lock:
            if not self.is_valid_for_join():
                return False, "Game is full."
            if self.active_count() >= MAX_ACTIVE_SNAKES and not self._kick_lowest_ranked_bot():
                return False, "Game is full."
            snake = self._make_snake(nickname=nickname, bot=False, client=client)
            if snake is None:
                return False, "Game has no safe spawn cells right now."

            self.players[client.conn_id] = client
            client.nickname = nickname
            client.game_id = self.game_id
            self._add_system_chat(f"{nickname} joined the game.")
            self._ensure_food(force=True)
            self._send_welcome(client, mode=mode)
            self.broadcast_state(force=True)
            return True, "ok"

    async def remove_human(self, client: ClientConn) -> None:
        async with self.lock:
            self.players.pop(client.conn_id, None)
            if client.snake_id and client.snake_id in self.snakes:
                snake = self.snakes[client.snake_id]
                self.snakes.pop(client.snake_id, None)
                self._add_system_chat(f"{snake.nickname} left the game.")
            client.game_id = None
            client.snake_id = None
            self.broadcast_state(force=True)

    async def receive_input(self, client: ClientConn, payload: dict[str, Any]) -> None:
        direction = payload.get("dir")
        if direction not in DIRS:
            return
        seq = clamp_int(payload.get("seq"), 0, 2_147_483_647)
        async with self.lock:
            snake = self.snakes.get(client.snake_id or "")
            if snake is None or not snake.alive or snake.bot:
                return
            if seq < snake.last_input_seq:
                return
            snake.last_input_seq = seq
            if OPPOSITE.get(direction) != snake.direction:
                snake.pending_direction = direction

    async def receive_telemetry(self, client: ClientConn, payload: dict[str, Any]) -> None:
        # Stored for observability and anti-cheat analysis. Not used for physics.
        segments = payload.get("segments", [])
        if isinstance(segments, list):
            segments = segments[:MAX_ACTIVE_SNAKES * 8]
        else:
            segments = []
        client.last_telemetry = {
            "time": unix_ms(),
            "seq": clamp_int(payload.get("seq"), 0, 2_147_483_647),
            "dir": payload.get("dir") if payload.get("dir") in DIRS else None,
            "length": clamp_int(payload.get("length"), 0, GRID_W * GRID_H),
            "segments": segments,
        }

    async def receive_chat(self, client: ClientConn, payload: dict[str, Any]) -> None:
        text = sanitize_chat(payload.get("text"))
        if not (1 <= len(text) <= CHAT_MAX_LENGTH):
            client.send_control({"type": "error", "code": "CHAT_INVALID", "message": "Chat message must be 1-255 characters."})
            return
        async with self.lock:
            if client.conn_id not in self.players:
                return
            message = {
                "type": "chat",
                "kind": "player",
                "from": client.nickname,
                "text": text,
                "time": unix_ms(),
            }
            self.chat_history.append(message)
            self.chat_history = self.chat_history[-CHAT_HISTORY_LIMIT:]
            self.broadcast_control(message)

    def _add_system_chat(self, text: str) -> None:
        message = {"type": "chat", "kind": "system", "from": "server", "text": text, "time": unix_ms()}
        self.chat_history.append(message)
        self.chat_history = self.chat_history[-CHAT_HISTORY_LIMIT:]
        self.broadcast_control(message)

    def broadcast_control(self, payload: dict[str, Any]) -> None:
        for player in list(self.players.values()):
            player.send_control(payload)

    def _send_welcome(self, client: ClientConn, mode: str) -> None:
        warmup_ms = max(0, int((self.start_at - mono()) * 1000)) if self.phase == "warmup" else 0
        client.send_control({
            "type": "welcome",
            "mode": mode,
            "playerId": client.snake_id,
            "gameId": self.game_id,
            "grid": {"w": GRID_W, "h": GRID_H},
            "maxPlayers": MAX_HUMAN_PLAYERS,
            "maxActiveSnakes": MAX_ACTIVE_SNAKES,
            "phase": self.phase,
            "warmupMs": warmup_ms,
            "serverNow": unix_ms(),
            "chatHistory": self.chat_history[-CHAT_HISTORY_LIMIT:],
        })

    def _level_targets(self) -> tuple[str, float, int]:
        leader = self.leader()
        leader_points = leader.length if leader is not None else 1
        if leader_points < 10:
            return "Chicken", CHICKEN_TICK_HZ, 5
        if leader_points <= 40:
            return "Normal", NORMAL_TICK_HZ, 3
        return "Noodle", NOODLE_TICK_HZ, 1

    def _update_level_progression(self) -> None:
        new_level, target_hz, target_food = self._level_targets()
        if new_level != self.level:
            self._add_system_chat(f"Level changed to {new_level}.")
        self.level = new_level
        self.target_tick_hz = target_hz
        self.target_food_count = target_food
        self.tick_hz += (self.target_tick_hz - self.tick_hz) * TICK_SMOOTHING

    def _ensure_food(self, force: bool = False) -> None:
        now = mono()
        if not force and now - self.last_food_adjust < FOOD_ADJUST_PERIOD:
            return
        self.last_food_adjust = now

        occupied = self._occupied_cells(include_dead=False)
        attempts = 0
        while len(self.food) < self.target_food_count and attempts < 1000:
            attempts += 1
            cell = (random.randint(1, GRID_W - 2), random.randint(1, GRID_H - 2))
            if cell not in occupied:
                self.food.add(cell)
                occupied.add(cell)

        if len(self.food) > self.target_food_count:
            remove_count = len(self.food) - self.target_food_count
            for cell in random.sample(list(self.food), remove_count):
                self.food.remove(cell)

    def _choose_bot_direction(self, snake: Snake) -> str:
        head = snake.head
        occupied: set[tuple[int, int]] = set()
        for other in self.snakes.values():
            if not other.alive:
                continue
            # Allow bot to move into its own tail if that tail will probably leave.
            body = list(other.body)
            if other.snake_id == snake.snake_id and len(body) > 1:
                body = body[:-1]
            occupied.update(body)

        def safe_score(direction: str) -> tuple[int, float]:
            if OPPOSITE.get(direction) == snake.direction:
                return (-100000, random.random())
            dx, dy = DIRS[direction]
            nxt = (head[0] + dx, head[1] + dy)
            if not (0 <= nxt[0] < GRID_W and 0 <= nxt[1] < GRID_H):
                return (-10000, random.random())
            if nxt in occupied:
                return (-9000, random.random())
            wall_distance = min(nxt[0], GRID_W - 1 - nxt[0], nxt[1], GRID_H - 1 - nxt[1])
            if self.food:
                food_distance = min(abs(nxt[0] - fx) + abs(nxt[1] - fy) for fx, fy in self.food)
            else:
                food_distance = GRID_W + GRID_H
            jitter = random.random()
            return (wall_distance * 2 - food_distance, jitter)

        ranked = sorted(DIRS.keys(), key=safe_score, reverse=True)
        # Add a little personality; bots are not perfect.
        if len(ranked) > 1 and random.random() < 0.12:
            return ranked[1]
        return ranked[0]

    def _update_bots(self) -> None:
        for snake in self.snakes.values():
            if snake.bot and snake.alive:
                snake.pending_direction = self._choose_bot_direction(snake)

    def _spawn_bot(self) -> bool:
        if self.active_count() >= MAX_ACTIVE_SNAKES:
            return False
        base_name = random.choice(BOT_NAMES)
        suffix = secrets.randbelow(90) + 10
        name = f"{base_name}{suffix}"
        name = re.sub(r"[^A-Za-z0-9]", "", name)[:15]
        if len(name) < 5:
            name += "Bot"
        return self._make_snake(nickname=name, bot=True, client=None) is not None

    def _maintain_bots(self) -> None:
        if mono() < self.next_bot_spawn_at:
            return
        alive_bots = [s for s in self.snakes.values() if s.bot and s.alive]
        while len(alive_bots) < self.desired_bot_count and self.active_count() < MAX_ACTIVE_SNAKES:
            if not self._spawn_bot():
                break
            alive_bots = [s for s in self.snakes.values() if s.bot and s.alive]

    def _step_snakes(self) -> None:
        alive = [s for s in self.snakes.values() if s.alive]
        if not alive:
            return

        old_heads = {s.snake_id: s.head for s in alive}
        old_lengths = {s.snake_id: s.length for s in alive}
        new_heads: dict[str, tuple[int, int]] = {}
        will_eat: dict[str, bool] = {}

        for snake in alive:
            if snake.pending_direction in DIRS and OPPOSITE.get(snake.pending_direction) != snake.direction:
                snake.direction = snake.pending_direction
            dx, dy = DIRS[snake.direction]
            new_head = (snake.head[0] + dx, snake.head[1] + dy)
            new_heads[snake.snake_id] = new_head
            will_eat[snake.snake_id] = new_head in self.food

        body_owners: dict[tuple[int, int], str] = {}
        for snake in alive:
            body = list(snake.body)
            # Body collision excludes the old head; head-to-head and cross-swap are handled separately.
            body_without_head = body[1:]
            if body_without_head and not will_eat.get(snake.snake_id, False):
                body_without_head = body_without_head[:-1]
            for cell in body_without_head:
                body_owners.setdefault(cell, snake.snake_id)

        dead: dict[str, tuple[str, Optional[str]]] = {}

        for snake in alive:
            head = new_heads[snake.snake_id]
            if not (0 <= head[0] < GRID_W and 0 <= head[1] < GRID_H):
                dead[snake.snake_id] = ("wall", None)
                continue
            owner_id = body_owners.get(head)
            if owner_id is not None:
                if owner_id == snake.snake_id:
                    dead[snake.snake_id] = ("self", None)
                else:
                    # self._add_system_chat(f"{nickname} joined the game.")
                    dead[snake.snake_id] = ("body", owner_id)

        heads_to_snakes: dict[tuple[int, int], list[str]] = {}
        for snake_id, head in new_heads.items():
            heads_to_snakes.setdefault(head, []).append(snake_id)
        for snake_ids in heads_to_snakes.values():
            if len(snake_ids) > 1:
                for snake_id in snake_ids:
                    dead[snake_id] = ("head", None)

        # Cross-swap: two snakes pass through each other head-on between cells.
        ids = [s.snake_id for s in alive]
        for i, a_id in enumerate(ids):
            for b_id in ids[i + 1:]:
                if new_heads[a_id] == old_heads[b_id] and new_heads[b_id] == old_heads[a_id]:
                    dead[a_id] = ("head", None)
                    dead[b_id] = ("head", None)

        rewards: dict[str, int] = {}
        for dead_id, (reason, killer_id) in dead.items():
            if reason == "body" and killer_id and killer_id not in dead:
                reward = math.ceil(old_lengths[dead_id] * 0.30)
                if reward > 0:
                    rewards[killer_id] = rewards.get(killer_id, 0) + reward

        for snake in alive:
            if snake.snake_id in dead:
                reason, killer_id = dead[snake.snake_id]
                snake.alive = False
                snake.death_reason = reason
                snake.killed_by = killer_id
                if not snake.bot and snake.client is not None:
                    killer_name = None
                    if killer_id and killer_id in self.snakes:
                        killer_name = self.snakes[killer_id].display_name()
                    snake.client.send_control({
                        "type": "you_died",
                        "reason": reason,
                        "killerId": killer_id,
                        "killerName": killer_name,
                        "message": self._death_message(snake, reason, killer_name),
                    })
                continue

            gained = 0
            if will_eat[snake.snake_id] and new_heads[snake.snake_id] in self.food:
                self.food.remove(new_heads[snake.snake_id])
                gained += 1
            gained += rewards.get(snake.snake_id, 0)
            snake.grow += gained
            snake.body.appendleft(new_heads[snake.snake_id])
            if snake.grow > 0:
                snake.grow -= 1
            elif len(snake.body) > 1:
                snake.body.pop()

        bot_died = any(self.snakes[snake_id].bot for snake_id in dead if snake_id in self.snakes)

        after_alive = [s for s in self.snakes.values() if s.alive]
        if len(alive) > 1 and len(after_alive) == 1:
            winner = after_alive[0]
            if mono() - self.last_winner_announcement_at > 2.0:
                self.last_winner_announcement_at = mono()
                message = f"{winner.display_name()} wins this noodle round."
                self._add_system_chat(message)
                self.broadcast_control({
                    "type": "round_winner",
                    "winnerId": winner.snake_id,
                    "winnerName": winner.display_name(),
                    "message": message,
                })

        # Remove dead bots quickly. Humans remain as spectators/chatters with their last visible body.
        for snake_id in list(dead.keys()):
            snake = self.snakes.get(snake_id)
            if snake is not None and snake.bot:
                self.snakes.pop(snake_id, None)

        if bot_died:
            self.next_bot_spawn_at = mono() + BOT_RESPAWN_DELAY

    def _death_message(self, snake: Snake, reason: str, killer_name: Optional[str]) -> str:
        if reason == "wall":
            return "You splatted into a wall. Very noodle, very tragic."
        if reason == "self":
            return "You tied yourself into a snack-knot."
        if reason == "body" and killer_name:
            return f"You bonked into {killer_name}. They inherit 30% of your length."
        if reason == "head":
            return "Head-on noodle collision!"
        return "Your snake died."

    def _scoreboard(self) -> list[dict[str, Any]]:
        ranked = sorted(
            [s for s in self.snakes.values() if s.alive],
            key=lambda s: (s.length, not s.bot, -s.joined_at),
            reverse=True,
        )
        return [
            {
                "rank": i + 1,
                "id": s.snake_id,
                "nickname": s.display_name(),
                "score": s.length,
                "color": s.color,
                "bot": s.bot,
            }
            for i, s in enumerate(ranked[:3])
        ]

    def snapshot(self) -> dict[str, Any]:
        leader = self.leader()
        leader_id = leader.snake_id if leader else None
        warmup_ms = max(0, int((self.start_at - mono()) * 1000)) if self.phase == "warmup" else 0
        return {
            "type": "state",
            "gameId": self.game_id,
            "serverNow": unix_ms(),
            "phase": self.phase,
            "warmupMs": warmup_ms,
            "level": self.level,
            "tickHz": round(self.tick_hz, 2),
            "targetTickHz": round(self.target_tick_hz, 2),
            "targetFoodCount": self.target_food_count,
            "grid": {"w": GRID_W, "h": GRID_H},
            "food": [point_to_json(p) for p in self.food],
            "scoreboard": self._scoreboard(),
            "humanCount": self.human_count(),
            "activeSnakeCount": self.active_count(),
            "snakes": [
                {
                    "id": s.snake_id,
                    "nickname": s.display_name(),
                    "rawNickname": s.nickname,
                    "bot": s.bot,
                    "color": s.color,
                    "body": [point_to_json(p) for p in s.body],
                    "dir": s.direction,
                    "length": s.length,
                    "alive": s.alive,
                    "leader": s.snake_id == leader_id and s.alive,
                }
                for s in self.snakes.values()
            ],
        }

    def broadcast_state(self, force: bool = False) -> None:
        now = mono()
        if not force and now - self.last_state_sent < 1.0 / STATE_SEND_LIMIT_HZ:
            return
        self.last_state_sent = now
        state = self.snapshot()
        for player in list(self.players.values()):
            player.send_state(state)

    async def loop(self) -> None:
        try:
            while True:
                sleep_for = max(1.0 / max(self.tick_hz, 1.0), 0.02)
                async with self.lock:
                    if self.phase == "warmup" and mono() >= self.start_at:
                        self.phase = "running"
                        self._add_system_chat("Go! The noodles are loose.")

                    self._update_level_progression()
                    self._ensure_food()

                    if self.phase == "running":
                        self._maintain_bots()
                        self._update_bots()
                        self._step_snakes()
                        self._ensure_food()

                    self.broadcast_state()

                    if self.human_count() == 0 and mono() - self.created_at > GAME_EMPTY_TTL:
                        break

                    sleep_for = max(1.0 / max(self.tick_hz, 1.0), 0.02)

                await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            raise
        finally:
            await self.manager.remove_game(self.game_id)


class GameManager:
    def __init__(self) -> None:
        self.games: dict[str, Game] = {}
        self.lock = asyncio.Lock()

    def _generate_game_id(self) -> str:
        for _ in range(1000):
            game_id = "".join(secrets.choice(GAME_ID_ALPHABET) for _ in range(GAME_ID_LENGTH))
            if game_id not in self.games:
                return game_id
        raise RuntimeError("Could not generate unique game id")

    async def remove_game(self, game_id: str) -> None:
        async with self.lock:
            game = self.games.get(game_id)
            if game is not None and game.human_count() == 0:
                self.games.pop(game_id, None)

    async def create_game_for(
        self,
        client: ClientConn,
        nickname: str,
        warmup_seconds: float,
        desired_bots: int,
        mode: str,
    ) -> None:
        async with self.lock:
            game_id = self._generate_game_id()
            game = Game(self, game_id=game_id, warmup_seconds=warmup_seconds, desired_bots=desired_bots)
            self.games[game_id] = game

        if desired_bots > 0:
            async with game.lock:
                game._maintain_bots()

        ok, error = await game.add_human(client, nickname, mode=mode)
        if not ok:
            client.send_control({"type": "error", "code": "CREATE_FAILED", "message": error})

    async def just_play(self, client: ClientConn, nickname: str) -> None:
        async with self.lock:
            candidates = [
                g for g in self.games.values()
                if g.is_valid_for_join() and g.phase == "running" and g.active_count() > 0
            ]
        if candidates:
            game = random.choice(candidates)
            ok, error = await game.add_human(client, nickname, mode="just_play")
            if not ok:
                # Race condition: someone filled it between selection and join. Try a new bot match.
                await self.create_game_for(client, nickname, warmup_seconds=0.0, desired_bots=4, mode="just_play")
            return

        await self.create_game_for(client, nickname, warmup_seconds=0.0, desired_bots=4, mode="just_play")

    async def create_game(self, client: ClientConn, nickname: str) -> None:
        await self.create_game_for(
            client,
            nickname,
            warmup_seconds=CREATE_GAME_WARMUP_SECONDS,
            desired_bots=0,
            mode="create_game",
        )

    async def join_game(self, client: ClientConn, nickname: str, game_id: str) -> None:
        game_id = str(game_id or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{5}", game_id):
            client.send_control({"type": "error", "code": "GAME_ID_INVALID", "message": "Game-ID must be 5 characters: A-Z and 0-9."})
            return
        async with self.lock:
            game = self.games.get(game_id)
        if game is None or game.phase not in {"warmup", "running"}:
            client.send_control({"type": "error", "code": "GAME_NOT_FOUND", "message": "Game-ID is invalid or expired."})
            return
        ok, error = await game.add_human(client, nickname, mode="join_game")
        if not ok:
            client.send_control({"type": "error", "code": "GAME_FULL", "message": error})


manager = GameManager()


async def handle_message(client: ClientConn, raw: str) -> None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        client.send_control({"type": "error", "code": "BAD_JSON", "message": "Message is not valid JSON."})
        return
    if not isinstance(payload, dict):
        client.send_control({"type": "error", "code": "BAD_PAYLOAD", "message": "Message must be an object."})
        return

    msg_type = payload.get("type")

    if msg_type in {"just_play", "create_game", "join_game"}:
        if client.game_id is not None:
            client.send_control({"type": "error", "code": "ALREADY_JOINED", "message": "You are already in a game."})
            return
        nickname = str(payload.get("nickname", "")).strip()
        if not valid_nickname(nickname):
            client.send_control({"type": "error", "code": "NICKNAME_INVALID", "message": "Nickname must be 5-15 characters, A-Z, a-z and 0-9 only."})
            return
        if msg_type == "just_play":
            await manager.just_play(client, nickname)
        elif msg_type == "create_game":
            await manager.create_game(client, nickname)
        else:
            await manager.join_game(client, nickname, str(payload.get("gameId", "")))
        return

    if client.game_id is None:
        client.send_control({"type": "error", "code": "NOT_IN_GAME", "message": "Join or create a game first."})
        return

    async with manager.lock:
        game = manager.games.get(client.game_id)
    if game is None:
        client.send_control({"type": "error", "code": "GAME_GONE", "message": "Game no longer exists."})
        return

    if msg_type == "input":
        await game.receive_input(client, payload)
    elif msg_type == "telemetry":
        await game.receive_telemetry(client, payload)
    elif msg_type == "chat":
        await game.receive_chat(client, payload)
    else:
        client.send_control({"type": "error", "code": "UNKNOWN_TYPE", "message": "Unknown message type."})


async def websocket_handler(ws: ServerConnection) -> None:
    client = ClientConn(ws=ws)
    client.send_task = asyncio.create_task(client.sender(), name=f"sender-{client.conn_id}")
    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                if len(raw) > 64 * 1024:
                    client.send_control({"type": "error", "code": "TOO_LARGE", "message": "Binary message too large."})
                    continue
                raw = raw.decode("utf-8", errors="replace")
            if len(raw) > 128 * 1024:
                client.send_control({"type": "error", "code": "TOO_LARGE", "message": "Message too large."})
                continue
            await handle_message(client, raw)
    except ConnectionClosed:
        pass
    finally:
        if client.game_id is not None:
            async with manager.lock:
                game = manager.games.get(client.game_id)
            if game is not None:
                await game.remove_human(client)
        if client.send_task is not None:
            client.send_task.cancel()
            try:
                await client.send_task
            except asyncio.CancelledError:
                pass


async def main() -> None:
    stop = asyncio.Future()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set_result, None)
        except NotImplementedError:
            pass

    async with serve(
        websocket_handler,
        HOST,
        PORT,
        max_size=128 * 1024,
        max_queue=16,
        ping_interval=20,
        ping_timeout=20,
        compression=None,
    ):
        print(f"Snake server listening on ws://{HOST}:{PORT}", flush=True)
        await stop


if __name__ == "__main__":
    asyncio.run(main())

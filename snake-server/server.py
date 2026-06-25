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
import sys
import ssl
import string
import time
from collections import deque
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

HOST = os.environ.get("SNAKE_HOST", "0.0.0.0")
PORT = int(os.environ.get("SNAKE_PORT", "8765"))
SSL_MODE = os.environ.get("SNAKE_SSL", "auto").strip().lower()
SSL_CERT_FILE = os.environ.get("SNAKE_SSL_CERT", "fullchain.pem")
SSL_KEY_FILE = os.environ.get("SNAKE_SSL_KEY", "privkey.pem")

TRUTHY_ENV = {"1", "true", "yes", "y", "on"}
FALSEY_ENV = {"0", "false", "no", "n", "off", "none", "disabled"}

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

SPRINT_COST = 2
SPRINT_STEPS = 4
BOT_FLOOD_LIMIT = 180
SPAWN_MIN_WALL_DISTANCE = 4
SPAWN_MIN_SNAKE_DISTANCE = 5

CHAT_MAX_LENGTH = 255
CHAT_HISTORY_LIMIT = 40
CHAT_SECONDS_LIMIT = 20
STATE_SEND_LIMIT_HZ = 30.0
ALL_TIME_HIGH_LIMIT = 10

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
    "BotBento", "ByteBoa", "BoaBot", "WiggleBot", "PastaBot", "NoodleOS",
    "SnackGPT", "CurlBot", "PixelPython", "HissGPT", "SnekAI",
    "CoilPilotAI", "FangBot", "ViperGPT", "SlitherBot", "RattleBotGPT",
    "BoaByteBot", "NoodleGPT", "VenomAI", "HissistantAI", "Botaconda",
    "PythonGPT", "ScaleBot", "CurlGPT", "SnackOverflowAI", "SssiriBot",
    "AutoHissGPT", "PromptPython", "ByteMeBoaBot", "SnektronAI",
    "CoilCompilerBot", "DebuggerBoaGPT",
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


def env_flag(name: str) -> Optional[bool]:
    value = os.environ.get(name)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in TRUTHY_ENV:
        return True
    if normalized in FALSEY_ENV:
        return False
    return None


def debug_mode_enabled() -> bool:
    """Return True when running under a local/debugger-style setup.

    PyCharm sets PYCHARM_HOSTED for many run/debug configurations, and
    sys.gettrace() is non-None while a Python debugger is attached. An explicit
    SNAKE_DEBUG=1 override is also supported for local terminal runs.
    """
    explicit = env_flag("SNAKE_DEBUG")
    if explicit is not None:
        return explicit
    if os.environ.get("PYCHARM_HOSTED") == "1":
        return True
    if sys.gettrace() is not None:
        return True
    return any(name.startswith("pydevd") for name in sys.modules)


def build_ssl_context() -> tuple[Optional[ssl.SSLContext], str]:
    """Build the TLS context, or return None for plain local ws:// mode.

    SNAKE_SSL can be:
      - auto/default: use TLS when cert/key files exist, except in debug mode.
      - 1/true/yes/on: require TLS and fail if cert/key files are missing.
      - 0/false/no/off/none/disabled: force plain ws://.

    In PyCharm/debug mode the server skips SSL unless SNAKE_SSL=1 is set.
    """
    ssl_mode = os.environ.get("SNAKE_SSL", SSL_MODE).strip().lower()
    cert_file = os.environ.get("SNAKE_SSL_CERT", SSL_CERT_FILE)
    key_file = os.environ.get("SNAKE_SSL_KEY", SSL_KEY_FILE)
    forced_ssl = ssl_mode in TRUTHY_ENV
    disabled_ssl = ssl_mode in FALSEY_ENV

    if disabled_ssl:
        return None, "disabled by SNAKE_SSL"
    if debug_mode_enabled() and not forced_ssl:
        return None, "debug mode detected"

    cert_path = Path(cert_file)
    key_path = Path(key_file)
    if not cert_path.exists() or not key_path.exists():
        if forced_ssl:
            raise FileNotFoundError(
                f"SSL requested, but certificate/key files are missing: {cert_path}, {key_path}"
            )
        return None, "certificate/key files not found"

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(str(cert_path), keyfile=str(key_path))
    return ssl_context, f"certificate {cert_path} with key {key_path}"


@dataclass
class ClientConn:
    ws: ServerConnection
    conn_id: str = field(default_factory=lambda: random_id("c_"))
    nickname: str = ""
    last_chat_time: int = 0
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
    pending_sprint: bool = False
    last_sprint_seq: int = 0
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
        self.target_food_count = 10
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
        # Prefer cells with meaningful distance from walls and every visible snake body.
        # This avoids the frustrating instant-death spawn that can happen in busy arenas.
        occupied = self._occupied_cells(include_dead=True)
        snake_cells: set[tuple[int, int]] = set()
        for snake in self.snakes.values():
            snake_cells.update(snake.body)

        def local_space(cell: tuple[int, int], radius: int = 3) -> int:
            cx, cy = cell
            free = 0
            for y in range(cy - radius, cy + radius + 1):
                for x in range(cx - radius, cx + radius + 1):
                    if 0 <= x < GRID_W and 0 <= y < GRID_H and (x, y) not in occupied:
                        free += 1
            return free

        def score(cell: tuple[int, int], strict: bool) -> Optional[int]:
            x, y = cell
            if cell in occupied:
                return None
            wall_distance = min(x, GRID_W - 1 - x, y, GRID_H - 1 - y)
            if strict and wall_distance < SPAWN_MIN_WALL_DISTANCE:
                return None
            if snake_cells:
                snake_distance = min(abs(x - sx) + abs(y - sy) for sx, sy in snake_cells)
            else:
                snake_distance = GRID_W + GRID_H
            if strict and snake_distance < SPAWN_MIN_SNAKE_DISTANCE:
                return None
            center_bonus = GRID_W + GRID_H - (abs(x - GRID_W // 2) + abs(y - GRID_H // 2))
            return wall_distance * 10 + min(snake_distance, 18) * 12 + local_space(cell) + center_bonus // 6

        candidates: list[tuple[int, tuple[int, int]]] = []
        for _ in range(900):
            cell = (random.randint(1, GRID_W - 2), random.randint(1, GRID_H - 2))
            value = score(cell, strict=True)
            if value is not None:
                candidates.append((value, cell))
        if candidates:
            candidates.sort(reverse=True)
            elite = candidates[: min(12, len(candidates))]
            return random.choice(elite)[1]

        # Crowded fallback: still choose the safest available non-wall cell.
        fallback: list[tuple[int, tuple[int, int]]] = []
        for y in range(1, GRID_H - 1):
            for x in range(1, GRID_W - 1):
                value = score((x, y), strict=False)
                if value is not None:
                    fallback.append((value, (x, y)))
        if not fallback:
            return None
        fallback.sort(reverse=True)
        return fallback[0][1]

    def _choose_spawn_direction(self, cell: tuple[int, int]) -> str:
        occupied = self._occupied_cells(include_dead=True)
        ranked: list[tuple[int, str]] = []
        for direction, (dx, dy) in DIRS.items():
            score = 0
            for step in range(1, 7):
                nxt = (cell[0] + dx * step, cell[1] + dy * step)
                if not (0 <= nxt[0] < GRID_W and 0 <= nxt[1] < GRID_H) or nxt in occupied:
                    score -= 1000 // step
                    break
                score += 10
            ranked.append((score + random.randrange(0, 3), direction))
        return max(ranked)[1]

    def _make_snake(self, nickname: str, bot: bool, client: Optional[ClientConn]) -> Optional[Snake]:
        cell = self._find_spawn_cell()
        if cell is None:
            return None
        direction = self._choose_spawn_direction(cell)
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
            self.manager.record_all_time_high(nickname, snake.length)
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

    async def receive_sprint(self, client: ClientConn, payload: dict[str, Any]) -> None:
        # Sprint is a one-shot request. The server validates it at receive time and
        # consumes it on the next game tick. Clients cannot force movement distance.
        seq = clamp_int(payload.get("seq"), 0, 2_147_483_647)
        async with self.lock:
            snake = self.snakes.get(client.snake_id or "")
            if snake is None or not snake.alive or snake.bot or self.phase != "running":
                return
            if seq < snake.last_sprint_seq:
                return
            snake.last_sprint_seq = seq
            if snake.pending_sprint:
                return
            if snake.length <= 5:
                return
            snake.pending_sprint = True

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
        now : int= int(time.time())

        if not (1 <= len(text) <= CHAT_MAX_LENGTH):
            client.send_control({"type": "error", "code": "CHAT_INVALID", "message": "Chat message must be 1-255 characters."})
            return

        if client.last_chat_time+CHAT_HISTORY_LIMIT > now:
            print(f"Ignoring chat message from {client.nickname} for flood ...", flush=True)
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
            client.last_chat_time = now

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
        if leader_points < 5:
            return "Chicken", CHICKEN_TICK_HZ, 5
        if leader_points <= 15:
            return "Noodle", NORMAL_TICK_HZ, 10
        if leader_points <= 45:
            return "Ramen", NORMAL_TICK_HZ, 15
        return "Nani?!", NOODLE_TICK_HZ, 30

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

        # Level rules define the minimum amount of ambient food. Bonus food from
        # sprint/death drops is intentionally not pruned here, so special drops
        # stay on the board until somebody eats them.
        occupied = self._occupied_cells(include_dead=False)
        attempts = 0
        while len(self.food) < self.target_food_count and attempts < 1000:
            attempts += 1
            cell = (random.randint(1, GRID_W - 2), random.randint(1, GRID_H - 2))
            if cell not in occupied:
                self.food.add(cell)
                occupied.add(cell)

        # Sprint drops intentionally allow temporary extra food above the level target.
        # Treat the level's food count as the minimum baseline, and trim only extreme
        # excess so sprint-heavy games cannot grow food forever.
        max_food = self.target_food_count + MAX_ACTIVE_SNAKES * 2
        if len(self.food) > max_food:
            remove_count = len(self.food) - max_food
            for cell in random.sample(list(self.food), remove_count):
                self.food.remove(cell)

    def _scatter_death_food(self, origin: tuple[int, int], old_length: int) -> int:
        if old_length <= 5:
            return 0

        min_drops = max(1, math.floor(old_length * 0.10))
        max_drops = max(min_drops, math.ceil(old_length * 0.30))
        drop_count = random.randint(min_drops, max_drops)

        cx = min(max(origin[0], 1), GRID_W - 2)
        cy = min(max(origin[1], 1), GRID_H - 2)
        blocked = self._occupied_cells(include_dead=False)

        candidates: list[tuple[int, int]] = []
        for radius in range(0, 8):
            ring: list[tuple[int, int]] = []
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    cell = (cx + dx, cy + dy)
                    if not (1 <= cell[0] <= GRID_W - 2 and 1 <= cell[1] <= GRID_H - 2):
                        continue
                    if cell in blocked or cell in candidates:
                        continue
                    ring.append(cell)
            random.shuffle(ring)
            candidates.extend(ring)
            if len(candidates) >= drop_count:
                break

        spawned = 0
        for cell in candidates[:drop_count]:
            self.food.add(cell)
            blocked.add(cell)
            spawned += 1
        return spawned

    def _bot_blocked_cells(self, snake: Snake) -> set[tuple[int, int]]:
        blocked: set[tuple[int, int]] = set()
        for other in self.snakes.values():
            if not other.alive:
                continue
            body = list(other.body)
            if other.snake_id == snake.snake_id and len(body) > 1:
                # The bot can usually move into its own tail because the tail will move.
                body = body[:-1]
            blocked.update(body)
        return blocked

    def _bot_danger_cells(self, snake: Snake) -> set[tuple[int, int]]:
        danger: set[tuple[int, int]] = set()
        for other in self.snakes.values():
            if not other.alive or other.snake_id == snake.snake_id:
                continue
            for direction, (dx, dy) in DIRS.items():
                if OPPOSITE.get(direction) == other.direction:
                    continue
                cell = (other.head[0] + dx, other.head[1] + dy)
                if 0 <= cell[0] < GRID_W and 0 <= cell[1] < GRID_H:
                    danger.add(cell)
        return danger

    def _flood_fill_area(self, start: tuple[int, int], blocked: set[tuple[int, int]], limit: int = BOT_FLOOD_LIMIT) -> int:
        if start in blocked or not (0 <= start[0] < GRID_W and 0 <= start[1] < GRID_H):
            return 0
        seen = {start}
        queue: Deque[tuple[int, int]] = deque([start])
        while queue and len(seen) < limit:
            x, y = queue.popleft()
            for dx, dy in DIRS.values():
                nxt = (x + dx, y + dy)
                if not (0 <= nxt[0] < GRID_W and 0 <= nxt[1] < GRID_H):
                    continue
                if nxt in seen or nxt in blocked:
                    continue
                seen.add(nxt)
                queue.append(nxt)
        return len(seen)

    def _path_to_nearest_food(
        self,
        snake: Snake,
        blocked: set[tuple[int, int]],
        danger: set[tuple[int, int]],
    ) -> Optional[str]:
        if not self.food:
            return None
        start = snake.head
        queue: Deque[tuple[tuple[int, int], Optional[str]]] = deque([(start, None)])
        seen = {start}
        while queue and len(seen) < GRID_W * GRID_H:
            cell, first_dir = queue.popleft()
            if cell in self.food and first_dir is not None:
                return first_dir
            for direction, (dx, dy) in DIRS.items():
                if first_dir is None and OPPOSITE.get(direction) == snake.direction:
                    continue
                nxt = (cell[0] + dx, cell[1] + dy)
                if not (0 <= nxt[0] < GRID_W and 0 <= nxt[1] < GRID_H):
                    continue
                if nxt in seen or nxt in blocked:
                    continue
                # Avoid possible head-on cells unless this cell contains food and no alternative
                # has been found yet. It keeps bots aggressive but not suicidal.
                if nxt in danger and nxt not in self.food:
                    continue
                seen.add(nxt)
                queue.append((nxt, first_dir or direction))
        return None

    def _choose_bot_direction(self, snake: Snake) -> str:
        head = snake.head
        blocked = self._bot_blocked_cells(snake)
        danger = self._bot_danger_cells(snake)
        food_dir = self._path_to_nearest_food(snake, blocked, danger)

        def candidate_score(direction: str) -> tuple[float, float]:
            if OPPOSITE.get(direction) == snake.direction:
                return (-1_000_000.0, random.random())
            dx, dy = DIRS[direction]
            nxt = (head[0] + dx, head[1] + dy)
            if not (0 <= nxt[0] < GRID_W and 0 <= nxt[1] < GRID_H):
                return (-900_000.0, random.random())
            if nxt in blocked:
                return (-800_000.0, random.random())

            reachable = self._flood_fill_area(nxt, blocked)
            wall_distance = min(nxt[0], GRID_W - 1 - nxt[0], nxt[1], GRID_H - 1 - nxt[1])
            food_distance = min((abs(nxt[0] - fx) + abs(nxt[1] - fy) for fx, fy in self.food), default=GRID_W + GRID_H)
            score = reachable * 3.5 + wall_distance * 4.0 - food_distance * 2.2
            if direction == food_dir:
                score += 220
            if nxt in danger:
                score -= 180
            # Longer bots become more conservative about cramped spaces.
            if reachable < max(10, snake.length * 2):
                score -= 120
            return (score, random.random())

        ranked = sorted(DIRS.keys(), key=candidate_score, reverse=True)
        # Small randomness keeps bots from forming deterministic traffic jams.
        if len(ranked) > 1 and random.random() < 0.04:
            return ranked[1]
        return ranked[0]

    def _update_bots(self) -> None:
        for snake in self.snakes.values():
            if snake.bot and snake.alive:
                snake.pending_direction = self._choose_bot_direction(snake)
                snake.pending_sprint = False

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

    def _add_food_near(self, desired: tuple[int, int], occupied: set[tuple[int, int]]) -> bool:
        candidates = [desired]
        for radius in range(1, 5):
            ring: list[tuple[int, int]] = []
            for dx in range(-radius, radius + 1):
                dy = radius - abs(dx)
                ring.append((desired[0] + dx, desired[1] + dy))
                if dy:
                    ring.append((desired[0] + dx, desired[1] - dy))
            random.shuffle(ring)
            candidates.extend(ring)
        for cell in candidates:
            if not (0 <= cell[0] < GRID_W and 0 <= cell[1] < GRID_H):
                continue
            if cell in occupied or cell in self.food:
                continue
            self.food.add(cell)
            return True
        return False

    def _step_snakes(self) -> None:
        alive = [s for s in self.snakes.values() if s.alive]
        if not alive:
            return

        old_heads = {s.snake_id: s.head for s in alive}
        old_lengths = {s.snake_id: s.length for s in alive}
        paths: dict[str, list[tuple[int, int]]] = {}
        sprint_used: dict[str, bool] = {}
        sprint_drop_cells: list[tuple[int, int]] = []
        food_on_path: dict[str, set[tuple[int, int]]] = {}
        final_heads: dict[str, tuple[int, int]] = {}

        for snake in alive:
            if snake.pending_direction in DIRS and OPPOSITE.get(snake.pending_direction) != snake.direction:
                snake.direction = snake.pending_direction

            use_sprint = bool(snake.pending_sprint and not snake.bot and snake.length > 5)
            snake.pending_sprint = False
            sprint_used[snake.snake_id] = use_sprint
            if use_sprint and snake.body:
                sprint_drop_cells.append(snake.body[-1])

            steps = SPRINT_STEPS if use_sprint else 1
            dx, dy = DIRS[snake.direction]
            cx, cy = snake.head
            path: list[tuple[int, int]] = []
            for _ in range(steps):
                cx += dx
                cy += dy
                path.append((cx, cy))
            paths[snake.snake_id] = path
            final_heads[snake.snake_id] = path[-1]
            food_on_path[snake.snake_id] = {cell for cell in path if cell in self.food}

        # Body cells that should be lethal for head/body collisions this tick.
        #
        # The previous version removed tail cells twice: once through cells_vacating
        # and once again through a second "not will_eat" tail trim. For a length-3
        # snake this accidentally removed the segment directly behind the head from
        # the collision map, which made another snake able to pass through it.
        #
        # Build the predicted final body instead: all path cells except the final
        # head plus the old body cells that remain after movement, sprint cost and
        # already-pending growth/food on the path. This also makes the old head count
        # as body when it remains after movement, and makes sprint intermediate cells
        # collide as body instead of becoming invisible for one tick.
        body_owners: dict[tuple[int, int], set[str]] = {}
        own_path_body_cells: dict[str, set[tuple[int, int]]] = {}
        own_retained_old_cells: dict[str, set[tuple[int, int]]] = {}
        for snake in alive:
            snake_id = snake.snake_id
            path = paths[snake_id]
            steps = len(path)
            old_body = list(snake.body)
            old_length = old_lengths[snake_id]
            sprint_cost = SPRINT_COST if sprint_used.get(snake_id, False) else 0
            predicted_growth = min(steps, snake.grow + len(food_on_path.get(snake_id, set())))
            predicted_target_length = max(1, old_length - sprint_cost + predicted_growth)
            retained_old_count = max(0, min(old_length, predicted_target_length - steps))

            path_body_cells = set(path[:-1])
            retained_old_cells = set(old_body[:retained_old_count]) if retained_old_count > 0 else set()
            own_path_body_cells[snake_id] = path_body_cells
            own_retained_old_cells[snake_id] = retained_old_cells

            for cell in path_body_cells | retained_old_cells:
                body_owners.setdefault(cell, set()).add(snake_id)

        # Store earliest death event per snake. For the same sub-step, head-to-head
        # wins over body collision so body rewards are not paid for true head clashes.
        # Tuple layout: (step_index, priority, reason, killer_id). Lower is earlier.
        death_events: dict[str, tuple[int, int, str, Optional[str]]] = {}

        def mark_dead(snake_id: str, step_index: int, priority: int, reason: str, killer_id: Optional[str] = None) -> None:
            event = (step_index, priority, reason, killer_id)
            previous = death_events.get(snake_id)
            if previous is None or (event[0], event[1]) < (previous[0], previous[1]):
                death_events[snake_id] = event

        ids = [s.snake_id for s in alive]

        # Head collisions, including sprint paths that hit a head after another
        # snake's shorter movement has already stopped for this tick.
        for i, a_id in enumerate(ids):
            for b_id in ids[i + 1:]:
                a_path = paths[a_id]
                b_path = paths[b_id]
                max_steps = max(len(a_path), len(b_path))
                for step in range(max_steps):
                    a_pos = a_path[step] if step < len(a_path) else a_path[-1]
                    b_pos = b_path[step] if step < len(b_path) else b_path[-1]
                    a_prev = old_heads[a_id] if step == 0 else (a_path[step - 1] if step - 1 < len(a_path) else a_path[-1])
                    b_prev = old_heads[b_id] if step == 0 else (b_path[step - 1] if step - 1 < len(b_path) else b_path[-1])
                    if a_pos == b_pos or (a_pos == b_prev and b_pos == a_prev):
                        mark_dead(a_id, step, 0, "head", None)
                        mark_dead(b_id, step, 0, "head", None)
                        break

        for snake in alive:
            snake_id = snake.snake_id
            for step, head in enumerate(paths[snake_id]):
                if not (0 <= head[0] < GRID_W and 0 <= head[1] < GRID_H):
                    mark_dead(snake_id, step, 2, "wall", None)
                    break
                owners = body_owners.get(head)
                if owners:
                    self_body_hit = False
                    other_owner_id: Optional[str] = None
                    for owner_id in sorted(owners):
                        if owner_id == snake_id:
                            # A snake may sprint across cells that become its own
                            # trailing path during the same tick. That should not
                            # count as self-collision unless the cell was also part
                            # of the retained old body.
                            if (
                                head in own_path_body_cells.get(snake_id, set())
                                and head not in own_retained_old_cells.get(snake_id, set())
                            ):
                                continue
                            self_body_hit = True
                            break
                        other_owner_id = owner_id
                        break
                    if self_body_hit:
                        mark_dead(snake_id, step, 1, "self", None)
                        break
                    if other_owner_id is not None:
                        mark_dead(snake_id, step, 1, "body", other_owner_id)
                        break

        dead: dict[str, tuple[str, Optional[str]]] = {
            snake_id: (reason, killer_id)
            for snake_id, (_step, _priority, reason, killer_id) in death_events.items()
        }

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
                if not snake.bot:
                    self.manager.record_all_time_high(snake.nickname, old_lengths.get(snake.snake_id, snake.length))
                self._scatter_death_food(final_heads.get(snake.snake_id, snake.head), old_lengths.get(snake.snake_id, snake.length))
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
            for cell in paths[snake.snake_id]:
                if cell in self.food:
                    self.food.remove(cell)
                    gained += 1
            gained += rewards.get(snake.snake_id, 0)

            old_length = old_lengths[snake.snake_id]
            sprint_cost = SPRINT_COST if sprint_used.get(snake.snake_id, False) else 0
            base_target_length = max(1, old_length - sprint_cost)
            snake.grow += gained

            for cell in paths[snake.snake_id]:
                snake.body.appendleft(cell)

            realized_growth = min(len(paths[snake.snake_id]), snake.grow)
            snake.grow -= realized_growth
            target_length = max(1, base_target_length + realized_growth)
            while len(snake.body) > target_length:
                snake.body.pop()
            if not snake.bot:
                self.manager.record_all_time_high(snake.nickname, snake.length)

        occupied_after: set[tuple[int, int]] = set()
        for snake in self.snakes.values():
            occupied_after.update(snake.body)
        for cell in sprint_drop_cells:
            self._add_food_near(cell, occupied_after)

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
        self.all_time_high: list[dict[str, Any]] = []

    def _highscore_timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def record_all_time_high(self, nickname: str, length: int) -> None:
        """Store a volatile, runtime-only all-time leaderboard entry.

        Scores are kept in memory only and are unique by nickname. If the same
        nickname appears again, only a strictly higher length replaces the old
        entry. Bots are intentionally not recorded because the landing-page
        board is meant to celebrate human players.
        """
        if not valid_nickname(nickname):
            return
        score = clamp_int(length, 1, GRID_W * GRID_H)
        existing = next((row for row in self.all_time_high if row.get("nickname") == nickname), None)
        if existing is not None:
            if score <= int(existing.get("length", 0)):
                return
            existing["length"] = score
            existing["datetime"] = self._highscore_timestamp()
        else:
            self.all_time_high.append({
                "nickname": nickname,
                "length": score,
                "datetime": self._highscore_timestamp(),
            })
        self.all_time_high.sort(key=lambda row: (-int(row.get("length", 0)), str(row.get("datetime", "")), str(row.get("nickname", ""))))
        del self.all_time_high[ALL_TIME_HIGH_LIMIT:]

    def all_time_high_snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "rank": index + 1,
                "nickname": str(row.get("nickname", "")),
                "length": int(row.get("length", 0)),
                "datetime": str(row.get("datetime", "")),
            }
            for index, row in enumerate(self.all_time_high[:ALL_TIME_HIGH_LIMIT])
        ]

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

    if msg_type == "leave_game":
        if client.game_id is not None:
            async with manager.lock:
                game = manager.games.get(client.game_id)
            if game is not None:
                await game.remove_human(client)
        client.send_control({"type": "left_game", "message": "You left the game."})
        return

    if msg_type == "all_time_high":
        client.send_control({"type": "all_time_high", "scores": manager.all_time_high_snapshot(), "serverNow": unix_ms()})
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
    elif msg_type == "sprint":
        await game.receive_sprint(client, payload)
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

    ssl_context, ssl_reason = build_ssl_context()
    scheme = "wss" if ssl_context is not None else "ws"

    async with serve(
        websocket_handler,
        HOST,
        PORT,
        max_size=128 * 1024,
        max_queue=16,
        ping_interval=20,
        ping_timeout=20,
        compression=None,
        ssl=ssl_context,
    ):
        print(f"Snake server listening on {scheme}://{HOST}:{PORT} ({ssl_reason})", flush=True)
        await stop


if __name__ == "__main__":
    asyncio.run(main())

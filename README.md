# Multiplayer Noodle Snake

A browser-based multiplayer Snake game with a Python WebSocket server.

## Features

- Server-generated Game-ID: 5 characters, A-Z and 0-9.
- Landing choices:
  - Just Play: join a random running joinable game, or create a new instant game with 4 bots if none is available.
  - Join Game: manually enter a valid Game-ID.
  - Create Game: create a lobby with a 30 second server-side warm-up countdown.
- Nicknames: 5-15 characters, A-Z, a-z and 0-9.
- Per-game chatroom with 1-255 character messages.
- Live top 3 scoreboard.
- Up to 16 human players per game.
- If an active game is full and has bots, the lowest-ranked bot is kicked for a human player.
- Three level states based on the current leader's length:
  - Chicken: leader below 10 points, 50% slower tick speed, 5 food items.
  - Normal: leader 10-40 points, normal tick speed, 3 food items.
  - Noodle: leader above 40 points, 20% faster tick speed, 1 food item.
- Smooth level progression: tick speed interpolates slowly toward the target speed and food count changes gradually.
- Snake length is the score.
- Collision detection on the server: walls, own body, other snakes, head-on collisions and head swaps.
- Body-kill reward: if a snake dies by hitting another snake's body, the surviving body owner inherits 30% of the dead snake's length.
- Server-controlled bots with names ending in `*`.
- Unique server-decided snake colors.
- The leader has a crown and a golden glow in the browser.
- Clients send direction and coordinate telemetry as often as the render loop allows, while avoiding WebSocket backpressure.

## Run locally

```bash
cd multiplayer-snake-browser
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python server.py
```

Open a second terminal:

```bash
cd multiplayer-snake-browser/client
python -m http.server 8080
```

Then open:

```text
http://localhost:8080
```

The client connects to:

```text
ws://localhost:8765
```

If you host the client on another machine, keep port `8765` reachable or change `WS_URL` near the top of `client/index.html`.

## Production notes

This is a complete playable implementation, but a public deployment should add:

- TLS and WSS behind a reverse proxy.
- Origin checks.
- Rate limiting per IP and per connection.
- Authentication or session tokens if persistent identity matters.
- Horizontal sharding by Game-ID for very large deployments.
- Binary delta snapshots instead of JSON full snapshots if bandwidth becomes the bottleneck.
- Metrics for tick duration, queue pressure, connection count, games count and dropped clients.

## Server-authoritative design

Clients send telemetry frequently, including coordinates, length and direction. The server stores this for observation, but never trusts it for game physics. Movement, food, score, collisions, bot decisions, level changes and death events are calculated server-side.

This avoids the easiest cheating path: a client cannot simply submit a fake snake body or fake length and win.

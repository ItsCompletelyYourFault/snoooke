# Multiplayer Noodle Snake

A browser-based multiplayer Snake game with a Python WebSocket server.

## Features

- Server-generated Game-ID: 5 characters, A-Z and 0-9.
- Landing choices:
  - Just Play: join a random running joinable game, or create a new instant game with 4 bots if none is available.
  - Join Game: manually enter a valid Game-ID.
  - Create Game: create a lobby with a 30 second server-side warm-up countdown.
- Nicknames: 5-15 characters, A-Z, a-z and 0-9. The default/random nickname is chosen from a silly snake-name list and filtered to valid names.
- Per-game chatroom with 1-255 character messages; the chat panel has a fixed height and scrolls.
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
- Death-food scramble: snakes longer than 5 drop a random 10-30% of their old length as food near the death point.
- Server-controlled bots with names ending in `*`.
- Unique server-decided snake colors.
- The leader has a crown and a golden glow in the browser.
- Your own snake pulses and rapidly blinks between its assigned color and the inverse color for 5 seconds when play starts.
- The game top bar includes a Leave Game button so players can return to the landing screen and start/join another game.
- Clients send direction and coordinate telemetry as often as the render loop allows, while avoiding WebSocket backpressure.
- Sprint: human players with length > 5 can press Space to spend 2 length, move 4 cells in the current direction on the next server tick, and drop 1 food at the tail.
- Smarter bots path toward food with BFS-style route finding, avoid likely head-on danger cells, and prefer open space.
- Safer spawn placement keeps new players and bots away from walls and existing snake bodies.
- Mobile/touch support: landscape prompt, nipplejs virtual joystick, touch sprint button, and chat hidden behind a message icon with unread marker.

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

## Controls

Desktop:

- Arrow keys or WASD to steer.
- Space to sprint when your snake length is greater than 5.

Mobile/touch devices:

- The client detects iPhone, iPad and Android style devices.
- It requests landscape orientation when a match starts.
- Use the left virtual joystick to steer and the right Sprint button to sprint.
- Chat is hidden behind the chat icon; unread chat is shown with a marker.

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

This avoids the easiest cheating path: a client cannot simply submit a fake snake body, fake length or fake sprint distance and win.

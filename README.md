# Multiplayer Noodle Snake

A browser-based multiplayer Snake game with a Python WebSocket server.

**Important**: This project is a vibe coding project for experimenting! It is intended for experienced developers who
(as me) are interested in better understanding where AI will be able to assist and where not. 
Please feel free to optimize, bugfix, and improve the project but **please use AI (preferably ChatGPT) for as much as 
possible**!
Ask ChatGPT to change the color of a button. If you spot an issue where it messed up, tell ChatGPT to fix that
before submitting your code. It is not supposed to be a project of just bad code, not everything AI outputs should end
up here. But please try use vibe coding and share your experiences, struggles, and stories here or on reddit.
I am very interested in what happens when smart people use AI to improve code.

**This project is deeply insecure and full of bugs**. It is not ready for production use. It is for sure not safe to
run. Please don't run server.py anywhere near a production environment.

## Features

- Server-generated Game-ID: 5 characters, A-Z, and 0-9.
- Landing choices:
  - Just Play: join a random running joinable game, or create a new instant game with 4 bots if none is available.
  - Join Game: manually enter a valid Game-ID.
  - Create Game: create a lobby with a 30 second server-side warm-up countdown.
- Nicknames: 5-15 characters, A-Z, a-z, and 0–9. The default/random nickname is chosen from a silly snake-name list and filtered to valid names.
- Per-game chatroom with 1–255 character messages; the chat panel has a fixed height and scrolls.
- Live top 3 scoreboard.
- Volatile server-runtime all-time highscore board for the biggest human noodles, shown on the landing page.
- Up to 16 human players per game.
- If an active game is full and has bots, the lowest-ranked bot is kicked for a human player.
- Three level states based on the current leader's length:
  - Chicken: leader below 10 points, 50% slower tick speed, 5 food items.
  - Normal: leader 10-40 points, normal tick speed, 3 food items.
  - Noodle: leader above 40 points, 20% faster tick speed, 1 food item.
- Smooth level progression: tick speed interpolates slowly toward the target speed and food count changes gradually.
- Snake length is the score.
- Collision detection on the server: walls, own body, other snakes, head-on collisions, and head swaps.
- Body-kill reward: if a snake dies by hitting another snake's body, the surviving body owner inherits 30% of the dead snake's length.
- Death-food scramble: snakes longer than 5 drop a random 10-30% of their old length as food near the death point.
- Server-controlled bots with names ending in `*`.
- Unique server-decided snake colors.
- The leader has a crown and a golden glow in the browser.
- Your own snake pulses and rapidly blinks between its assigned color and the inverse color for 5 seconds when play starts.
- The game top bar includes a Leave Game button so players can return to the landing screen and start/join another game.
- Dead players can rejoin the same still-running game after a 5 second cooldown using the Rejoin button.
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
- The client now forces focus back to the canvas after joining, and stale focus in hidden landing inputs no longer eats movement keys.

Mobile/touch devices:

- The client detects iPhone, iPad, and Android style devices.
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
- Metrics for tick duration, queue pressure, connection count, games count, and dropped clients.

## Server-authoritative design

Clients send telemetry frequently, including coordinates, length, and direction. The server stores this for observation, but never trusts it for game physics. Movement, food, score, collisions, bot decisions, level changes, and death events are calculated server-side.

This avoids the easiest cheating path: a client cannot simply submit a fake snake body, fake length, or fake sprint distance and win.

## Mobile browser strategy

The client remains a single responsive `index.html` instead of a separate mobile page. Mobile devices switch into a focused game mode after joining a match:

- the page requests fullscreen/landscape where the browser permits it;
- the app uses `100dvh`/safe-area layout and locks scrolling during active play;
- the canvas fills the viewport while the HUD, joystick, sprint button, chat, and scoreboard are overlays;
- chat and scoreboard are hidden behind floating buttons and can be opened without leaving the game;
- the joystick uses nipplejs plus a direct pointer/touch fallback path so direction changes are sent immediately.

Some iOS browser chrome cannot be removed by JavaScript unless the page is installed as a home-screen web app. The CSS still minimizes wasted space in normal Safari/Chrome tabs.

## Local SSL / PyCharm debug mode

The server now supports automatic plain WebSocket mode for local development.

- `SNAKE_SSL=auto` is the default. The server uses TLS only when the configured certificate and key files exist and no debugger is detected.
- When the server is launched from PyCharm debug mode, or when `SNAKE_DEBUG=1` is set, SSL is skipped and the server listens on `ws://...`.
- Use `SNAKE_SSL=0` to force plain local WebSockets.
- Use `SNAKE_SSL=1` to require TLS and fail fast if the certificate or key is missing.
- Certificate paths can be changed with `SNAKE_SSL_CERT=/path/fullchain.pem` and `SNAKE_SSL_KEY=/path/privkey.pem`.

Examples:

```bash
SNAKE_SSL=0 python server.py
SNAKE_DEBUG=1 python server.py
SNAKE_SSL=1 SNAKE_SSL_CERT=fullchain.pem SNAKE_SSL_KEY=privkey.pem python server.py
```

In local `ws://` mode the existing browser client works from `http://localhost:8080`.

## Collision fixes in this version

The server collision map now predicts the snake bodies that remain after a tick instead of trimming tail cells twice. This fixes a bug where the body segment directly behind a snake's head could disappear from the collision map for one tick. The collision model also now treats an old head as body when it remains after movement, and checks sprint paths against body cells consistently.


## Testing
### Testing the collision model
```bash
python3 -m py_compile server.py
python3 test_server_collisions.py
```
Test collision model Head-To-Wall | Head-To-Head | Head-To-Body

### Testing the volatile all-time highscore
```bash
python3 test_server_alltime.py
```
The all-time board is in memory only. Restarting `server.py` resets it.

### Testing the browser keyboard controls
```bash
node test_index_keyboard.js
```
The keyboard test runs without external npm dependencies. It simulates a game join while the old Game-ID input still has focus, then verifies that a WASD key sends an input message instead of being ignored.

### Testing the server security for malformed inputs
1. Expected-input test
`test_server_expected_inputs.py` verifies that valid input still works:
* valid env import/config values;
* valid `SNAKE_SSL=0` and debug SSL behavior;
* WebSocket text and binary frames;
* `create_game`;
* `join_game`;
* `just_play`;
* `leave_game`;
* `all_time_high`;
* `input`;
* `sprint`;
* `telemetry`;
* `chat`;
baseline error handling for bad JSON / non-object JSON / in-game messages before joining.
```bash
cd snoooke-server-input-tests
python3 test_server_expected_inputs.py
```


2. Malicious-input test
`test_server_malicious_inputs.py` sends malformed and hostile-but-JSON-valid inputs to every identified location:
* invalid `SNAKE_PORT`;
* forced SSL with missing cert/key;
* oversized WebSocket frames;
* invalid UTF-8 bytes;
* invalid JSON;
* array payloads instead of objects;
* invalid nicknames;
* invalid Game-IDs;
* already-joined attempts;
* unknown message types;
* stale/invalid direction input;
* invalid sprint conditions;
* huge telemetry;
* non-list telemetry segments;
* empty / non-string / oversized chat;
* leave without game;
* ghost game;
* unhashable JSON values in type and dir.
  
```bash
cd snoooke-server-input-tests
python3 test_server_malicious_inputs.py
```

3. Fuzz-input test
`test_server_fuzz_inputs.py` performs deterministic random/fuzzy testing without extra dependencies. The malicious/fuzz suites verify that malformed JSON envelopes, unhashable JSON object/list values in `type` and `dir`, oversized transport frames, invalid UTF-8, invalid nicknames/Game-IDs, bad telemetry, and chat abuse are rejected, ignored, bounded, or handled without crashing the server.
* random env values;
* random raw JSON envelopes;
* random public message payloads;
* random in-game payloads;
* random WebSocket text/binary frames;
* oversized transport frames;
* invalid UTF-8 frames;
* random nested JSON values.

```bash
cd snoooke-server-input-tests
python3 test_server_fuzz_inputs.py
```

4. Full Validation:
```bash
python3 -m py_compile server.py server_input_test_utils.py test_server_expected_inputs.py test_server_malicious_inputs.py test_server_fuzz_inputs.py
python3 test_server_expected_inputs.py
python3 test_server_alltime.py
python3 test_server_collisions.py
```

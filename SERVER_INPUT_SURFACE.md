# Server input surface map

This document identifies the locations where the current `server.py` accepts external or client-controlled input. The server itself was not modified.

## 1. Process/runtime configuration input

Read during module import or TLS setup:

- `SNAKE_HOST` -> `HOST`
- `SNAKE_PORT` -> `PORT` via `int(...)`
- `SNAKE_SSL` -> SSL mode
- `SNAKE_SSL_CERT` -> certificate path
- `SNAKE_SSL_KEY` -> private-key path
- `SNAKE_DEBUG` -> debug SSL decision
- `PYCHARM_HOSTED`, `sys.gettrace()`, and loaded `pydevd*` modules -> debug-mode detection
- `SIGTERM` and `SIGINT` -> graceful shutdown path in `main()`

## 2. WebSocket transport input

`websocket_handler(ws)` accepts every incoming WebSocket frame:

- text frames are passed as JSON strings;
- binary frames <= 64 KiB are decoded as UTF-8 with replacement;
- binary frames > 64 KiB are rejected with `TOO_LARGE`;
- decoded/text frames > 128 KiB are rejected with `TOO_LARGE`;
- accepted frames are passed to `handle_message(client, raw)`.

## 3. JSON envelope input

`handle_message(client, raw)` accepts:

- raw JSON text/decoded bytes;
- JSON object payloads only;
- `payload["type"]` as message dispatcher input.

## 4. Public WebSocket message inputs

Handled before the client must be in a game:

- `just_play`: uses `nickname`.
- `create_game`: uses `nickname`.
- `join_game`: uses `nickname` and `gameId`.
- `leave_game`: no required fields.
- `all_time_high`: no required fields.

## 5. In-game WebSocket message inputs

Handled after the client is in a game:

- `input`: uses `dir`, `seq`, optional client telemetry fields are ignored.
- `sprint`: uses `seq`; `dir` and `clientTime` are accepted as telemetry but not trusted for physics.
- `telemetry`: uses `seq`, `dir`, `length`, `segments`, `clientTime`.
- `chat`: uses `text`.

## 6. Server-side trust boundary notes

- Client telemetry is stored, but not used for authoritative movement or collision physics.
- Nicknames are validated as 5-15 alphanumeric characters.
- Chat text is normalized and truncated to 255 characters.
- Telemetry `segments` are capped at `MAX_ACTIVE_SNAKES * 8` entries.

## 7. Current issues exposed by the new negative/fuzz tests

The server currently crashes on some malicious JSON object values because membership checks assume hashable strings:

- `payload["type"]` can be an object/list, causing a `TypeError` in `handle_message`.
- `payload["dir"]` can be an object/list, causing a `TypeError` in `receive_input`.
- `payload["dir"]` can be an object/list, causing a `TypeError` in `receive_telemetry`.

These are intentionally left unfixed because this task requested tests only.

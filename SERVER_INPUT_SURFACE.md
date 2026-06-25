# Server input surface map

This document identifies the locations where the current `server.py` accepts external or client-controlled input and notes the input-sanitization fixes added after the negative/fuzz tests were created.

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
- accepted frames are passed to `handle_message(client, raw)`;
- connection end/disconnect is client-controlled transport lifecycle input and triggers cleanup in `websocket_handler(...)`'s `finally` block.

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

## 7. Negative/fuzz issues fixed in this version

The negative/fuzz suites exposed crashes where membership checks assumed hashable strings:

- `payload["type"]` could be an object/list, causing a `TypeError` in `handle_message`.
- `payload["dir"]` could be an object/list, causing a `TypeError` in `receive_input`.
- `payload["dir"]` could be an object/list, causing a `TypeError` in `receive_telemetry`.

`server.py` now verifies that dispatcher `type` and direction `dir` values are strings before checking membership against known message types or `DIRS`. Invalid non-string values are rejected, ignored, or handled through the existing `NOT_IN_GAME` / `UNKNOWN_TYPE` error paths instead of crashing. `clamp_int` also catches `OverflowError`, and `handle_message` now treats non-JSON-compatible raw input as `BAD_JSON`.


## 8. Additional test-only coverage added after this review

No additional JSON message types or hidden gameplay input handlers were found beyond the documented environment, WebSocket transport, JSON envelope, public messages, in-game messages, and disconnect lifecycle surfaces.

New test files added without modifying `server.py`:

- `test_server_zero_trust_vectors.py`: injection-like strings, overlong fields, missing/extra fields, chat-as-data behavior, telemetry bounds, and WebSocket disconnect cleanup.
- `test_server_deep_fuzz_inputs.py`: heavier deterministic fuzzing with random binary frames, unexpected non-string/non-bytes frame objects, NUL/control characters, char(255)-style bytes, tabs/newlines, long strings, randomized public messages, and randomized in-game messages.
- `test_server_python_sanitization_inputs.py`: Python-specific parser/conversion edge cases such as JSON `NaN`/`Infinity`, massive integer literals, deeply nested JSON, and Python dunder/format strings treated as data.
- `test_server_unicode_inputs.py`: Unicode nickname rejection according to the ASCII nickname policy, emoji/combining/RTL/zero-width chat handling, lone-surrogate JSON strings, and Unicode/control strings in non-chat fields.

Current known test-discovered issue with unchanged `server.py`:

- `test_server_python_sanitization_inputs.py` currently exposes that `json.loads(...)` can raise a plain `ValueError` for huge integer literals before the message reaches normal validation. Because this task requested tests only, the server is intentionally not patched here.

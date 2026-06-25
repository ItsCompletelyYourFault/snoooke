# Multiplayer Snake WebSocket Protocol

All messages are JSON objects.

## Transport

The JSON messages below are unchanged whether the socket uses plain WebSocket or TLS. For local development/debug mode the server may listen on `ws://HOST:PORT`. With SSL enabled it listens on `wss://HOST:PORT`.

## Client to server

### Just Play

```json
{"type":"just_play","nickname":"Noodle123"}
```

The server joins a random running joinable game. If no running joinable game exists, it creates a new instant game with 4 bots.

### Create Game

```json
{"type":"create_game","nickname":"Noodle123"}
```

The server creates a new game with a 30 second warm-up countdown and returns a Game-ID.

### Join Game

```json
{"type":"join_game","nickname":"Noodle123","gameId":"A1B2C"}
```

If the Game-ID is invalid, expired or full without kickable bots, the server returns an error.


### Leave Game

```json
{"type":"leave_game"}
```

The server removes the player from the current game and keeps the WebSocket usable for a future join/create request if the client does not close it.

### Rejoin Game client flow

There is no special server-side rejoin message. After a death and a 5 second client-side cooldown, the browser sends `leave_game`, waits for `left_game`, then sends `join_game` again with the same Game-ID and nickname. The new snake starts at length 1 through the existing join logic.

### All-Time Highscore request

```json
{"type":"all_time_high"}
```

This request can be sent before joining a game. The returned board is volatile and exists only during the current `server.py` process lifetime.

### Direction input

```json
{"type":"input","dir":"left","seq":42,"clientTime":1780000000000}
```

Allowed directions: `up`, `down`, `left`, `right`.

### Sprint

```json
{"type":"sprint","seq":7,"dir":"left","clientTime":1780000000000}
```

Sprint is a one-shot request. The server accepts it only for living human players in a running game with length greater than 5. On the next tick, the snake spends 2 length, advances 4 cells in its current server-authoritative direction, and drops 1 food at its tail. The `dir` field is accepted as client telemetry only; the server does not trust it for physics.

### Telemetry

```json
{
  "type":"telemetry",
  "seq":43,
  "dir":"left",
  "length":12,
  "segments":[[10,4],[11,4]],
  "clientTime":1780000000000
}
```

The server stores telemetry for observation and possible anti-cheat analysis. It does not use telemetry for authoritative game physics, movement distance or sprint validation.

### Chat

```json
{"type":"chat","text":"hello noodles"}
```

Length must be 1-255 characters.

## Server to client

### Welcome

```json
{
  "type":"welcome",
  "mode":"create_game",
  "playerId":"s_abc",
  "gameId":"A1B2C",
  "grid":{"w":64,"h":38},
  "maxPlayers":16,
  "maxActiveSnakes":16,
  "phase":"warmup",
  "warmupMs":30000,
  "serverNow":1780000000000,
  "chatHistory":[]
}
```

### State snapshot

```json
{
  "type":"state",
  "gameId":"A1B2C",
  "phase":"running",
  "warmupMs":0,
  "level":"Normal",
  "tickHz":10.0,
  "targetTickHz":10.0,
  "targetFoodCount":3,
  "grid":{"w":64,"h":38},
  "food":[[5,7],[10,11]],
  "scoreboard":[{"rank":1,"id":"s_abc","nickname":"Noodle123","score":12,"color":"#ff5c8a","bot":false}],
  "humanCount":3,
  "activeSnakeCount":7,
  "snakes":[
    {
      "id":"s_abc",
      "nickname":"Noodle123",
      "rawNickname":"Noodle123",
      "bot":false,
      "color":"#ff5c8a",
      "body":[[10,4],[11,4]],
      "dir":"left",
      "length":12,
      "alive":true,
      "leader":true
    }
  ]
}
```

### Chat

```json
{"type":"chat","kind":"player","from":"Noodle123","text":"hello","time":1780000000000}
```

System messages use `kind: "system"`.

### Death

```json
{"type":"you_died","reason":"wall","killerId":null,"killerName":null,"message":"You splatted into a wall."}
```

Reasons include `wall`, `self`, `body` and `head`.

### Round winner

```json
{"type":"round_winner","winnerId":"s_abc","winnerName":"Noodle123","message":"Noodle123 wins this noodle round."}
```

The game remains usable; this is a live high-score arena rather than a hard-ended match.

Death-food drops are represented as ordinary `food` cells in later state snapshots.


### Left game

```json
{"type":"left_game","message":"You left the game."}
```

### All-Time Highscore

```json
{
  "type":"all_time_high",
  "serverNow":1780000000000,
  "scores":[
    {"rank":1,"nickname":"NoodleNinja","length":42,"datetime":"2026-06-25T14:00:00Z"}
  ]
}
```

The server stores `nickname`, `length`, and UTC `datetime` for the top human-player scores in memory only.

### Error

```json
{"type":"error","code":"GAME_NOT_FOUND","message":"Game-ID is invalid or expired."}
```

#!/usr/bin/env node
"use strict";

const assert = require("assert");
const { createHarness } = require("./test_index_harness");

const h = createHarness();
const scoreSocket = h.FakeWebSocket.instances[0];
assert(scoreSocket, "loading the page should open a temporary all-time-high WebSocket");
scoreSocket.open();
assert(scoreSocket.sent.some((m) => m.type === "all_time_high"), "landing page should request the all-time highscore once on load");

scoreSocket.message({
  type: "all_time_high",
  scores: [
    { rank: 1, nickname: "NoodleNinja", length: 42, datetime: "2026-06-25T14:00:00Z" },
    { rank: 2, nickname: "SirNoodle", length: 12, datetime: "2026-06-25T13:00:00Z" },
  ],
});

assert.strictEqual(h.el("allTimeHighList").children.length, 2, "all-time highscore response should render score rows");
assert.strictEqual(h.el("allTimeHighList").children[0].children[1].textContent, "NoodleNinja", "first highscore nickname should be rendered");
assert.strictEqual(h.el("allTimeHighList").children[0].children[2].textContent, "42m", "first highscore length should be rendered");

console.log("PASS index landing all-time highscore regression test");

#!/usr/bin/env node
"use strict";

const assert = require("assert");
const { createHarness, NICKNAME_STORAGE_KEY, fakeEvent } = require("./test_index_harness");

function isValidNickname(value) {
  return /^[A-Za-z0-9]{5,15}$/.test(value);
}

// A valid nickname in browser permanent storage should win over the random default.
{
  const h = createHarness({ localStorage: { [NICKNAME_STORAGE_KEY]: "Snake999" } });
  assert.strictEqual(h.el("nickname").value, "Snake999", "saved nickname should prefill the landing nickname input");
}

// An invalid stored value should be ignored and removed, then the normal valid random fallback is used.
{
  const h = createHarness({ localStorage: { [NICKNAME_STORAGE_KEY]: "bad name<script>" } });
  assert(isValidNickname(h.el("nickname").value), "fallback nickname should still be valid");
  assert.strictEqual(h.storage.getItem(NICKNAME_STORAGE_KEY), null, "invalid stored nickname should be removed");
}

// Random-name selection should become the new remembered nickname.
{
  const h = createHarness({ localStorage: { [NICKNAME_STORAGE_KEY]: "Snake999" } });
  h.el("randomName").click();
  assert(isValidNickname(h.el("nickname").value), "random nickname button should produce a valid nickname");
  assert.strictEqual(h.storage.getItem(NICKNAME_STORAGE_KEY), h.el("nickname").value, "random nickname should be stored");
}

// Typing should sanitize illegal characters and remember the latest valid nickname.
{
  const h = createHarness({ localStorage: { [NICKNAME_STORAGE_KEY]: "Snake999" } });
  h.el("nickname").value = "Bad Name!!!";
  h.el("nickname").dispatchEvent(fakeEvent("input", { target: h.el("nickname") }));
  assert.strictEqual(h.el("nickname").value, "BadName", "nickname input should strip non-alphanumeric characters");
  assert.strictEqual(h.storage.getItem(NICKNAME_STORAGE_KEY), "BadName", "valid typed nickname should be stored");

  h.el("nickname").value = "abc";
  h.el("nickname").dispatchEvent(fakeEvent("input", { target: h.el("nickname") }));
  assert.strictEqual(h.storage.getItem(NICKNAME_STORAGE_KEY), "BadName", "too-short nickname should not overwrite the last valid one");
}

// Starting a game with a valid manually entered nickname should persist it.
{
  const h = createHarness();
  h.el("nickname").value = "Manual777";
  h.el("justPlay").click();
  const ws = h.latestSocket();
  ws.open();
  assert(ws.sent.some((m) => m.type === "just_play" && m.nickname === "Manual777"), "Just Play should send the manual nickname");
  assert.strictEqual(h.storage.getItem(NICKNAME_STORAGE_KEY), "Manual777", "manual nickname should be stored when starting a game");
}

console.log("PASS index nickname storage regression test");

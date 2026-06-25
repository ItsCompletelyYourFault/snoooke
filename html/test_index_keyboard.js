#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const htmlPath = path.join(__dirname, "client", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/);
if (!scriptMatch) throw new Error("Could not find the inline game script in client/index.html");
const script = scriptMatch[1];

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach((name) => this.values.add(name)); }
  remove(...names) { names.forEach((name) => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    if (force === undefined) {
      if (this.values.has(name)) { this.values.delete(name); return false; }
      this.values.add(name); return true;
    }
    if (force) this.values.add(name);
    else this.values.delete(name);
    return Boolean(force);
  }
}

class FakeElement {
  constructor(tagName = "DIV", id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.childNodes = this.children;
    this.parentNode = null;
    this.listeners = {};
    this.classList = new FakeClassList();
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.value = "";
    this.textContent = "";
    this.innerText = "";
    this.hidden = false;
    this.disabled = false;
    this.isContentEditable = false;
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  append(...items) { items.forEach((item) => this.appendChild(item)); }
  remove() {
    if (!this.parentNode) return;
    const list = this.parentNode.children;
    const index = list.indexOf(this);
    if (index >= 0) list.splice(index, 1);
    this.parentNode = null;
  }
  get firstChild() { return this.children[0] || null; }
  set innerHTML(_value) { this.children.length = 0; }
  get innerHTML() { return ""; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  dispatchEvent(event) {
    event.target ||= this;
    event.currentTarget = this;
    for (const handler of this.listeners[event.type] || []) handler(event);
    return !event.defaultPrevented;
  }
  click() { this.dispatchEvent(fakeEvent("click", { target: this })); }
  focus() { fakeDocument.activeElement = this; }
  blur() { if (fakeDocument.activeElement === this) fakeDocument.activeElement = fakeDocument.body; }
  contains(node) {
    let current = node;
    while (current) {
      if (current === this) return true;
      current = current.parentNode;
    }
    return false;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getBoundingClientRect() { return { width: 960, height: 570, left: 0, top: 0 }; }
  getContext() { return fakeCanvasContext; }
}

function fakeEvent(type, extras = {}) {
  return {
    type,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
    ...extras,
  };
}

const fakeCanvasContext = new Proxy({}, {
  get(_target, prop) {
    if (prop === "measureText") return (text) => ({ width: String(text).length * 8 });
    return () => {};
  },
  set() { return true; },
});

const ids = [
  "landing", "gameShell", "nickname", "gameIdInput", "landingError", "joinBox",
  "gameCanvas", "warmupOverlay", "deadOverlay", "countdown", "deathText",
  "chatLog", "chatInput", "scoreList", "allTimeHighList", "toastArea", "gameIdModal",
  "modalGameId", "joystickZone", "sprintButton", "mobileChatToggle", "mobileChatMarker",
  "mobileScoreToggle", "gameIdPill", "levelPill", "speedPill", "playersPill",
  "randomName", "showJoin", "justPlay", "createGame", "joinGame", "chatForm",
  "leaveGame", "rejoinGame", "copyGameId", "modalCopy", "modalClose", "meaningfultips",
];

const elements = new Map();
function el(id, tag = "DIV") {
  if (!elements.has(id)) elements.set(id, new FakeElement(tag, id));
  return elements.get(id);
}

ids.forEach((id) => {
  const tag = id.includes("Input") || id === "nickname" || id === "chatInput" ? "INPUT" :
    id === "gameCanvas" ? "CANVAS" :
    ["randomName", "showJoin", "justPlay", "createGame", "joinGame", "leaveGame", "rejoinGame", "copyGameId", "modalCopy", "modalClose", "sprintButton", "mobileChatToggle", "mobileScoreToggle"].includes(id) ? "BUTTON" : "DIV";
  el(id, tag);
});

const chatSection = new FakeElement("SECTION"); chatSection.classList.add("chat");
const scorePanel = new FakeElement("ASIDE"); scorePanel.classList.add("panel");
const touchButtons = ["up", "left", "down", "right"].map((dir) => { const b = new FakeElement("BUTTON"); b.dataset.dir = dir; return b; });

const fakeDocument = {
  body: new FakeElement("BODY", "body"),
  documentElement: new FakeElement("HTML", "html"),
  activeElement: null,
  listeners: {},
  getElementById(id) { return elements.get(id) || null; },
  createElement(tag) { return new FakeElement(tag); },
  createTextNode(text) { const n = new FakeElement("#text"); n.textContent = String(text); return n; },
  querySelector(selector) {
    if (selector === ".chat") return chatSection;
    if (selector === ".panel") return scorePanel;
    return null;
  },
  querySelectorAll(selector) {
    if (selector === ".touch-pad button") return touchButtons;
    return [];
  },
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
  dispatchEvent(event) {
    event.target ||= this;
    for (const handler of this.listeners[event.type] || []) handler(event);
    return !event.defaultPrevented;
  },
};
fakeDocument.activeElement = fakeDocument.body;

// Minimal containment tree: landing inputs stay in the hidden landing section;
// game controls/chat stay in the game shell.
fakeDocument.body.appendChild(el("landing"));
fakeDocument.body.appendChild(el("gameShell"));
fakeDocument.body.appendChild(el("gameIdModal"));
el("landing").appendChild(el("nickname"));
el("landing").appendChild(el("gameIdInput"));
el("landing").appendChild(el("joinGame"));
el("gameShell").appendChild(el("gameCanvas"));
el("gameShell").appendChild(el("chatInput"));
el("gameShell").appendChild(chatSection);
el("gameShell").appendChild(scorePanel);

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.bufferedAmount = 0;
    this.sent = [];
    this.listeners = {};
    FakeWebSocket.instances.push(this);
  }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  send(message) { this.sent.push(JSON.parse(message)); }
  close() { this.readyState = FakeWebSocket.CLOSED; this.dispatch("close", {}); }
  dispatch(type, event) { for (const handler of this.listeners[type] || []) handler(event); }
  open() { this.readyState = FakeWebSocket.OPEN; this.dispatch("open", {}); }
  message(data) { this.dispatch("message", { data: JSON.stringify(data) }); }
}

const context = {
  console,
  document: fakeDocument,
  window: null,
  location: { protocol: "http:", hostname: "localhost" },
  navigator: { maxTouchPoints: 0, userAgent: "node", platform: "Linux" },
  performance: { now: () => 1000 },
  WebSocket: FakeWebSocket,
  screen: { orientation: { lock: () => Promise.resolve() } },
  setTimeout: () => 1,
  clearTimeout: () => {},
  setInterval: () => 1,
  clearInterval: () => {},
  requestAnimationFrame: () => 1,
};
context.window = context;
context.window.matchMedia = () => ({ matches: false });
context.window.addEventListener = () => {};
context.window.visualViewport = null;
context.window.scrollTo = () => {};
context.navigator.clipboard = { writeText: () => Promise.resolve() };

vm.createContext(context);
vm.runInContext(script, context, { filename: "index.html.inline.js" });

function latestGameSocket() {
  return FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
}

function keydown(target, key, code = key) {
  const event = fakeEvent("keydown", { target, key, code, repeat: false });
  fakeDocument.dispatchEvent(event);
  return event;
}

// Start a join flow while the Game-ID input owns focus, exactly the case that
// used to make desktop controls feel dead until the player clicked the page.
el("nickname").value = "NoodleNinja";
el("gameIdInput").value = "ABCDE";
el("gameIdInput").focus();
el("joinGame").click();
const gameWs = latestGameSocket();
gameWs.open();
assert(gameWs.sent.some((m) => m.type === "join_game" && m.gameId === "ABCDE"), "join_game should be sent");
gameWs.message({ type: "welcome", mode: "join_game", playerId: "s_me", gameId: "ABCDE", grid: { w: 64, h: 38 }, chatHistory: [] });

// Simulate the browser still targeting the hidden Game-ID input: W must steer.
el("gameIdInput").focus();
const moveEvent = keydown(el("gameIdInput"), "w", "KeyW");
assert(moveEvent.defaultPrevented, "movement key should be consumed by the game");
assert(gameWs.sent.some((m) => m.type === "input" && m.dir === "up"), "W from stale Game-ID focus must send input/up");
assert.strictEqual(fakeDocument.activeElement, el("gameCanvas"), "focus should move back to canvas controls");

// Chat input is a real typing field during a game and must not steer.
const sentBeforeChat = gameWs.sent.length;
el("chatInput").focus();
const chatEvent = keydown(el("chatInput"), "a", "KeyA");
assert(!chatEvent.defaultPrevented, "chat typing should not be consumed as movement");
assert.strictEqual(gameWs.sent.length, sentBeforeChat, "chat typing must not send direction input");

// Space from stale landing focus should still reach sprint logic.
el("gameIdInput").focus();
keydown(el("gameIdInput"), " ", "Space");
assert(gameWs.sent.some((m) => m.type === "sprint"), "Space from stale landing focus must send sprint");

console.log("PASS index keyboard focus regression test");

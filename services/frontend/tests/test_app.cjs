"use strict";

// Exercise the unmodified browser script through form/toggle events, using a small DOM and
// fake fetch/timers. No packages, network, browser installation or runtime build step needed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const site = path.join(__dirname, "../site");
const script = fs.readFileSync(path.join(site, "app.js"), "utf8");
const html = fs.readFileSync(path.join(site, "index.html"), "utf8");

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = new Map();
    this.className = "";
    this.style = {};
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.classList = {
      toggle: (name, on) => {
        const classes = new Set(this.className.split(/\s+/).filter(Boolean));
        on ? classes.add(name) : classes.delete(name);
        this.className = [...classes].join(" ");
      },
    };
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return (this._text || "") + this.children.map((c) => c.textContent ?? String(c)).join(""); }
  append(...children) {
    children.forEach((child) => { child.parent = this; this.children.push(child); });
  }
  replaceChildren(...children) { this.children = []; this._text = ""; this.append(...children); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  dispatch(name, extra = {}) { return this.listeners.get(name)?.({preventDefault() {}, ...extra}); }
  requestSubmit() { this.dispatch("submit"); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
  focus() {}
}

function descendants(node, className) {
  return [
    ...(node.className.split(/\s+/).includes(className) ? [node] : []),
    ...node.children.flatMap((child) => descendants(child, className)),
  ];
}

async function until(predicate) {
  for (let i = 0; i < 50; i++) {
    if (predicate()) return;
    await new Promise(setImmediate);
  }
  assert.fail("The browser script did not finish the expected operation");
}

function openPage(replies) {
  const nodes = new Map([...html.matchAll(/id="([^"]+)"/g)].map((m) => [m[1], new Element(m[1])]));
  nodes.get("messages").append(nodes.get("empty"));
  const timers = new Map(), delays = [], calls = [];
  let nextTimer = 0;
  const document = {
    body: new Element("body"), hidden: false,
    getElementById: (id) => nodes.get(id) || null,
    createElement: (tag) => new Element(tag), addEventListener() {},
  };
  const context = {
    document, window: {scrollTo() {}}, location: {search: ""},
    localStorage: {getItem() { return null; }, setItem() {}},
    URLSearchParams, AbortController, performance,
    setTimeout(callback, delay) { delays.push(delay); timers.set(++nextTimer, {callback, delay}); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
    async fetch(url, options) {
      calls.push({url, options});
      const reply = replies.shift();
      assert.ok(reply, `Unexpected request: ${url}`);
      if (reply instanceof Error) throw reply;
      const headers = new Map(Object.entries({"content-type": "application/json", "x-served-by": "frontend-1",
        "x-request-id": "request-header", ...reply.headers}).map(([key, value]) => [key.toLowerCase(), value]));
      return {status: reply.status ?? 200, headers: {get: (key) => headers.get(key.toLowerCase()) ?? null},
        async json() { return reply.body; }};
    },
  };
  vm.runInNewContext(script, context, {filename: "app.js"});
  return {
    nodes, calls, delays,
    async ask(question = "วิชา 2110221 เรียนเกี่ยวกับอะไร") {
      nodes.get("question").value = question;
      nodes.get("composer").requestSubmit();
      await until(() => !nodes.get("send").disabled);
      return nodes.get("messages").children.at(-1);
    },
    async startDemo() {
      nodes.get("demo-toggle").checked = true;
      nodes.get("demo-toggle").dispatch("change");
      await until(() => nodes.get("trace-stats").textContent.includes("failed"));
    },
    async nextTrace() {
      const [id, timer] = [...timers].find(([, timer]) => timer.delay === 2000) || [];
      assert.ok(timer, "Expected another trace check to be scheduled");
      timers.delete(id);
      await timer.callback();
    },
  };
}

const chain = [{service: "api-gateway", pod: "gateway-1"}, {service: "rag-service", pod: "rag-1"},
  {service: "vector-db", pod: "vdb-1"}, {service: "model-service", pod: "model-1"}];
const sources = [
  {n: 1, doc_id: "2110221", doc_title: "Computer Engineering Essentials", page: 1, text: "First passage."},
  {n: 2, doc_id: "2110221", doc_title: "Computer Engineering Essentials", page: 1, text: "Second passage."},
];

test("course answer keeps both citation numbers and expandable passages from the same document", async () => {
  const ui = openPage([{body: {answer: "รายละเอียด [2].", sources, served_by: chain, request_id: "request-123456789"}}]);
  const card = await ui.ask();
  const labels = descendants(card, "source").map((n) => n.textContent);
  assert.equal(labels.length, 2);
  assert.match(labels[0], /^\[1\] 2110221/);
  assert.match(labels[1], /^\[2\] 2110221/);
  assert.ok(labels.every((label) => !label.includes("p.1")));
  assert.equal(descendants(card, "source-passage")[1].textContent, "Second passage.");
  assert.match(descendants(card, "served")[0].textContent, /request-123456789/);
  assert.match(card.textContent, /5 containers/);
  assert.deepEqual(JSON.parse(ui.calls[0].options.body), {question: "วิชา 2110221 เรียนเกี่ยวกับอะไร"});
  assert.ok(ui.delays.includes(110000));
});

test("slide sources retain real page numbers and the source-supplied passage number", async () => {
  const ui = openPage([{body: {answer: "Answer [7].", sources: [{n: 7, doc_id: "slides", doc_title: "Slides", page: 10}]}}]);
  const card = await ui.ask();
  assert.equal(descendants(card, "source")[0].textContent, "[7] Slides · p.10");
});

test("LLM failure still shows passages, all reached pods and the request ID without retrying", async () => {
  const ui = openPage([{status: 502, body: {error: "model_service_unavailable", detail: "model-service: timeout",
    sources, served_by: chain, request_id: "failed-request"}}]);
  const card = await ui.ask();
  assert.match(card.className, /error/);
  assert.match(card.textContent, /Retrieved passages · answer unavailable/);
  assert.equal(descendants(card, "source").length, 2);
  assert.match(descendants(card, "served")[0].textContent, /model-1/);
  assert.match(descendants(card, "served")[0].textContent, /failed-request/);
  assert.match(descendants(card, "served")[0].textContent, /model_service_unavailable/);
  assert.equal(ui.calls.length, 1);
});

test("rate limit keeps Retry-After and request ID from the response header", async () => {
  const ui = openPage([{status: 429, headers: {"retry-after": "7"}, body: {error: "rate_limited"}}]);
  const card = await ui.ask();
  assert.match(card.className, /notice/);
  assert.match(card.textContent, /wait 7 s/);
  assert.match(card.textContent, /request-header/);
});

test("missing sources and malformed answer do not produce a blank success card", async () => {
  const ui = openPage([{body: {answer: "No matching course information."}}, {body: {answer: null}}]);
  const first = await ui.ask();
  assert.match(first.textContent, /No supporting passages were returned/);
  const second = await ui.ask();
  assert.match(second.className, /error/);
});

test("passage content is rendered as text, including HTML from the source documents", async () => {
  const literal = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
  const ui = openPage([{body: {answer: literal, sources: [{...sources[0], text: literal}]}}]);
  const card = await ui.ask();
  assert.equal(descendants(card, "source-passage")[0].textContent, literal);
  assert.ok(!card.children.some((child) => child.tagName === "script"));
});

test("trace shows the failed dependency and clears the message after recovery", async () => {
  const ui = openPage([
    {status: 502, body: {ok: false, detail: "model-service: HTTP 503", served_by: chain.slice(0, 3)}},
    {body: {ok: true, served_by: chain}},
  ]);
  await ui.startDemo();
  assert.equal(ui.nodes.get("trace-detail").textContent, "model-service: HTTP 503");
  assert.equal(ui.nodes.get("trace-detail").hidden, false);
  assert.match(ui.nodes.get("trace-stats").textContent, /0 ok · 1 failed/);
  await ui.nextTrace();
  assert.equal(ui.nodes.get("trace-detail").hidden, true);
  assert.equal(ui.nodes.get("trace-detail").textContent, "");
  assert.match(ui.nodes.get("trace-stats").textContent, /1 ok · 1 failed/);
  assert.ok(ui.delays.includes(10000));
});

test("trace rejects truthy non-boolean ok and clears stale pod names on network failure", async () => {
  const ui = openPage([{body: {ok: "true", served_by: chain}}, new Error("offline")]);
  await ui.startDemo();
  assert.match(ui.nodes.get("trace-stats").textContent, /0 ok · 1 failed/);
  await ui.nextTrace();
  assert.equal(ui.nodes.get("chain-list").children.length, 0);
  assert.equal(ui.nodes.get("trace-detail").textContent, "Can't reach the service");
});

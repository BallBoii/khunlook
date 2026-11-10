/* SDS Course Assistant: chat page and live trace panel. Plain JavaScript, no build step.
 *
 * The page talks only to its own nginx (same origin); nginx forwards /api/* to api-gateway.
 *
 *   POST /api/v1/chat   question -> answer, sources, served_by (gateway, rag, vector-db, model)
 *   GET  /api/v1/trace  cheap check through the same chain, no LLM call (demo panel)
 *
 * The frontend pod is not in the body: nginx adds it as the X-Served-By response header.
 *
 * Demo mode (switch, or open the page with ?demo) adds the `demo` class to <body>, which shows
 * the pod chain under every answer, and starts the live trace panel.
 */
(() => {
  "use strict";

  // =========================================================================================
  // Settings
  // =========================================================================================

  const API = "/api/v1";
  const CHAT_TIMEOUT_MS = 60_000;    // above the gateway's 45 s, so its clearer error arrives first
  const RETRY_DELAY_MS = 2_000;      // wait before the one automatic retry during a failover
  const TRACE_EVERY_MS = 2_000;      // pause between trace checks
  const TRACE_TIMEOUT_MS = 3_000;    // a trace check slower than this counts as failed
  const TRACE_TICKS = 30;            // ~1 minute of history; must match the grid in style.css
  const NEW_POD_HIGHLIGHT_MS = 10_000;

  // Service names as reported in served_by, and the short labels shown on the chips.
  const SHORT = { "frontend": "frontend", "api-gateway": "gateway", "rag-service": "rag",
                  "vector-db": "vector-db", "model-service": "model" };

  const $ = (id) => document.getElementById(id);
  const els = {
    messages: $("messages"), empty: $("empty"), examples: $("examples"), composer: $("composer"),
    question: $("question"), send: $("send"), banner: $("banner"), layout: $("layout"),
    trace: $("trace"), toggle: $("demo-toggle"), ticks: $("ticks"), stats: $("trace-stats"),
    chainList: $("chain-list"), logo: $("logo"),
  };

  // =========================================================================================
  // Helpers
  // =========================================================================================

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /** Create an element. Text is always set as text (never as HTML), so answers can't inject code. */
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    }
    for (const c of children) if (c != null) node.append(c);
    return node;
  }

  /** localStorage that never throws (it can in private mode). Only remembers the demo switch. */
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* ignore */ } },
  };

  function showBanner(text) { els.banner.textContent = text; els.banner.hidden = false; }
  function hideBanner() { els.banner.hidden = true; }
  function scrollToEnd() { window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }); }

  /**
   * One request to the API, with a time limit.
   * Resolves to {status, data, frontendPod, retryAfter, ms}. `data` is null when the reply isn't
   * JSON (e.g. nginx's own error page). Rejects on a network error; on timeout the error's
   * name is "AbortError".
   */
  async function call(path, { method = "GET", body, timeoutMs }) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    const started = performance.now();
    try {
      const res = await fetch(API + path, {
        method, signal: ctrl.signal, cache: "no-store",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
      });
      let data = null;
      if ((res.headers.get("content-type") || "").includes("application/json")) {
        try { data = await res.json(); } catch { data = null; }
      }
      return { status: res.status, data, frontendPod: res.headers.get("X-Served-By"),
               retryAfter: res.headers.get("Retry-After"), ms: performance.now() - started };
    } finally {
      clearTimeout(timer);
    }
  }

  // =========================================================================================
  // Rendering
  // =========================================================================================

  /** CSS class that gives each service its CI colour (see .svc-* in style.css). */
  function serviceClass(name) { return name in SHORT ? `svc-${name}` : "svc-other"; }

  /** frontend pod (from the header) + the chain from the body. */
  function fullChain(frontendPod, servedBy) {
    const chain = Array.isArray(servedBy) ? servedBy.slice() : [];
    if (frontendPod) chain.unshift({ service: "frontend", pod: frontendPod });
    return chain;
  }

  /** The coloured chips: frontend › gateway › rag › vector-db › model. Hover shows the pod name. */
  function renderChain(chain) {
    const wrap = el("div", { class: "chain" });
    chain.forEach((hop, i) => {
      if (i) wrap.append(el("span", { class: "sep", text: "›", "aria-hidden": "true" }));
      wrap.append(el("span", { class: `chip ${serviceClass(hop.service)}`, title: hop.pod,
                               text: SHORT[hop.service] || hop.service }));
    });
    return wrap;
  }

  function addUserMessage(text) {
    els.empty?.remove();
    els.messages.append(el("div", { class: "msg-user", text }));
  }

  /** Placeholder card shown while waiting; filled later by fillAnswer or fillMessage. */
  function addPendingCard() {
    const card = el("article", { class: "msg-answer pending" },
      el("div", { class: "text" }, el("p", { text: "Searching the slides…" })),
      el("div", { class: "progress", "aria-hidden": "true" }));
    els.messages.append(card);
    scrollToEnd();
    return card;
  }

  /** Answer text, source chips (or "not covered"), then the demo-only pod chain and timing. */
  function fillAnswer(card, result) {
    const { data, frontendPod, ms } = result;
    card.className = "msg-answer";
    card.replaceChildren();

    const text = el("div", { class: "text" });
    String(data.answer || "").split(/\n{2,}/).forEach((para) => text.append(el("p", { text: para })));
    card.append(text);

    const sources = Array.isArray(data.sources) ? data.sources : [];
    if (sources.length) {
      const labels = new Set(sources.map((s) =>      // e.g. "SDS Final Project · p.10", no duplicates
        [s.doc_title || s.doc_id || "Source", s.page != null ? `p.${s.page}` : null].filter(Boolean).join(" · ")));
      card.append(el("div", { class: "label", text: "Sources" }),
                  el("div", { class: "sources" }, ...[...labels].map((l) => el("span", { class: "source", text: l }))));
    } else {
      card.append(el("p", { class: "not-covered", text: "Not covered in the course slides" }));
    }

    // Shown only in demo mode (body.demo .served in style.css).
    const served = el("div", { class: "served" });
    const chain = fullChain(frontendPod, data.served_by);
    if (chain.length) {
      served.append(el("div", { class: "label", text: `Served by · ${chain.length} containers` }),
                    renderChain(chain),
                    el("div", { class: "pods", text: chain.map((h) => h.pod).join(" › ") }));
    }
    served.append(el("div", { class: "meta",
      text: `${(ms / 1000).toFixed(1)} s · request ${String(data.request_id || "").slice(0, 8)}` }));
    card.append(served);
  }

  /** An error or notice in place of an answer. `kind` is "error" (red) or "notice" (grey). */
  function fillMessage(card, kind, message, frontendPod) {
    card.className = `msg-answer ${kind}`;
    card.replaceChildren(el("div", { class: "text" }, el("p", { text: message })));
    if (frontendPod) card.append(el("div", { class: "served" }, el("div", { class: "pods", text: `frontend ${frontendPod}` })));
  }

  // =========================================================================================
  // Chat
  // =========================================================================================

  /**
   * Does this failure look like a failover in progress? Then one automatic retry is worth it.
   * Not for timeouts (that would double a long wait) or for limits (429, busy).
   */
  function isRecoverable(result) {
    if (result.networkError) return true;                         // connection dropped
    if (!result.data && (result.status === 502 || result.status === 504)) return true;  // nginx couldn't reach a gateway
    return result.status === 503 && result.data?.error === "upstream_unavailable";       // gateway couldn't reach rag-service
  }

  function errorText(result) {
    if (result.timedOut) return "The answer took too long, please try again";
    if (result.networkError) return "Can't reach the service, please try again";
    const d = result.data || {};
    switch (result.status) {
      case 400: return `Invalid question: ${d.detail || "please rephrase"}`;
      case 429: return `Too many questions, please wait ${result.retryAfter || "a few"} s`;
      case 503: return d.error === "busy"
        ? "Busy with other questions, please try again shortly"
        : "The service is recovering, please try again";
      case 504: return "The answer took too long, please try again";
      default:  return `Something went wrong (${result.status})${d.detail ? `: ${d.detail}` : ""}`;
    }
  }

  /** Ask once; on a recoverable failure show the banner, wait 2 s and ask once more. */
  async function ask(question) {
    addUserMessage(question);
    const card = addPendingCard();
    setBusy(true);
    let result;
    try {
      for (let attempt = 1; attempt <= 2; attempt++) {
        try {
          result = await call("/chat", { method: "POST", body: { question }, timeoutMs: CHAT_TIMEOUT_MS });
        } catch (e) {
          result = e.name === "AbortError" ? { timedOut: true } : { networkError: true };
        }
        const ok = result.status === 200 && result.data;
        if (ok || attempt === 2 || !isRecoverable(result)) break;
        showBanner("Recovering, retrying…");
        await sleep(RETRY_DELAY_MS);
      }
    } finally {
      hideBanner();
      setBusy(false);
    }

    if (result.status === 200 && result.data) fillAnswer(card, result);
    else fillMessage(card, result.status === 429 || result.data?.error === "busy" ? "notice" : "error",
                     errorText(result), result.frontendPod);
    scrollToEnd();
  }

  /** Disable the input while waiting; give focus back to the question box afterwards. */
  function setBusy(busy) {
    els.send.disabled = busy;
    els.question.disabled = busy;
    if (!busy) els.question.focus();
  }

  /** Grow the question box with its text, up to 160 px. */
  function autosize() {
    els.question.style.height = "auto";
    els.question.style.height = `${Math.min(els.question.scrollHeight, 160)}px`;
  }

  els.composer.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = els.question.value.trim();
    if (!q || els.send.disabled) return;
    els.question.value = "";
    autosize();
    ask(q);
  });
  els.question.addEventListener("keydown", (e) => {   // Enter sends, Shift+Enter adds a line
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); els.composer.requestSubmit(); }
  });
  els.question.addEventListener("input", autosize);
  els.examples?.addEventListener("click", (e) => {    // example buttons ask their own text
    const b = e.target.closest("button");
    if (b) { els.question.value = b.textContent; els.composer.requestSubmit(); }
  });
  if (els.logo && els.logo.complete && els.logo.naturalWidth === 0) els.logo.remove();   // logo file missing

  // =========================================================================================
  // Live trace panel (demo mode)
  //   One cheap check at a time, 2 s apart. Each result is a green or red tick. "Latest chain"
  //   lists the pods that served the last check. Pods seen in the first 10 s are the starting
  //   set (both replicas of each service show up within a few checks); a pod name first seen
  //   after that is highlighted for 10 s, which is how a replacement pod shows up after a Pi
  //   is pulled.
  // =========================================================================================

  const trace = { running: false, timer: null, results: [], ok: 0, fail: 0,
                  firstSeen: new Map(), startedAt: 0 };

  async function traceOnce() {
    let r = null;
    try { r = await call("/trace", { timeoutMs: TRACE_TIMEOUT_MS }); } catch { r = null; }
    const ok = !!(r && r.status === 200 && r.data?.ok);

    trace.results.push({ ok, ms: r?.ms });
    if (trace.results.length > TRACE_TICKS) trace.results.shift();
    ok ? trace.ok++ : trace.fail++;

    renderTicks();
    const chain = r ? fullChain(r.frontendPod, r.data?.served_by) : [];
    if (chain.length) renderTraceChain(chain);
    els.stats.textContent = `${trace.ok} ok · ${trace.fail} failed` + (ok ? ` · last ${Math.round(r.ms)} ms` : "");
  }

  function renderTicks() {
    const missing = TRACE_TICKS - trace.results.length;   // grey placeholders before the first results
    const cells = [];
    for (let i = 0; i < TRACE_TICKS; i++) {
      const res = i < missing ? null : trace.results[i - missing];
      cells.push(el("span", { class: `tick ${res ? (res.ok ? "ok" : "fail") : ""}`,
                              title: res ? (res.ok ? `ok ${Math.round(res.ms || 0)} ms` : "failed") : "" }));
    }
    els.ticks.replaceChildren(...cells);
  }

  function renderTraceChain(chain) {
    const now = Date.now();
    const warmingUp = now - trace.startedAt < NEW_POD_HIGHLIGHT_MS;   // still collecting the starting set
    for (const hop of chain) if (!trace.firstSeen.has(hop.pod)) trace.firstSeen.set(hop.pod, warmingUp ? 0 : now);
    els.chainList.replaceChildren(...chain.map((hop) => {
      const isNew = now - trace.firstSeen.get(hop.pod) < NEW_POD_HIGHLIGHT_MS;
      return el("li", { class: `${serviceClass(hop.service)}${isNew ? " new" : ""}`, title: hop.service },
        el("span", { class: "dot", "aria-hidden": "true" }), el("span", { class: "pod", text: hop.pod }));
    }));
  }

  /** Start or stop the checks. The next check starts 2 s after the previous one finished. */
  function polling(on) {
    trace.running = on;
    clearTimeout(trace.timer);
    if (!on) return;
    const loop = async () => {
      await traceOnce();
      if (trace.running) trace.timer = setTimeout(loop, TRACE_EVERY_MS);
    };
    loop();
  }

  function setDemoMode(on) {
    els.trace.hidden = !on;
    els.layout.classList.toggle("with-trace", on);
    document.body.classList.toggle("demo", on);   // reveals the pod chain under every answer
    store.set("demoMode", on ? "1" : "0");
    if (on) {
      if (!trace.startedAt) trace.startedAt = Date.now();
      renderTicks();
    }
    polling(on && !document.hidden);
  }

  els.toggle.addEventListener("change", () => setDemoMode(els.toggle.checked));
  // Pause the checks while the tab is hidden or minimised; resume when it's visible again.
  document.addEventListener("visibilitychange", () => {
    if (els.toggle.checked && trace.running === document.hidden) polling(!document.hidden);
  });

  // =========================================================================================
  // Start
  // =========================================================================================

  els.toggle.checked = store.get("demoMode") === "1" || new URLSearchParams(location.search).has("demo");
  setDemoMode(els.toggle.checked);
  els.question.focus();
})();

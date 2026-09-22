/* psd.ai installer frontend.
 *
 * Talks to the Rust backend through the Tauri global (withGlobalTauri):
 *   invoke("get_state")            -> full Ui snapshot
 *   invoke("send_action", {...})   -> retry / skip / launch / stop / quit
 *   listen("state", ...)           -> the backend pushes a fresh snapshot
 *                                     (throttled to ~7 Hz) on every change.
 * Rendering is a pure function of the snapshot: render(state) rebuilds the
 * dynamic parts of the DOM. No build step, no npm, no external resources.
 */
(function () {
  "use strict";

  var lastState = null;
  var SEQ_IDS = ["venv", "deps", "extras", "stt", "setup", "build"];

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function act(action, step) {
    window.__TAURI__.core.invoke("send_action", {
      action: action,
      step: step === undefined ? null : step,
    }).catch(function (e) { console.error("action failed:", e); });
  }

  function button(label, cls, action, step) {
    var b = el("button", cls, label);
    b.addEventListener("click", function () { act(action, step); });
    return b;
  }

  // ── banner + overall progress ────────────────────────────────────
  function setupProgress(st) {
    var seq = st.steps.filter(function (s) { return SEQ_IDS.indexOf(s.id) >= 0; });
    if (!seq.length) return { pct: 0, indet: false };
    var done = 0;
    seq.forEach(function (s) {
      if (s.state === "done" || s.state === "skipped" || s.state === "warn") done += 1;
      else if (s.state === "running") done += Math.max(0, Math.min(1, st.step_pct || 0));
    });
    var running = seq.some(function (s) { return s.state === "running"; });
    var pct = Math.round((done / seq.length) * 100);
    // pip/npm phases often cannot report a percentage - show motion honestly
    var indet = running && (!st.step_pct || st.step_pct <= 0);
    return { pct: pct, indet: indet };
  }

  function renderBanner(st) {
    var banner = $("banner");
    banner.className = "banner " + st.phase;
    $("headline").textContent = st.headline;
    $("activity").textContent = st.activity || "";
    var pill = $("phase-pill");
    pill.className = "pill " + st.phase;
    pill.textContent = (st.phase || "").replace("_", " ");

    var p = setupProgress(st);
    var bar = $("setup-bar");
    bar.className = "bar-inner" + (p.indet ? " indet" : "");
    bar.style.width = p.pct + "%";
    $("setup-pct").textContent =
      st.phase === "ready" || st.phase === "running_app" || st.phase === "closed"
        ? "" : p.pct + "%";
  }

  // ── step timeline ────────────────────────────────────────────────
  function renderSteps(st) {
    var host = $("steps");
    host.innerHTML = "";
    st.steps.forEach(function (s) {
      var row = el("div", "step " + s.state);
      row.appendChild(el("span", "dot"));
      var body = el("div");
      body.appendChild(el("span", "label", s.label));
      if (s.msg) body.appendChild(el("span", "meta", s.msg));
      if (s.state === "running" && st.step_id === s.id && st.step_pct > 0) {
        var mini = el("div", "mini");
        var fill = document.createElement("i");
        fill.style.width = Math.round(st.step_pct * 100) + "%";
        mini.appendChild(fill);
        body.appendChild(mini);
      }
      row.appendChild(body);
      host.appendChild(row);
    });
  }

  // ── model group card ─────────────────────────────────────────────
  function renderModels(st) {
    var m = st.models;
    var card = $("models-card");
    var active = m.state !== "idle";
    if (!active) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");

    var chip = $("models-chip");
    chip.className = "chip " + m.state;
    chip.textContent = m.state;

    $("dl-pct").textContent = m.state === "downloading" ? m.pct.toFixed(1) + "%" : "";
    $("dl-detail").textContent = m.detail || "";
    $("dl-file").textContent = m.file || "";
    var bar = $("dl-bar");
    bar.className = "bar-inner" + (m.state === "downloading" && m.pct <= 0 ? " indet" : "");
    bar.style.width = (m.state === "ready" ? 100 : m.pct) + "%";

    var host = $("models");
    host.innerHTML = "";
    (m.items || []).forEach(function (item) {
      var box = el("div", "model " + item.state);
      box.appendChild(el("span", "tag " + item.state, item.state));
      box.appendChild(el("div", "name", item.label));
      box.appendChild(el("div", "sub", ":" + item.port));
      host.appendChild(box);
    });
    if (m.error) host.appendChild(el("div", "dl-file", m.error));

    var actions = $("models-actions");
    actions.innerHTML = "";
    if (m.state === "failed" || m.state === "timeout") {
      actions.appendChild(button("Retry models", "small", "retry", "models"));
    }
  }

  // ── faults ───────────────────────────────────────────────────────
  function renderFaults(st) {
    var card = $("faults-card");
    if (!st.errors || !st.errors.length) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    var host = $("faults");
    host.innerHTML = "";
    // newest first: the fault that just happened is the one to read
    st.errors.slice().reverse().forEach(function (e) {
      var box = el("div", "fault " + (e.kind === "warn" ? "warn" : ""));
      var head = el("div", "fhead");
      head.appendChild(el("span", "fstep", e.step));
      head.appendChild(el("span", "fcode",
        (e.ts ? e.ts + "  " : "") + (e.code ? "exit " + e.code : "")));
      box.appendChild(head);
      box.appendChild(el("div", "fmsg", e.msg));
      if (e.hint) box.appendChild(el("div", "fhint", e.hint));
      if (e.lines && e.lines.length) {
        var det = document.createElement("details");
        var sum = el("summary", "", "output (" + e.lines.length + " lines)");
        det.appendChild(sum);
        var pre = el("pre", "", e.lines.join("\n"));
        det.appendChild(pre);
        box.appendChild(det);
      }
      // the fatal step gets its buttons right on the card too
      if (st.phase === "failed" && st.fatal_step &&
          e.kind !== "warn" && st.steps.some(function (s) {
            return s.id === st.fatal_step && s.label === e.step;
          })) {
        var btns = el("div", "fbtns");
        btns.appendChild(button("Retry step", "small primary", "retry", st.fatal_step));
        btns.appendChild(button("Skip step", "small", "skip", st.fatal_step));
        box.appendChild(btns);
      }
      host.appendChild(box);
    });
  }

  // ── log console ──────────────────────────────────────────────────
  function lineClass(t) {
    var low = t.toLowerCase();
    if (/(error|traceback|fatal|panic)/.test(low)) return "l-err";
    if (/(warn|\[warn\])/.test(low)) return "l-warn";
    if (/(==>|\[ok\]|ready|successfully|finished)/.test(low)) return "l-ok";
    return "";
  }

  function renderLog(st) {
    var host = $("console");
    var atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 40;
    host.innerHTML = "";
    (st.log || []).forEach(function (t) {
      host.appendChild(el("div", lineClass(t), t));
    });
    if ($("autoscroll").checked && atBottom) host.scrollTop = host.scrollHeight;
  }

  // ── footer actions per phase ─────────────────────────────────────
  function renderActions(st) {
    var host = $("actions");
    host.innerHTML = "";
    if (st.app_url) host.appendChild(el("span", "url", st.app_url));
    host.appendChild(el("span", "spacer"));

    if (st.phase === "failed") {
      host.appendChild(button("Retry step", "primary", "retry", st.fatal_step));
      host.appendChild(button("Skip step", "", "skip", st.fatal_step));
      host.appendChild(button("Quit", "danger", "quit"));
    } else if (st.phase === "ready") {
      host.appendChild(button("Launch psd.ai", "primary", "launch"));
      host.appendChild(button("Quit", "danger", "quit"));
    } else if (st.phase === "running_app") {
      host.appendChild(button("Stop psd.ai", "danger", "stop"));
      host.appendChild(button("Quit", "danger", "quit"));
    } else if (st.phase === "closed") {
      host.appendChild(button("Launch psd.ai again", "primary", "launch"));
      host.appendChild(button("Quit", "danger", "quit"));
    } else {
      host.appendChild(button("Quit", "danger", "quit"));
    }
  }

  function render(st) {
    lastState = st;
    document.title =
      (st.phase === "failed" ? "\u2716 " :
       st.phase === "ready" || st.phase === "running_app" ? "\u2714 " : "\u2026 ") +
      "psd.ai Installer";
    renderBanner(st);
    renderSteps(st);
    renderModels(st);
    renderFaults(st);
    renderLog(st);
    renderActions(st);
  }

  function boot() {
    if (!window.__TAURI__) {
      $("headline").textContent =
        "This page must run inside the psd.ai installer window (Tauri IPC is missing).";
      return;
    }
    window.__TAURI__.event.listen("state", function (e) { render(e.payload); });
    window.__TAURI__.core.invoke("get_state").then(render).catch(function (e) {
      console.error("get_state failed:", e);
    });
    $("autoscroll").addEventListener("change", function () {
      if (this.checked && lastState) renderLog(lastState);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

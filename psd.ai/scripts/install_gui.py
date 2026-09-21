#!/usr/bin/env python3
"""Graphical install dashboard for run.sh (Fedora/Linux, stdlib only).

run.sh is a terminal script, but watching a first-run install scroll by in a
terminal - dnf, pip, a multi-GB model download, a Rust build - is exactly
where people lose track of what is happening and what broke. This module
serves a small web dashboard on 127.0.0.1 that run.sh opens in the browser:

  * a step timeline (system packages -> python -> deps -> models -> app)
    fed by JSON-lines markers that run.sh appends to logs/install-steps.jsonl
  * the live model download as a real progress bar (percent + GB), parsed
    from the bootstrap's progress lines in logs/local-model.log
  * model cards for the local group once it is serving (label, quant, port)
  * an error panel: every error-looking line from both logs, deduped
  * a log console with the tails of logs/run.log and logs/local-model.log

Design rules:

  * stdlib only - this must run on the SYSTEM python3 before the venv even
    exists (run.sh starts it before the dnf step so the dashboard covers
    the whole install).
  * read-only - it never writes to the logs it tails, only its own port/pid
    files, so two runs cannot corrupt each other.
  * best effort - if this server cannot start, run.sh continues headless in
    the terminal exactly as before. The dashboard is an overlay, never a
    dependency.
  * loopback only - binds 127.0.0.1; the page polls same-origin /api/status.

The server outlives run.sh by a grace period (--grace, default 10 min) so a
failed install stays readable in the browser instead of vanishing with the
terminal, then exits by itself.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_NAME = "psd.ai"

# Canonical install steps, in run.sh order. Markers with unknown ids are
# appended after these, in first-seen order.
STEP_ORDER: List[Tuple[str, str]] = [
    ("system", "System packages (dnf)"),
    ("python", "Python 3.11+"),
    ("venv", "Virtual environment"),
    ("deps", "Python dependencies"),
    ("voice", "Voice & PC-control extras"),
    ("setup", "First-time setup (folders, database)"),
    ("models", "Local model group (download + serve)"),
    ("desktop", "Desktop app (npm + Rust build)"),
    ("launch", "Launch psd.ai"),
]
STEP_LABELS = dict(STEP_ORDER)

# Terminal colour codes - the logs are written for a tty, the page is not.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")

# Bootstrap download progress, e.g.
#   "      Qwen3.5-9B-Q4_K_M.gguf:  42.1%  5.62/13.40 GB"
_DL_RE = re.compile(
    r"^\s*(?P<file>\S+\.gguf\S*):\s+(?P<pct>\d+(?:\.\d+)?)%\s+"
    r"(?P<have>[\d.]+)/(?P<total>[\d.]+)\s*GB"
)

# "  ==> Starting Qwen3.5 9B (chat) on port 8181 ..."
_STARTING_RE = re.compile(r"Starting (?P<label>.+?) on port (?P<port>\d+)")

# "  ==> Local model group ready: 3 models on ports 8181, 8182, 8183"
_GROUP_READY_RE = re.compile(r"Local model group ready:\s*(\d+)\s*models")

_ERR_RE = re.compile(
    r"(?i)\berror\b|\bfailed\b|\bfatal\b|traceback \(most recent"
    r"|command not found|no space left|permission denied|\bpanic\b"
    r"|segmentation fault|core dumped"
)
# Lines that mention failure machinery without being failures.
_BENIGN_RE = re.compile(
    r"(?i)0 failed|local_model_failed|fail_file|failed\.txt"
    r"|failure modes|reported a failure:|\[warn\] port \d+ is busy"
    r"|what to do when|error handling|--wait-ready"
)

MAX_LOG_LINES = 220
MAX_ERRORS = 12
TAIL_BYTES = 512 * 1024


def strip_ansi(text: str) -> str:
    """Remove terminal colour/cursor codes so log lines render as plain text."""
    return _ANSI_RE.sub("", text)


def tail_lines(path: Path, limit: int = MAX_LOG_LINES) -> List[str]:
    """Last non-empty lines of a log file, ANSI-stripped. Missing file -> []."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [strip_ansi(ln.rstrip()) for ln in chunk.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    return lines[-limit:]


def read_step_markers(path: Path) -> List[Dict[str, Any]]:
    """Parse logs/install-steps.jsonl, tolerating truncated/garbage lines."""
    markers: List[Dict[str, Any]] = []
    for raw in tail_lines(path, limit=500):
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("step"):
            markers.append(obj)
    return markers


def aggregate_steps(markers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Latest marker per step, rendered in canonical run.sh order."""
    latest: Dict[str, Dict[str, Any]] = {}
    for m in markers:
        step = str(m.get("step"))
        if step == "run":
            continue  # run lifecycle, not a timeline step
        latest[step] = {
            "id": step,
            "label": STEP_LABELS.get(step, step),
            "state": str(m.get("state") or "running"),
            "msg": str(m.get("msg") or ""),
            "ts": str(m.get("ts") or ""),
        }
    steps = [latest[sid] if sid in latest
             else {"id": sid, "label": label, "state": "pending", "msg": "", "ts": ""}
             for sid, label in STEP_ORDER]
    seen = set(STEP_LABELS)
    for step, payload in latest.items():
        if step not in seen:
            steps.append(payload)
    return steps


def parse_download(lines: List[str]) -> Optional[Dict[str, Any]]:
    """Most recent download-progress line -> {file, pct, have_gb, total_gb}."""
    for line in reversed(lines):
        m = _DL_RE.match(line)
        if m:
            return {
                "file": m.group("file"),
                "pct": min(100.0, float(m.group("pct"))),
                "have_gb": float(m.group("have")),
                "total_gb": float(m.group("total")),
            }
    return None


def parse_model_activity(lines: List[str]) -> Dict[str, Any]:
    """Which models the bootstrap announced, and whether the group is ready."""
    planned: List[Dict[str, Any]] = []
    seen_labels = set()
    group_ready: Optional[int] = None
    for line in lines:
        m = _STARTING_RE.search(line)
        if m and m.group("label") not in seen_labels:
            seen_labels.add(m.group("label"))
            planned.append({"label": m.group("label"), "port": int(m.group("port"))})
        m = _GROUP_READY_RE.search(line)
        if m:
            group_ready = int(m.group(1))
    return {"planned": planned, "group_ready": group_ready}


def collect_errors(lines: List[str], fail_text: str = "") -> List[str]:
    """Error-looking log lines + the bootstrap's failure report, deduped."""
    out: List[str] = []
    seen = set()

    def add(text: str) -> None:
        text = text.strip()[:240]
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            out.append(text)

    for ln in fail_text.splitlines():
        if ln.strip():
            add(ln)
    for ln in lines:
        if _ERR_RE.search(ln) and not _BENIGN_RE.search(ln):
            add(ln)
    return out[:MAX_ERRORS]


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def default_runtime_dir() -> Path:
    """Same resolution as scripts/local_llama.py (env override first)."""
    env = os.getenv("PSD_AI_RUNTIME_DIR")
    if env:
        return Path(env)
    base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "psd.ai" / "runtime"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def model_cards(group: Optional[Dict[str, Any]],
                activity: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Ready models (from the group state) merged with announced ones."""
    cards: List[Dict[str, Any]] = []
    if group and isinstance(group.get("models"), list):
        for m in group["models"]:
            if not isinstance(m, dict):
                continue
            cards.append({
                "label": str(m.get("label") or m.get("model_id") or "model"),
                "quant": str(m.get("quant") or ""),
                "port": m.get("port"),
                "size_gb": m.get("file_size_gb"),
                "gpu_layers": m.get("gpu_layers"),
                "vision": bool(m.get("vision")),
                "state": "ready",
            })
    ready_labels = {c["label"] for c in cards}
    for p in activity.get("planned", []):
        if p["label"] not in ready_labels:
            cards.append({
                "label": p["label"], "quant": "", "port": p["port"],
                "size_gb": None, "gpu_layers": None, "vision": False,
                "state": "starting",
            })
    return cards


def build_status(log_dir: Path, runtime_dir: Optional[Path] = None,
                 run_pid: Optional[int] = None) -> Dict[str, Any]:
    """Everything the dashboard page needs, in one JSON-ready dict."""
    runtime_dir = runtime_dir or default_runtime_dir()
    steps = aggregate_steps(read_step_markers(log_dir / "install-steps.jsonl"))
    run_lines = tail_lines(log_dir / "run.log")
    model_lines = tail_lines(log_dir / "local-model.log")

    group = read_json(runtime_dir / "local_model_group.json")
    activity = parse_model_activity(model_lines)
    download = None if group else parse_download(model_lines)

    fail_text = ""
    try:
        fail_text = (runtime_dir / "local_model_failed.txt").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        pass

    errors = collect_errors(run_lines + model_lines, fail_text)
    ready_file = runtime_dir / "local_model.json"

    run_alive = pid_alive(run_pid) if run_pid else None
    run_marker = [m for m in read_step_markers(log_dir / "install-steps.jsonl")
                  if m.get("step") == "run"]
    run_exited = run_alive is False or any(
        m.get("state") == "exited" for m in run_marker)
    run_failed = any(m.get("state") == "failed" for m in run_marker)

    if any(s["state"] == "failed" for s in steps) or fail_text.strip() or run_failed:
        overall = "failed"
    elif run_exited:
        overall = "finished"
    elif ready_file.exists() or group:
        overall = "ready"
    else:
        overall = "running"

    active = next((s for s in steps if s["state"] == "running"), None)
    headline = ""
    if overall == "failed":
        headline = errors[0] if errors else "Install failed - see the error panel."
    elif overall == "finished":
        headline = "psd.ai install finished - this dashboard closes shortly."
    elif overall == "ready":
        headline = "psd.ai is ready."
    elif download:
        headline = (f"Downloading {Path(download['file']).name} "
                    f"- {download['pct']:.0f}%")
    elif active:
        headline = f"{active['label']} - {active['msg']}".rstrip(" -")
    else:
        headline = "Installing psd.ai..."

    return {
        "app": APP_NAME,
        "overall": overall,
        "headline": headline,
        "steps": steps,
        "download": download,
        "models": model_cards(group, activity),
        "model_group": {
            "count": (group or {}).get("count"),
            "profile": (group or {}).get("profile"),
            "ports": (group or {}).get("ports"),
        } if group else None,
        "errors": errors,
        "failed_text": fail_text.strip()[:2000],
        "log": {"run": run_lines[-MAX_LOG_LINES:],
                "model": model_lines[-MAX_LOG_LINES:]},
        "run_alive": run_alive,
        "ts": time.time(),
    }


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>psd.ai - install dashboard</title>
<link rel="icon" href="data:,">
<style>
:root {
  --bg: #0b0e17; --panel: #121728; --panel2: #0e1322; --line: #232b45;
  --text: #e8ecf8; --dim: #8b93b0; --accent: #7c8cff; --accent2: #a78bfa;
  --ok: #34d399; --warn: #fbbf24; --err: #f87171; --run: #60a5fa;
  --mono: ui-monospace, "Cascadia Code", "JetBrains Mono", Menlo, monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: radial-gradient(1200px 600px at 70% -10%, #1a2140 0%, var(--bg) 55%);
  color: var(--text); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  min-height: 100vh; padding: 28px 20px 60px;
}
.wrap { max-width: 1180px; margin: 0 auto; }
header { display: flex; align-items: center; gap: 14px; margin-bottom: 20px; }
.logo {
  width: 44px; height: 44px; border-radius: 12px; flex: 0 0 auto;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  display: grid; place-items: center; font-weight: 800; font-size: 20px; color: #0b0e17;
}
h1 { font-size: 20px; font-weight: 700; }
h1 small { display: block; font-size: 12.5px; color: var(--dim); font-weight: 500; }
#banner {
  border-radius: 14px; padding: 16px 20px; margin-bottom: 20px;
  display: flex; align-items: center; gap: 14px; font-size: 16px; font-weight: 600;
  background: var(--panel); border: 1px solid var(--line);
}
#banner .dot { width: 12px; height: 12px; border-radius: 50%; flex: 0 0 auto; background: var(--run); }
#banner.running { border-color: #2b3a6b; }
#banner.running .dot { animation: pulse 1.2s infinite; }
#banner.ready { border-color: #1d4c3c; } #banner.ready .dot { background: var(--ok); }
#banner.finished { border-color: #1d4c3c; } #banner.finished .dot { background: var(--ok); }
#banner.failed { border-color: #5c2330; background: #1a1020; } #banner.failed .dot { background: var(--err); }
#banner .msg { word-break: break-word; }
@keyframes pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(96,165,250,.55);} 50% { box-shadow: 0 0 0 7px rgba(96,165,250,0);} }
.grid { display: grid; grid-template-columns: 380px 1fr; gap: 18px; align-items: start; }
@media (max-width: 940px) { .grid { grid-template-columns: 1fr; } }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; margin-bottom: 18px; }
.card h2 { font-size: 12px; letter-spacing: .12em; text-transform: uppercase; color: var(--dim); margin-bottom: 14px; }
.step { display: flex; gap: 12px; padding: 7px 0; align-items: baseline; }
.step .dot { width: 11px; height: 11px; border-radius: 50%; flex: 0 0 auto; background: #2a3352; transform: translateY(1px); }
.step.running .dot { background: var(--run); animation: pulse 1.2s infinite; }
.step.done .dot { background: var(--ok); }
.step.warn .dot { background: var(--warn); }
.step.failed .dot { background: var(--err); }
.step.skipped .dot { background: #4b5578; }
.step .label { font-weight: 600; }
.step.pending .label { color: var(--dim); font-weight: 500; }
.step .meta { display: block; font-size: 12.5px; color: var(--dim); word-break: break-word; }
.step.failed .meta { color: var(--err); }
.bar-outer { background: var(--panel2); border: 1px solid var(--line); border-radius: 10px; height: 14px; overflow: hidden; margin: 10px 0 6px; }
.bar-inner { height: 100%; width: 0%; border-radius: 10px 0 0 10px;
  background: linear-gradient(90deg, var(--accent), var(--accent2)); transition: width .8s ease; }
.dl-name { font-family: var(--mono); font-size: 12.5px; color: var(--dim); word-break: break-all; }
.dl-pct { font-size: 22px; font-weight: 700; }
.models { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px; }
.model { background: var(--panel2); border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.model .name { font-weight: 600; font-size: 13.5px; }
.model .sub { font-size: 12px; color: var(--dim); font-family: var(--mono); }
.model .tag { float: right; font-size: 10.5px; padding: 2px 8px; border-radius: 99px; font-weight: 700; }
.tag.ready { background: #123f31; color: var(--ok); }
.tag.starting { background: #1c2f52; color: var(--run); }
.errs { display: flex; flex-direction: column; gap: 8px; }
.err {
  background: #1d1220; border: 1px solid #4d2233; color: #fda4af;
  font-family: var(--mono); font-size: 12.5px; border-radius: 10px; padding: 9px 12px; word-break: break-word;
}
.tabs { display: flex; gap: 8px; margin-bottom: 10px; align-items: center; }
.tab {
  background: var(--panel2); border: 1px solid var(--line); color: var(--dim);
  padding: 5px 14px; border-radius: 99px; font-size: 12.5px; font-weight: 600; cursor: pointer;
}
.tab.on { color: var(--text); border-color: var(--accent); }
.tabs .spacer { flex: 1; }
.tabs label { font-size: 12px; color: var(--dim); display: flex; gap: 6px; align-items: center; cursor: pointer; }
#console {
  background: #07090f; border: 1px solid var(--line); border-radius: 10px;
  font-family: var(--mono); font-size: 12px; line-height: 1.55;
  height: 380px; overflow-y: auto; padding: 12px 14px; white-space: pre-wrap; word-break: break-word;
}
#console .l-err { color: var(--err); }
#console .l-warn { color: var(--warn); }
#console .l-ok { color: var(--ok); }
#console .l-dim { color: #5c6488; }
footer { margin-top: 26px; text-align: center; color: var(--dim); font-size: 12.5px; }
.hidden { display: none !important; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">P</div>
    <h1>psd.ai install dashboard
      <small>live progress, models and errors while ./run.sh sets everything up</small>
    </h1>
  </header>

  <div id="banner" class="running"><span class="dot"></span><span class="msg" id="headline">Connecting to the installer...</span></div>

  <div class="grid">
    <div>
      <div class="card">
        <h2>Install steps</h2>
        <div id="steps"></div>
      </div>
      <div class="card hidden" id="dl-card">
        <h2>Model download</h2>
        <div style="display:flex;justify-content:space-between;align-items:baseline">
          <span class="dl-pct" id="dl-pct">0%</span>
          <span class="dl-name" id="dl-size"></span>
        </div>
        <div class="bar-outer"><div class="bar-inner" id="dl-bar"></div></div>
        <div class="dl-name" id="dl-file"></div>
      </div>
      <div class="card hidden" id="err-card">
        <h2>Errors &amp; warnings</h2>
        <div class="errs" id="errs"></div>
      </div>
    </div>
    <div>
      <div class="card hidden" id="models-card">
        <h2>Local model group</h2>
        <div class="models" id="models"></div>
      </div>
      <div class="card">
        <div class="tabs">
          <button class="tab on" id="tab-run" data-tab="run">run.sh log</button>
          <button class="tab" id="tab-model" data-tab="model">model log</button>
          <span class="spacer"></span>
          <label><input type="checkbox" id="autoscroll" checked> auto-scroll</label>
        </div>
        <div id="console"></div>
      </div>
    </div>
  </div>
  <footer>
    Served locally by <b>psd.ai/scripts/install_gui.py</b> - loopback only, closes itself after the
    install. Re-run without it: <code>./run.sh --no-gui</code>
  </footer>
</div>
<script>
(function () {
  var tab = "run";
  var fails = 0;
  var $ = function (id) { return document.getElementById(id); };

  function lineClass(t) {
    var low = t.toLowerCase();
    if (/(error|traceback|fatal|panic)/.test(low)) return "l-err";
    if (/(warn|\[warn\])/.test(low)) return "l-warn";
    if (/(==>|\[ok\]|ready|installed)/.test(low)) return "l-ok";
    return "";
  }

  function renderSteps(steps) {
    var host = $("steps"); host.innerHTML = "";
    steps.forEach(function (s) {
      var row = document.createElement("div");
      row.className = "step " + s.state;
      var dot = document.createElement("span"); dot.className = "dot";
      var body = document.createElement("div");
      var label = document.createElement("span"); label.className = "label";
      label.textContent = s.label;
      body.appendChild(label);
      if (s.msg || s.ts) {
        var meta = document.createElement("span"); meta.className = "meta";
        meta.textContent = [s.ts, s.msg].filter(Boolean).join("  -  ");
        body.appendChild(meta);
      }
      row.appendChild(dot); row.appendChild(body);
      host.appendChild(row);
    });
  }

  function renderDownload(dl) {
    var card = $("dl-card");
    if (!dl) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    $("dl-pct").textContent = dl.pct.toFixed(1) + "%";
    $("dl-bar").style.width = dl.pct + "%";
    $("dl-size").textContent = dl.have_gb.toFixed(2) + " / " + dl.total_gb.toFixed(2) + " GB";
    $("dl-file").textContent = dl.file;
  }

  function renderModels(models, group) {
    var card = $("models-card");
    if (!models || !models.length) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    var host = $("models"); host.innerHTML = "";
    models.forEach(function (m) {
      var box = document.createElement("div"); box.className = "model";
      var tag = document.createElement("span"); tag.className = "tag " + m.state;
      tag.textContent = m.state;
      var name = document.createElement("div"); name.className = "name";
      name.textContent = m.label;
      var sub = document.createElement("div"); sub.className = "sub";
      var bits = [];
      if (m.quant) bits.push(m.quant);
      if (m.port) bits.push(":" + m.port);
      if (m.size_gb) bits.push(m.size_gb + " GB");
      if (m.gpu_layers) bits.push("ngl " + m.gpu_layers);
      if (m.vision) bits.push("vision");
      sub.textContent = bits.join("  -  ");
      box.appendChild(tag); box.appendChild(name); box.appendChild(sub);
      host.appendChild(box);
    });
  }

  function renderErrors(errs) {
    var card = $("err-card");
    if (!errs || !errs.length) { card.classList.add("hidden"); return; }
    card.classList.remove("hidden");
    var host = $("errs"); host.innerHTML = "";
    errs.forEach(function (e) {
      var div = document.createElement("div"); div.className = "err";
      div.textContent = e;
      host.appendChild(div);
    });
  }

  function renderLog(st) {
    var host = $("console");
    var lines = (st.log && st.log[tab]) || [];
    var atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 40;
    host.innerHTML = "";
    lines.forEach(function (t) {
      var div = document.createElement("div");
      var cls = lineClass(t);
      if (cls) div.className = cls;
      div.textContent = t;
      host.appendChild(div);
    });
    if ($("autoscroll").checked && (atBottom || tab === lastTab)) {
      host.scrollTop = host.scrollHeight;
    }
    lastTab = tab;
  }
  var lastTab = "run";

  function render(st) {
    var banner = $("banner");
    banner.className = st.overall;
    $("headline").textContent = st.headline;
    document.title = (st.overall === "running" ? "… " : st.overall === "failed" ? "✖ " : "✔ ")
      + "psd.ai install";
    renderSteps(st.steps || []);
    renderDownload(st.download);
    renderModels(st.models, st.model_group);
    renderErrors(st.errors);
    // If the model step is active and the user has not picked a tab, follow it.
    var modelsRunning = (st.steps || []).some(function (s) { return s.id === "models" && s.state === "running"; });
    if (!userPickedTab) { tab = modelsRunning ? "model" : "run"; syncTabs(); }
    renderLog(st);
  }

  var userPickedTab = false;
  function syncTabs() {
    $("tab-run").className = "tab" + (tab === "run" ? " on" : "");
    $("tab-model").className = "tab" + (tab === "model" ? " on" : "");
  }
  document.querySelectorAll(".tab").forEach(function (btn) {
    btn.addEventListener("click", function () {
      userPickedTab = true; tab = btn.dataset.tab; syncTabs();
    });
  });

  function poll() {
    fetch("/api/status", { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      fails = 0;
      return r.json();
    }).then(render).catch(function () {
      fails++;
      if (fails >= 4) {
        $("banner").className = "finished";
        $("headline").textContent =
          "The install dashboard has closed (run.sh finished). Everything above stays as it was.";
      }
    });
  }
  poll();
  setInterval(poll, 1500);
})();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "psd-install-gui/1.0"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass  # browser closed mid-response

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            ctx = self.server.psd_ctx  # type: ignore[attr-defined]
            payload = build_status(Path(ctx["log_dir"]), Path(ctx["runtime_dir"]),
                                   ctx["run_pid"])
            payload["grace_left"] = ctx.get("grace_left")
            self._send(200, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"", "text/plain")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stderr
        pass


def pick_port(host: str, wanted: int, tries: int = 20) -> Tuple[socket.socket, int]:
    """Bind the first free port at or above `wanted`; returns the bound socket."""
    for offset in range(tries):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, wanted + offset))
            return sock, wanted + offset
        except OSError:
            sock.close()
    raise OSError(f"no free port in {wanted}..{wanted + tries - 1}")


def create_server(log_dir: Path, runtime_dir: Path, port: int,
                  run_pid: Optional[int], host: str = "127.0.0.1"
                  ) -> Tuple[ThreadingHTTPServer, int]:
    """Build the dashboard server. `port=0` asks the OS for any free port."""
    if port == 0:
        httpd = ThreadingHTTPServer((host, 0), _Handler)
        actual = httpd.server_address[1]
    else:
        _sock, actual = pick_port(host, port)
        _sock.close()
        httpd = ThreadingHTTPServer((host, actual), _Handler)
    httpd.psd_ctx = {  # type: ignore[attr-defined]
        "log_dir": str(log_dir),
        "runtime_dir": str(runtime_dir),
        "run_pid": run_pid,
        "grace_left": None,
    }
    return httpd, actual


def _grace_watchdog(httpd: ThreadingHTTPServer, run_pid: Optional[int],
                    grace: float) -> None:
    """After run.sh exits, keep serving for `grace` seconds, then stop."""
    if not run_pid:
        return
    exited_at: Optional[float] = None
    while True:
        time.sleep(2)
        if exited_at is None:
            if not pid_alive(run_pid):
                exited_at = time.time()
            continue
        left = grace - (time.time() - exited_at)
        httpd.psd_ctx["grace_left"] = max(0, int(left))  # type: ignore[attr-defined]
        if left <= 0:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
            return


def main(argv: Optional[List[str]] = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log-dir", default=str(repo_root / "logs"))
    ap.add_argument("--runtime-dir", default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int,
                    default=int(os.getenv("PSD_INSTALL_GUI_PORT") or 7123))
    ap.add_argument("--run-pid", type=int, default=None,
                    help="pid of run.sh; the server exits GRACE seconds after it dies")
    ap.add_argument("--grace", type=float, default=600.0)
    args = ap.parse_args(argv)

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = Path(args.runtime_dir) if args.runtime_dir else default_runtime_dir()
    run_pid = args.run_pid or (os.getppid() if os.getppid() > 1 else None)

    try:
        httpd, port = create_server(log_dir, runtime_dir, args.port, run_pid,
                                    host=args.host)
    except OSError as exc:
        print(f"install-gui: could not bind a port ({exc})", file=sys.stderr)
        return 1

    (log_dir / "install-gui.port").write_text(str(port), encoding="utf-8")
    (log_dir / "install-gui.pid").write_text(str(os.getpid()), encoding="utf-8")
    threading.Thread(target=_grace_watchdog, args=(httpd, run_pid, args.grace),
                     daemon=True).start()
    print(f"install-gui: serving http://{args.host}:{port} (run pid {run_pid})",
          flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        for name in ("install-gui.port", "install-gui.pid"):
            try:
                (log_dir / name).unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.exit(main())

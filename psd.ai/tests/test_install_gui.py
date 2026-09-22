"""scripts/install_gui.py — the browser dashboard run.sh opens while installing.

The first run of ./run.sh is long (dnf, pip, a multi-GB model download, a Rust
build) and used to be a wall of terminal text. The dashboard turns that into a
step timeline, a real download progress bar, model cards and an error panel.
These tests pin the contracts that make it safe and honest:

  * stdlib only - it runs on the SYSTEM python3 before the venv exists;
  * read-only on the logs it tails, loopback-only on the socket;
  * overall state follows explicit markers/files, never log-line vibes;
  * run.sh wires it in (--no-gui opt-out, markers at every step, die() and
    cleanup() report failures) and the server outlives run.sh by a grace
    window so a dead install stays readable.
"""

import json
import pathlib
import threading
import urllib.request

import pytest

from scripts import install_gui as gui

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUN_SH = REPO_ROOT / "run.sh"


# ── log rendering helpers ────────────────────────────────────────────────────
def test_strip_ansi_removes_color_and_carriage_returns():
    raw = "\x1b[31mERROR\x1b[0m: boom\r\x1b[Kdone"
    assert gui.strip_ansi(raw) == "ERROR: boomdone"


def test_tail_lines_reads_the_end_and_tolerates_a_missing_file(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("\n".join(f"line {i}" for i in range(10)), encoding="utf-8")
    assert gui.tail_lines(log, limit=3) == ["line 7", "line 8", "line 9"]
    assert gui.tail_lines(tmp_path / "absent.log") == []


# ── step markers written by run.sh's gui_step() ─────────────────────────────
def _write_markers(path, rows):
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_read_step_markers_skips_garbage_lines(tmp_path):
    p = tmp_path / "install-steps.jsonl"
    p.write_text(
        '{"ts":"1","step":"system","state":"done","msg":""}\n'
        "not json at all\n"
        '{"truncated": \n',
        encoding="utf-8",
    )
    markers = gui.read_step_markers(p)
    assert [m["step"] for m in markers] == ["system"]


def test_aggregate_steps_latest_state_wins_in_canonical_order(tmp_path):
    p = tmp_path / "install-steps.jsonl"
    _write_markers(p, [
        {"ts": "1", "step": "models", "state": "running", "msg": "downloading"},
        {"ts": "2", "step": "system", "state": "done", "msg": ""},
        {"ts": "3", "step": "models", "state": "done", "msg": "3 models serving"},
        {"ts": "4", "step": "run", "state": "exited", "msg": "code 0"},
    ])
    steps = gui.aggregate_steps(gui.read_step_markers(p))
    by_id = {s["id"]: s for s in steps}
    assert by_id["models"]["state"] == "done"
    assert by_id["models"]["msg"] == "3 models serving"
    assert by_id["system"]["state"] == "done"
    assert by_id["launch"]["state"] == "pending"  # never marked yet
    assert "run" not in by_id  # run lifecycle is not a timeline step
    # canonical order: system before models before launch
    ids = [s["id"] for s in steps]
    assert ids.index("system") < ids.index("models") < ids.index("launch")


# ── download progress + model activity parsed from the bootstrap log ────────
MODEL_LOG = [
    "  ============================================================",
    "  ==> Starting Qwen3.5 9B (chat) on port 8181 (ngl 99)...",
    "      Qwen3.5-9B-Q4_K_M.gguf:   7.0%  0.94/13.40 GB",
    "      Qwen3.5-9B-Q4_K_M.gguf:  42.1%  5.62/13.40 GB",
    "  ==> Starting gpt-oss 20B (reasoning) on port 8182 (ngl 0, cpu-moe)...",
]


def test_parse_download_takes_the_latest_progress_line():
    dl = gui.parse_download(MODEL_LOG)
    assert dl == {"file": "Qwen3.5-9B-Q4_K_M.gguf", "pct": 42.1,
                  "have_gb": 5.62, "total_gb": 13.40}


def test_parse_download_none_when_nothing_is_downloading():
    assert gui.parse_download(["  ==> Local model group ready: 3 models"]) is None


def test_parse_model_activity_finds_announced_models_and_group_ready():
    activity = gui.parse_model_activity(
        MODEL_LOG + ["  ==> Local model group ready: 2 models on ports 8181, 8182"])
    assert activity["group_ready"] == 2
    assert activity["planned"] == [
        {"label": "Qwen3.5 9B (chat)", "port": 8181},
        {"label": "gpt-oss 20B (reasoning)", "port": 8182},
    ]


# ── error panel ──────────────────────────────────────────────────────────────
def test_collect_errors_finds_real_failures_and_skips_benign_mentions():
    lines = [
        "2026-09-21 10:00:00  ERROR: pip install requirements.txt failed",
        "      [warn] port 8182 is busy; skipping gpt-oss",
        "npm ERR! command not found: tauri",
        "    0 failed, 90 passed",                       # benign summary
        "rm -f local_model_failed.txt",                    # machinery, not a failure
        "Traceback (most recent call last):",
    ]
    errors = gui.collect_errors(lines, fail_text="only 2 of 3 models ready")
    assert errors[0] == "only 2 of 3 models ready"  # the failure report leads
    joined = "\n".join(errors)
    assert "pip install requirements.txt failed" in joined
    assert "command not found" in joined
    assert "Traceback" in joined
    assert "port 8182 is busy" not in joined
    assert "0 failed" not in joined


def test_collect_errors_dedupes_and_caps():
    lines = ["ERROR: the same thing failed"] * 40
    errors = gui.collect_errors(lines)
    assert errors == ["ERROR: the same thing failed"]
    many = [f"ERROR: distinct failure {i}" for i in range(40)]
    assert len(gui.collect_errors(many)) == gui.MAX_ERRORS


# ── build_status: the whole JSON payload the page polls ─────────────────────
@pytest.fixture
def env(tmp_path):
    log_dir = tmp_path / "logs"
    runtime = tmp_path / "runtime"
    log_dir.mkdir()
    runtime.mkdir()
    return log_dir, runtime


def test_status_running_with_download_headline(env):
    log_dir, runtime = env
    _write_markers(log_dir / "install-steps.jsonl", [
        {"ts": "1", "step": "system", "state": "done", "msg": ""},
        {"ts": "2", "step": "models", "state": "running", "msg": "downloading"},
    ])
    (log_dir / "local-model.log").write_text(
        "      Qwen3.5-9B-Q4_K_M.gguf:  42.1%  5.62/13.40 GB\n", encoding="utf-8")
    st = gui.build_status(log_dir, runtime, run_pid=None)
    assert st["overall"] == "running"
    assert "42%" in st["headline"]
    assert st["download"]["pct"] == 42.1


def test_status_failed_by_step_marker_and_fail_file(env):
    log_dir, runtime = env
    _write_markers(log_dir / "install-steps.jsonl", [
        {"ts": "1", "step": "deps", "state": "failed", "msg": "pip exploded"},
    ])
    (runtime / "local_model_failed.txt").write_text("no group reachable", encoding="utf-8")
    st = gui.build_status(log_dir, runtime, run_pid=None)
    assert st["overall"] == "failed"
    assert "no group reachable" in st["errors"]
    assert st["failed_text"] == "no group reachable"


def test_status_ready_from_group_state_with_model_cards(env):
    log_dir, runtime = env
    (runtime / "local_model.json").write_text("{}", encoding="utf-8")
    (runtime / "local_model_group.json").write_text(json.dumps({
        "count": 2, "profile": "balanced", "ports": [8181, 8182],
        "models": [
            {"label": "Qwen3.5 9B (chat)", "quant": "Q4_K_M", "port": 8181,
             "file_size_gb": 13.4, "gpu_layers": 99, "vision": False},
            {"label": "gpt-oss 20B (reasoning)", "quant": "MXFP4", "port": 8182,
             "file_size_gb": 11.6, "gpu_layers": 0, "vision": False},
        ],
    }), encoding="utf-8")
    (log_dir / "local-model.log").write_text(
        "      Qwen3.5-9B-Q4_K_M.gguf: 100.0%  13.40/13.40 GB\n"
        "  ==> Local model group ready: 2 models on ports 8181, 8182\n",
        encoding="utf-8")
    st = gui.build_status(log_dir, runtime, run_pid=None)
    assert st["overall"] == "ready"
    assert st["download"] is None  # group is up: no progress bar anymore
    assert [m["state"] for m in st["models"]] == ["ready", "ready"]
    assert st["model_group"]["count"] == 2


def test_status_finished_when_the_run_pid_is_gone(env):
    log_dir, runtime = env
    dead_pid = 2 ** 22 + 1  # above the kernel's hard pid_max: guaranteed ESRCH
    st = gui.build_status(log_dir, runtime, run_pid=dead_pid)
    assert st["run_alive"] is False
    assert st["overall"] == "finished"


def test_status_merges_announced_models_not_yet_in_the_group(env):
    log_dir, runtime = env
    (log_dir / "local-model.log").write_text(
        "  ==> Starting Qwen3.5 9B (chat) on port 8181 (ngl 99)...\n",
        encoding="utf-8")
    st = gui.build_status(log_dir, runtime, run_pid=None)
    assert st["models"] == [{
        "label": "Qwen3.5 9B (chat)", "quant": "", "port": 8181,
        "size_gb": None, "gpu_layers": None, "vision": False, "state": "starting",
    }]


# ── the module must run on a bare system python3 (before the venv) ──────────
def test_install_gui_is_stdlib_only():
    import ast
    tree = ast.parse((pathlib.Path(gui.__file__)).read_text(encoding="utf-8"))
    allowed = {
        "argparse", "json", "os", "re", "signal", "socket", "sys", "threading",
        "time", "http.server", "http", "pathlib", "typing", "__future__",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import inside scripts/
                continue
            assert (node.module or "").split(".")[0] in allowed, node.module


# ── live server on an ephemeral port ────────────────────────────────────────
def test_server_serves_page_and_status_over_loopback(env):
    log_dir, runtime = env
    _write_markers(log_dir / "install-steps.jsonl", [
        {"ts": "1", "step": "system", "state": "running", "msg": "dnf install"},
    ])
    httpd, port = gui.create_server(log_dir, runtime, 0, run_pid=None)
    thread = threading.Thread(target=httpd.serve_forever,
                              kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status",
                                    timeout=10) as r:
            assert r.status == 200
            payload = json.loads(r.read())
        assert payload["overall"] == "running"
        assert payload["steps"][0]["id"] == "system"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            html = r.read().decode("utf-8")
        assert "install dashboard" in html
        assert "/api/status" in html  # the page polls same-origin

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico",
                                    timeout=10) as r:
            assert r.status == 204
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=10)
            raise AssertionError("expected a 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_pick_port_skips_a_busy_port(env):
    log_dir, runtime = env
    httpd, first = gui.create_server(log_dir, runtime, 0, run_pid=None)
    try:
        _sock, picked = gui.pick_port("127.0.0.1", first, tries=5)
        _sock.close()
        assert picked > first  # the busy port was skipped, not reused
    finally:
        httpd.server_close()


# ── run.sh contract: the dashboard is wired into the installer ──────────────
@pytest.fixture(scope="module")
def run_sh():
    return RUN_SH.read_text(encoding="utf-8")


def test_run_sh_has_a_no_gui_opt_out(run_sh):
    assert "--no-gui)" in run_sh
    assert 'NO_GUI="${PSD_NO_GUI:-0}"' in run_sh
    assert "[ \"$NO_GUI\" = \"1\" ] && return 0" in run_sh


def test_run_sh_marks_every_canonical_step(run_sh):
    for step_id, _label in gui.STEP_ORDER:
        assert f'gui_step {step_id} running' in run_sh or \
               f'gui_step {step_id} done' in run_sh or \
               f'gui_step {step_id} skipped' in run_sh, step_id


def test_run_sh_failures_reach_the_dashboard(run_sh):
    # die() marks the in-flight step AND the run, so the page shows where it died
    assert 'gui_step "$CURRENT_STEP" failed' in run_sh
    assert "gui_step run failed" in run_sh
    # cleanup() (EXIT trap) writes the exited marker that flips the page to
    # "finished" and starts the grace countdown
    assert 'gui_step run exited "code $rc"' in run_sh


def test_run_sh_starts_the_server_before_dnf_and_reopens_after_python(run_sh):
    # started with the SYSTEM python3 before section 1 so the dnf step is on
    # the dashboard too ...
    early = run_sh.index("start_install_gui python3")
    assert early < run_sh.index("gui_step system running")
    # ... and again with the discovered interpreter when python3 was missing
    assert 'if [ -z "$GUI_URL" ]; then' in run_sh
    assert 'start_install_gui "$PYCMD"' in run_sh


def test_run_sh_serves_loopback_only_and_opens_the_browser_safely(run_sh):
    # the dashboard is an overlay, never a dependency: setsid + best effort
    assert "scripts/install_gui.py" in run_sh
    assert '--run-pid "$$"' in run_sh
    # browser opening is guarded by a session check (headless boxes skip it)
    assert '${WAYLAND_DISPLAY:-}${DISPLAY:-}' in run_sh
    assert "xdg-open" in run_sh


def test_run_sh_writes_markers_to_the_log_dir(run_sh):
    assert 'GUI_STEP_FILE="${LOG_DIR}/install-steps.jsonl"' in run_sh
    # the same file the server reads
    assert "install-steps.jsonl" in pathlib.Path(gui.__file__).read_text(encoding="utf-8")


def test_run_sh_starts_each_run_with_a_fresh_timeline(run_sh):
    # The server reads the marker file whole; a stale "run exited" from an
    # earlier --doctor run would otherwise flip a brand-new install straight
    # to "finished". run.sh must clear the file before the first marker.
    assert 'rm -f "$GUI_STEP_FILE"' in run_sh
    assert run_sh.index('rm -f "$GUI_STEP_FILE"') < run_sh.index("gui_step system running")


def test_gui_step_marker_format_matches_what_the_server_parses(run_sh, tmp_path):
    """The printf in run.sh and read_step_markers must agree on the schema."""
    # run.sh's printf writes exactly the keys the server reads, one JSON
    # object per line - pin the format string so the two cannot drift apart
    assert '"ts":"%s","step":"%s","state":"%s","msg":"%s"' in run_sh
    marker = {"ts": "12:00:00", "step": "system", "state": "running", "msg": "dnf"}
    p = tmp_path / "install-steps.jsonl"
    p.write_text(json.dumps(marker) + "\n", encoding="utf-8")
    parsed = gui.read_step_markers(p)
    assert parsed == [marker]
    steps = gui.aggregate_steps(parsed)
    assert steps[0]["state"] == "running"

"""installer/ - the native graphical installer window.

The install must run behind a REAL app window (like a proper installer):
progress bar, running step, live output, the model download, and faults
listed properly with Retry / Skip - not on a localhost browser page.
`./run.sh` installs the Rust toolchain first, builds `installer/` once and
hands the whole setup to it.

Rust cannot be compiled in CI here, so these tests pin the contract
statically:

  * the Tauri v2 config stays minimal and offline-safe (no bundling, no
    npm frontend, CSP 'self', withGlobalTauri so ui/ is plain JS);
  * Cargo.toml keeps the dependency surface tiny (no network clients -
    the installer spawns run.sh's own commands instead);
  * main.rs keeps the UI contract (get_state/send_action/"state" event),
    the supervisor semantics (process_group(0), TERM->KILL on quit) and
    the step plan (venv -> deps -> extras -> stt -> setup -> build,
    models after deps, launch with a headless fallback);
  * ui/ never reaches the network (works fully offline under CSP);
  * run.sh gates the installer correctly and propagates its exit code.
"""

import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "installer"
SRC_TAURI = INSTALLER / "src-tauri"
UI = INSTALLER / "ui"
MAIN_RS = SRC_TAURI / "src" / "main.rs"
CONF = SRC_TAURI / "tauri.conf.json"
CARGO = SRC_TAURI / "Cargo.toml"
CAPS = SRC_TAURI / "capabilities" / "default.json"
RUN_SH = REPO_ROOT / "run.sh"
BINARY = "psd-ai-installer"


@pytest.fixture(scope="module")
def conf():
    return json.loads(CONF.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cargo():
    return CARGO.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def main_rs():
    return MAIN_RS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def run_sh():
    return RUN_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js():
    return (UI / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html():
    return (UI / "index.html").read_text(encoding="utf-8")


# ── tauri config ────────────────────────────────────────────────────────────

def test_conf_is_tauri_v2_and_minimal(conf):
    assert conf["$schema"] == "https://schema.tauri.app/config/2"
    assert conf["identifier"] == "ai.psd.installer"
    # the installer is built with plain `cargo build --release` - no
    # `tauri build`, no bundles, no npm frontend pipeline
    assert conf["build"]["frontendDist"] == "../ui"
    assert conf["bundle"]["active"] is False


def test_conf_uses_global_tauri_so_ui_needs_no_bundler(conf):
    assert conf["app"]["withGlobalTauri"] is True


def test_conf_window_matches_capabilities(conf):
    win = conf["app"]["windows"][0]
    assert win["label"] == "main"
    caps = json.loads(CAPS.read_text(encoding="utf-8"))
    assert caps["windows"] == ["main"]
    # core:default covers invoke + listen, nothing more is needed
    assert "core:default" in caps["permissions"]


def test_conf_csp_keeps_the_ui_offline(conf):
    csp = conf["app"]["security"]["csp"]
    assert "default-src 'self'" in csp


def test_conf_icons_exist(conf):
    for rel in conf["bundle"]["icon"]:
        assert (SRC_TAURI / rel).is_file(), rel


# ── cargo manifest ──────────────────────────────────────────────────────────

def test_cargo_binary_name_is_consistent(cargo, run_sh, main_rs):
    assert f'name = "{BINARY}"' in cargo
    assert f"target/release/{BINARY}" in run_sh
    # main.rs derives the repo root from its own exe path - name is only
    # documented there, but the run.sh probe must match the manifest
    assert BINARY in main_rs


def test_cargo_deps_stay_tiny_and_offline(cargo):
    for dep in ("tauri", "serde", "serde_json"):
        assert dep in cargo
    # no HTTP client, no async runtime, no regex: the installer spawns the
    # project's own commands and parses their stdout by hand
    for banned in ("reqwest", "tokio", "regex", "ureq"):
        assert banned not in cargo, f"{banned} does not belong in the installer"


# ── backend contract (main.rs) ──────────────────────────────────────────────

def test_main_rs_exposes_the_ui_contract(main_rs):
    assert "generate_handler![get_state, send_action]" in main_rs
    assert 'app.emit("state"' in main_rs
    assert '"retry"' in main_rs and '"skip"' in main_rs
    assert '"launch"' in main_rs and '"stop"' in main_rs and '"quit"' in main_rs


def test_main_rs_reads_the_run_sh_environment(main_rs):
    for key in (
        "PSD_INSTALL_APP_DIR", "PSD_INSTALL_DESKTOP_DIR", "PSD_INSTALL_LOG_DIR",
        "PSD_INSTALL_PYCMD", "PSD_INSTALL_LLAMA_PORT", "PSD_INSTALL_MODEL_WAIT",
        "PSD_INSTALL_NO_VOICE", "PSD_INSTALL_NO_MODELS", "PSD_INSTALL_NO_STT",
    ):
        assert key in main_rs, key


def test_main_rs_plans_the_canonical_steps(main_rs):
    # the same work run.sh does in the terminal flow, in order
    for marker in (
        "-m\", \"venv",                 # python -m venv
        "requirements.txt",
        "requirements-jarvis.txt",
        "faster-whisper",
        "setup.py",
        "npm\", \"run\", \"tauri\", \"build\", \"--\", \"--no-bundle",
        "local_llama.py",
        '"--foreground"',
    ):
        assert marker in main_rs, marker
    # stamps keep re-runs fast (same files the terminal flow writes)
    assert ".deps_ok" in main_rs
    assert ".jarvis_ok" in main_rs
    # setup.py runs politely inside the installer window
    assert "PSD_AI_DEFER_ADMIN" in main_rs
    assert "PSD_AI_SKIP_RUN_HINT" in main_rs


def test_main_rs_models_start_after_deps(main_rs):
    # scripts/local_llama.py imports core.database inside register_endpoint,
    # so the model track may only spawn once the venv has its dependencies;
    # main.rs documents that constraint and the runner gates the spawn on it
    assert "spawn_models_track" in main_rs
    assert "core.database" in main_rs          # the ordering comment
    assert "models_spawned" in main_rs         # runner tracks the gate
    assert "fn build_plan" in main_rs and "fn runner" in main_rs


def test_main_rs_supervises_process_groups(main_rs):
    assert "process_group(0)" in main_rs
    assert '"TERM"' in main_rs and '"KILL"' in main_rs
    assert "kill_all" in main_rs and "quit_now" in main_rs


def test_main_rs_has_a_headless_launch_fallback(main_rs):
    assert "uvicorn" in main_rs
    assert "app:app" in main_rs
    assert "find_app_bin" in main_rs


def test_main_rs_parses_progress_without_crates(main_rs):
    for fn in (
        "parse_gguf_progress", "parse_starting", "parse_model_ready",
        "parse_group_ready", "parse_compiling",
    ):
        assert f"fn {fn}" in main_rs, fn


# ── frontend (ui/) ──────────────────────────────────────────────────────────

def test_ui_files_exist():
    for name in ("index.html", "app.js", "style.css"):
        assert (UI / name).is_file(), name


def test_ui_is_offline_safe(index_html, app_js):
    css = (UI / "style.css").read_text(encoding="utf-8")
    for blob in (index_html, app_js, css):
        assert "http://" not in blob.replace("http://127.0.0.1", "")
        assert "https://" not in blob
        assert "cdn." not in blob
    # local files only
    assert 'href="style.css"' in index_html
    assert 'src="app.js"' in index_html


def test_app_js_speaks_the_backend_contract(app_js):
    assert 'invoke("get_state")' in app_js
    assert 'invoke("send_action"' in app_js
    assert 'listen("state"' in app_js
    for action in ("retry", "skip", "launch", "stop", "quit"):
        assert f'"{action}"' in app_js, action


def test_app_js_renders_phases_faults_and_models(app_js):
    for token in ("phase", "fatal_step", "models", "step_pct", "headline",
                  "errors", "log"):
        assert token in app_js, token


# ── run.sh wiring ───────────────────────────────────────────────────────────

def test_run_sh_gates_the_graphical_installer(run_sh):
    assert "needs_graphical_install()" in run_sh
    assert "run_graphical_installer()" in run_sh
    # opt-outs: --no-gui, --no-app, headless session, missing cargo
    assert 'if [ "$NO_GUI" = "1" ] || [ "$NO_APP" = "1" ]; then return 1; fi' in run_sh
    assert '"$DISPLAY"' in run_sh and '"$WAYLAND_DISPLAY"' in run_sh
    assert ".cargo/bin/cargo" in run_sh


def test_run_sh_builds_then_runs_the_installer(run_sh):
    assert "installer/src-tauri" in run_sh
    assert "build --release" in run_sh
    # the installer's own exit code is what run.sh reports
    assert "exit $rc" in run_sh
    # a failed installer build falls back to the terminal flow, it never
    # strands the user
    assert "falling back to the terminal installation flow" in run_sh


def test_run_sh_passes_the_install_environment(run_sh):
    for key in ("PSD_INSTALL_APP_DIR", "PSD_INSTALL_DESKTOP_DIR",
                "PSD_INSTALL_LOG_DIR", "PSD_INSTALL_PYCMD",
                "PSD_INSTALL_LLAMA_PORT", "PSD_INSTALL_MODEL_WAIT",
                "PSD_INSTALL_NO_VOICE", "PSD_INSTALL_NO_MODELS",
                "PSD_INSTALL_NO_STT"):
        assert key in run_sh, key


def test_run_sh_installs_the_toolchain_before_the_installer(run_sh):
    # user-facing order: first the Rust/Tauri toolchain (dnf section), then
    # build+run the native installer - the dnf group carries rust/cargo/
    # webkit2gtk4.1-devel, and section 2b comes after the python section
    assert "rust cargo" in run_sh or "rust\ncargo" in run_sh
    assert "webkit2gtk4.1-devel" in run_sh
    assert run_sh.index("PKGS_DESKTOP=") < run_sh.index("run_graphical_installer")


def test_help_text_mentions_the_graphical_installer(run_sh):
    # the header comment IS the --help text
    assert "GRAPHICAL" in run_sh.upper().split("usage()")[0]
    assert "--no-gui" in run_sh


def test_installer_readme_documents_the_contract():
    readme = (INSTALLER / "README.md").read_text(encoding="utf-8")
    for token in ("get_state", "send_action", "PSD_INSTALL_APP_DIR",
                  "cargo build --release", "Retry", "Skip", "process group"):
        assert token in readme, token

//! psd.ai graphical installer.
//!
//! A native window that owns the whole first-run setup, the way a real
//! application installer does - no browser page, no localhost server:
//!
//!   1. run.sh installs the system packages (dnf, the only step needing a
//!      password) and builds this crate, then hands everything over here.
//!   2. This app runs the remaining steps as child processes - venv,
//!      pip dependencies, voice extras, setup.py, the desktop-app build -
//!      parsing their output into a progress bar, per-step status and an
//!      activity line.
//!   3. The local model group (scripts/local_llama.py) downloads in a
//!      parallel track with its own progress bar and model chips.
//!   4. Every fault is collected into a card: which step, exit code, the
//!      suspicious output lines and a concrete hint. Fatal faults stop the
//!      run and offer Retry/Skip; optional faults (voice extras, the
//!      desktop build, the model group) become warnings and the install
//!      continues - psd.ai can always start headless.
//!   5. When ready, Launch starts the desktop app (or the headless server
//!      + browser when the window build failed). This process stays as the
//!      supervisor: closing the app window kills the model group, exactly
//!      like run.sh does in the terminal flow.
//!
//! Deliberately dependency-free beyond tauri/serde: no HTTP client, no
//! async runtime, no regex crate - std threads, std processes and manual
//! line parsing only, so the first `cargo build` stays small.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use tauri::{AppHandle, Emitter, State};

// ------------------------------------------------------------------
// UI state (serialized wholesale to the frontend on every change)
// ------------------------------------------------------------------

#[derive(Clone, Serialize)]
struct StepUi {
    id: String,
    label: String,
    /// pending | running | done | warn | failed | skipped
    state: String,
    msg: String,
    /// true when a failure here must stop the install
    fatal: bool,
}

#[derive(Clone, Serialize)]
struct ModelItem {
    label: String,
    port: u16,
    /// starting | ready
    state: String,
}

#[derive(Clone, Serialize)]
struct ModelsUi {
    /// idle | downloading | ready | failed | timeout
    state: String,
    pct: f64,
    file: String,
    detail: String,
    ready_count: u32,
    items: Vec<ModelItem>,
    error: String,
}

#[derive(Clone, Serialize)]
struct ErrorUi {
    /// error | warn
    kind: String,
    step: String,
    code: i64,
    msg: String,
    hint: String,
    lines: Vec<String>,
    ts: String,
}

#[derive(Clone, Serialize)]
struct Ui {
    /// installing | waiting_models | ready | failed | running_app | closed
    phase: String,
    headline: String,
    activity: String,
    /// 0..1 progress within the running sequential step (parsers fill this)
    step_pct: f64,
    /// which step id `step_pct` belongs to
    step_id: String,
    steps: Vec<StepUi>,
    models: ModelsUi,
    errors: Vec<ErrorUi>,
    log: Vec<String>,
    /// set when the headless server is the way in
    app_url: String,
    app_running: bool,
    can_launch: bool,
    /// step id that hard-failed and can be retried
    fatal_step: String,
}

fn initial_ui() -> Ui {
    Ui {
        phase: "installing".into(),
        headline: "Preparing psd.ai...".into(),
        activity: String::new(),
        step_pct: 0.0,
        step_id: String::new(),
        steps: Vec::new(),
        models: ModelsUi {
            state: "idle".into(),
            pct: 0.0,
            file: String::new(),
            detail: String::new(),
            ready_count: 0,
            items: Vec::new(),
            error: String::new(),
        },
        errors: Vec::new(),
        log: Vec::new(),
        app_url: String::new(),
        app_running: false,
        can_launch: false,
        fatal_step: String::new(),
    }
}

// ------------------------------------------------------------------
// Config (everything run.sh tells us through the environment)
// ------------------------------------------------------------------

struct Config {
    app_dir: PathBuf,
    desktop_dir: PathBuf,
    log_dir: PathBuf,
    pycmd: String,
    venvpy: PathBuf,
    llama_port: String,
    model_wait_secs: u64,
    no_voice: bool,
    no_models: bool,
    no_stt: bool,
    app_bind: String,
    app_port: String,
}

fn env_or(key: &str, default: &str) -> String {
    match std::env::var(key) {
        Ok(v) if !v.trim().is_empty() => v,
        _ => default.to_string(),
    }
}

fn env_path(key: &str, fallback: PathBuf) -> PathBuf {
    let v = env_or(key, "");
    if v.is_empty() {
        fallback
    } else {
        PathBuf::from(v)
    }
}

fn repo_root_from_exe() -> PathBuf {
    // installer/src-tauri/target/release/psd-ai-installer -> repo root
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent().map(Path::to_path_buf);
        for _ in 0..6 {
            if let Some(d) = dir.clone() {
                if d.join("run.sh").exists() && d.join("psd.ai").is_dir() {
                    return d;
                }
                dir = d.parent().map(Path::to_path_buf);
            }
        }
    }
    PathBuf::from(".")
}

fn load_config() -> Config {
    let root = repo_root_from_exe();
    let app_dir = env_path("PSD_INSTALL_APP_DIR", root.join("psd.ai"));
    let desktop_dir = env_path("PSD_INSTALL_DESKTOP_DIR", root.join("desktop"));
    let log_dir = env_path("PSD_INSTALL_LOG_DIR", root.join("logs"));
    let venvpy = app_dir.join("venv").join("bin").join("python");
    Config {
        app_dir,
        desktop_dir,
        log_dir,
        pycmd: env_or("PSD_INSTALL_PYCMD", "python3"),
        venvpy,
        llama_port: env_or("PSD_INSTALL_LLAMA_PORT", "8080"),
        model_wait_secs: env_or("PSD_INSTALL_MODEL_WAIT", "2400")
            .parse()
            .unwrap_or(2400),
        no_voice: env_or("PSD_INSTALL_NO_VOICE", "") == "1",
        no_models: env_or("PSD_INSTALL_NO_MODELS", "") == "1",
        no_stt: env_or("PSD_INSTALL_NO_STT", "") == "1",
        app_bind: env_or("APP_BIND", "127.0.0.1"),
        app_port: env_or("APP_PORT", "7000"),
    }
}

// ------------------------------------------------------------------
// Shared context, actions, child registry
// ------------------------------------------------------------------

enum Action {
    Retry(String),
    Skip(String),
    Launch,
    Stop,
    Quit,
}

/// One shared block for the tauri commands, the runner thread and the
/// window-close handler. Everything inside is cheap to clone (Arcs).
struct CtxShared {
    ui: Arc<Mutex<Ui>>,
    tx: Arc<Mutex<Sender<Action>>>,
    /// process-group ids of every live child (models, builds, the app)
    children: Arc<Mutex<Vec<u32>>>,
    models_pgid: Arc<Mutex<Option<u32>>>,
    app_pgid: Arc<Mutex<Option<u32>>>,
    models_started: Arc<Mutex<Option<Instant>>>,
    quit: Arc<AtomicBool>,
}

fn ts() -> String {
    let s = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("{:02}:{:02}:{:02}", (s / 3600) % 24, (s / 60) % 60, s % 60)
}

fn register_child(children: &Arc<Mutex<Vec<u32>>>, pgid: u32) {
    children.lock().unwrap().push(pgid);
}

fn unregister_child(children: &Arc<Mutex<Vec<u32>>>, pgid: u32) {
    children.lock().unwrap().retain(|p| *p != pgid);
}

/// Signal a whole process group like run.sh's cleanup does: the child leads
/// its own group (process_group(0)), so a negative pid reaches pip workers,
/// npm, cargo and every llama-server at once. `kill(1)` keeps this crate
/// free of unsafe/libc dependencies.
#[cfg(unix)]
fn signal_group(pgid: u32, signal: &str) {
    let _ = Command::new("kill")
        .arg(format!("-{signal}"))
        .arg(format!("-{pgid}"))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

fn kill_group(pgid: u32) {
    #[cfg(unix)]
    {
        signal_group(pgid, "TERM");
        std::thread::sleep(Duration::from_millis(500));
        signal_group(pgid, "KILL");
    }
    #[cfg(not(unix))]
    let _ = pgid;
}

fn kill_all(ctx: &CtxShared) {
    ctx.quit.store(true, Ordering::SeqCst);
    let pgids: Vec<u32> = {
        let mut list = ctx.children.lock().unwrap();
        let copy = list.clone();
        list.clear();
        copy
    };
    #[cfg(unix)]
    for pgid in &pgids {
        signal_group(*pgid, "TERM");
    }
    #[cfg(not(unix))]
    let _ = &pgids;
    std::thread::sleep(Duration::from_millis(400));
    #[cfg(unix)]
    for pgid in &pgids {
        signal_group(*pgid, "KILL");
    }
}

fn quit_now(ctx: &CtxShared) {
    kill_all(ctx);
    std::process::exit(0);
}

// ------------------------------------------------------------------
// State updates + throttled emission
// ------------------------------------------------------------------

static LAST_EMIT: Mutex<Option<Instant>> = Mutex::new(None);

fn emit_state(app: &AppHandle, ui: &Arc<Mutex<Ui>>, force: bool) {
    let now = Instant::now();
    {
        let mut last = LAST_EMIT.lock().unwrap();
        if !force {
            if let Some(t) = *last {
                if now.duration_since(t) < Duration::from_millis(150) {
                    return;
                }
            }
        }
        *last = Some(now);
    }
    let snapshot = ui.lock().unwrap().clone();
    let _ = app.emit("state", snapshot);
}

/// Appends to every open log file; the model track writes to BOTH
/// logs/installer.log and logs/local-model.log (the same file run.sh's
/// terminal flow produces, so `tail -f` and the fallback dashboard keep
/// working no matter who drove the install).
struct MultiSink(Vec<File>);

impl Write for MultiSink {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        for f in &mut self.1 {
            let _ = f.write_all(buf);
        }
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        for f in &mut self.1 {
            let _ = f.flush();
        }
        Ok(())
    }
}

type Sink = Arc<Mutex<dyn Write + Send>>;

fn open_sink(paths: &[PathBuf]) -> Sink {
    let files: Vec<File> = paths
        .iter()
        .filter_map(|p| OpenOptions::new().create(true).append(true).open(p).ok())
        .collect();
    Arc::new(Mutex::new(MultiSink(files)))
}

struct Tee {
    ui: Arc<Mutex<Ui>>,
    sink: Sink,
}

impl Tee {
    fn line(&self, text: &str) {
        {
            let mut ui = self.ui.lock().unwrap();
            ui.log.push(text.to_string());
            if ui.log.len() > 300 {
                let drain = ui.log.len() - 300;
                ui.log.drain(0..drain);
            }
        }
        let stamped = format!("{}  {}\n", ts(), text);
        let _ = self.sink.lock().unwrap().write_all(stamped.as_bytes());
    }
}

// ------------------------------------------------------------------
// Step definitions
// ------------------------------------------------------------------

struct CmdSpec {
    argv: Vec<String>,
    /// a soft command may fail without failing the step (pip upgrade)
    soft: bool,
    /// retry the command this many times on failure (pip network hiccups)
    retry: u8,
}

#[derive(Clone, Copy, PartialEq)]
enum ParseKind {
    Plain,
    Pip,
    Cargo,
    Setup,
    Models,
}

struct StepSpec {
    id: String,
    label: String,
    cmds: Vec<CmdSpec>,
    cwd: PathBuf,
    env: Vec<(String, String)>,
    /// false => a failure becomes a warning card and the install continues
    fatal: bool,
    hint: String,
    kind: ParseKind,
    /// written with "ok" after the step succeeds (run.sh reads the same stamps)
    stamp: Option<PathBuf>,
}

fn is_executable(path: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::metadata(path)
            .map(|m| m.is_file() && (m.permissions().mode() & 0o111) != 0)
            .unwrap_or(false)
    }
    #[cfg(not(unix))]
    {
        path.is_file()
    }
}

fn find_app_bin(desktop_dir: &Path) -> Option<PathBuf> {
    let candidates = [
        desktop_dir
            .join("src-tauri")
            .join("target")
            .join("release")
            .join("psd-ai-desktop"),
        desktop_dir.join("psd.ai"),
        PathBuf::from("/opt/psd-ai/psd-ai-desktop"),
        PathBuf::from("/usr/bin/psd-ai-desktop"),
    ];
    candidates.into_iter().find(|c| is_executable(c))
}

fn cmd(argv: &[&str]) -> CmdSpec {
    CmdSpec {
        argv: argv.iter().map(|s| s.to_string()).collect(),
        soft: false,
        retry: 0,
    }
}

fn build_plan(cfg: &Config) -> Vec<StepSpec> {
    let venvpy = cfg.venvpy.to_string_lossy().to_string();
    let mut steps: Vec<StepSpec> = Vec::new();

    // 1. venv
    if !is_executable(&cfg.venvpy) {
        steps.push(StepSpec {
            id: "venv".into(),
            label: "Python environment".into(),
            cmds: vec![cmd(&[&cfg.pycmd, "-m", "venv", "venv"])],
            cwd: cfg.app_dir.clone(),
            env: vec![],
            fatal: true,
            hint: "On Fedora this needs the pip bootstrap: sudo dnf install python3-pip python3-devel".into(),
            kind: ParseKind::Plain,
            stamp: None,
        });
    }

    // 2. dependencies (stamp shared with run.sh)
    let deps_stamp = cfg.app_dir.join("venv").join(".deps_ok");
    if !deps_stamp.exists() {
        let mut install = cmd(&[
            &venvpy, "-m", "pip", "install", "-r", "requirements.txt",
            "--disable-pip-version-check", "--no-input",
        ]);
        install.retry = 1; // transient network errors are common; run.sh retries too
        let upgrade = CmdSpec {
            argv: vec![
                venvpy.clone(), "-m".into(), "pip".into(), "install".into(),
                "--upgrade".into(), "pip".into(), "--quiet".into(),
                "--disable-pip-version-check".into(),
            ],
            soft: true,
            retry: 0,
        };
        steps.push(StepSpec {
            id: "deps".into(),
            label: "Python dependencies".into(),
            cmds: vec![upgrade, install],
            cwd: cfg.app_dir.clone(),
            env: vec![],
            fatal: true,
            hint: "Usually a network/proxy problem or a missing compiler - the pip error is listed below. Fix it and press Retry.".into(),
            kind: ParseKind::Pip,
            stamp: Some(deps_stamp),
        });
    }

    // 3. voice + PC-control extras (optional)
    let jarvis_req = cfg.app_dir.join("requirements-jarvis.txt");
    let jarvis_stamp = cfg.app_dir.join("venv").join(".jarvis_ok");
    if !cfg.no_voice && jarvis_req.is_file() && !jarvis_stamp.exists() {
        steps.push(StepSpec {
            id: "extras".into(),
            label: "Voice & PC-control extras".into(),
            cmds: vec![cmd(&[
                &venvpy, "-m", "pip", "install", "-r", "requirements-jarvis.txt",
                "--disable-pip-version-check", "--no-input",
            ])],
            cwd: cfg.app_dir.clone(),
            env: vec![],
            fatal: false,
            hint: "Optional: voice mode falls back to browser speech and PC control to the system binaries.".into(),
            kind: ParseKind::Pip,
            stamp: Some(jarvis_stamp),
        });
    }

    // 4. offline speech-to-text (optional, ~1 GB)
    let stt_stamp = cfg.app_dir.join("venv").join(".stt_ok");
    if !cfg.no_voice && !cfg.no_stt && !stt_stamp.exists() {
        steps.push(StepSpec {
            id: "stt".into(),
            label: "Offline speech-to-text (faster-whisper)".into(),
            cmds: vec![cmd(&[
                &venvpy, "-m", "pip", "install", "faster-whisper",
                "--disable-pip-version-check", "--no-input",
            ])],
            cwd: cfg.app_dir.clone(),
            env: vec![],
            fatal: false,
            hint: "Optional: without it the microphone uses the browser for speech.".into(),
            kind: ParseKind::Pip,
            stamp: Some(stt_stamp),
        });
    }

    // 5. first-time setup (idempotent, always runs)
    steps.push(StepSpec {
        id: "setup".into(),
        label: "First-time setup (folders, database, .env)".into(),
        cmds: vec![cmd(&[&venvpy, "setup.py"])],
        cwd: cfg.app_dir.clone(),
        env: vec![
            ("PSD_AI_DEFER_ADMIN".into(), "1".into()),
            ("PSD_AI_SKIP_RUN_HINT".into(), "1".into()),
        ],
        fatal: true,
        hint: "setup.py failed - its output is listed below. Re-run ./run.sh --repair if this persists.".into(),
        kind: ParseKind::Setup,
        stamp: None,
    });

    // 6. desktop app build (skipped when a built binary exists; a failure
    //    is a warning because the headless server is a full fallback)
    if find_app_bin(&cfg.desktop_dir).is_none() {
        let mut cmds = Vec::new();
        if !is_executable(&cfg.desktop_dir.join("node_modules").join(".bin").join("tauri")) {
            cmds.push(cmd(&["npm", "install", "--no-audit", "--no-fund"]));
        }
        cmds.push(cmd(&["npm", "run", "tauri", "build", "--", "--no-bundle"]));
        steps.push(StepSpec {
            id: "build".into(),
            label: "Desktop app (npm + Rust build, a few minutes)".into(),
            cmds,
            cwd: cfg.desktop_dir.clone(),
            env: vec![],
            fatal: false,
            hint: "psd.ai can still run headless in your browser. The compiler output is listed below - common causes are missing webkit2gtk4.1-devel packages or no network for cargo.".into(),
            kind: ParseKind::Cargo,
            stamp: None,
        });
    }

    steps
}

/// Display rows: the sequential plan plus the two special tracks, so the
/// window always shows the whole pipeline.
fn display_steps(plan: &[StepSpec], cfg: &Config) -> Vec<StepUi> {
    let mut rows: Vec<StepUi> = plan
        .iter()
        .map(|s| StepUi {
            id: s.id.clone(),
            label: s.label.clone(),
            state: "pending".into(),
            msg: String::new(),
            fatal: s.fatal,
        })
        .collect();
    rows.push(StepUi {
        id: "models".into(),
        label: "Local model group (parallel track)".into(),
        state: if cfg.no_models { "skipped".into() } else { "pending".into() },
        msg: String::new(),
        fatal: false,
    });
    rows.push(StepUi {
        id: "launch".into(),
        label: "Launch psd.ai".into(),
        state: "pending".into(),
        msg: String::new(),
        fatal: true,
    });
    rows
}

fn set_step(ui: &Arc<Mutex<Ui>>, id: &str, state: &str, msg: &str) {
    let mut ui = ui.lock().unwrap();
    if let Some(s) = ui.steps.iter_mut().find(|s| s.id == id) {
        s.state = state.to_string();
        if !msg.is_empty() {
            s.msg = msg.to_string();
        }
    }
}

fn push_fault(
    ui: &Arc<Mutex<Ui>>,
    kind: &str,
    step: &str,
    code: i64,
    msg: &str,
    hint: &str,
    lines: Vec<String>,
) {
    let mut ui = ui.lock().unwrap();
    ui.errors.push(ErrorUi {
        kind: kind.into(),
        step: step.into(),
        code,
        msg: msg.into(),
        hint: hint.into(),
        lines,
        ts: ts(),
    });
    if ui.errors.len() > 30 {
        ui.errors.remove(0);
    }
}

// ------------------------------------------------------------------
// Output parsing (manual, no regex crate)
// ------------------------------------------------------------------

const SUSPECT: &[&str] = &[
    "error",
    "failed",
    "fatal",
    "traceback",
    "command not found",
    "no space left",
    "permission denied",
    "panic",
];
const BENIGN: &[&str] = &[
    "0 failed",
    "--disable-pip-version-check",
    "warning:",
    "deprecat",
    "movedin20",
    "error-",
    "errors are listed",
];

fn suspicious(line: &str) -> bool {
    let low = line.to_lowercase();
    if BENIGN.iter().any(|b| low.contains(b)) {
        return false;
    }
    SUSPECT.iter().any(|s| low.contains(s))
}

/// "      Qwen3.5-9B-Q4_K_M.gguf:  42.1%  5.62/13.40 GB"
fn parse_gguf_progress(line: &str) -> Option<(String, f64, f64, f64)> {
    let bytes = line.trim();
    let ci = bytes.find(": ")?;
    let pi = bytes.find('%')?;
    if pi <= ci {
        return None;
    }
    let file = bytes[..ci].trim().to_string();
    if !file.contains(".gguf") {
        return None;
    }
    let pct: f64 = bytes[ci + 2..pi].trim().parse().ok()?;
    let rest = bytes[pi + 1..].trim().trim_end_matches("GB").trim();
    let mut parts = rest.split('/');
    let have: f64 = parts.next()?.trim().parse().ok()?;
    let total: f64 = parts.next()?.trim().parse().ok()?;
    Some((file, pct, have, total))
}

/// "  ==> Starting Qwen3.5 9B (chat) on port 8181 (ngl 99)..."
fn parse_starting(line: &str) -> Option<(String, u16)> {
    let start = line.find("Starting ")? + "Starting ".len();
    let rest = &line[start..];
    let mid = rest.find(" on port ")?;
    let label = rest[..mid].trim().to_string();
    let after = &rest[mid + " on port ".len()..];
    let digits: String = after.chars().take_while(|c| c.is_ascii_digit()).collect();
    Some((label, digits.parse().ok()?))
}

/// "  ==> Qwen3.5 9B (chat) ready at http://127.0.0.1:8181/v1"
fn parse_model_ready(line: &str) -> Option<String> {
    let start = line.find("==> ")? + 4;
    let rest = &line[start..];
    let mid = rest.find(" ready at ")?;
    Some(rest[..mid].trim().to_string())
}

/// "  ==> Local model group ready: 3 models on ports 8181, 8182, 8183"
fn parse_group_ready(line: &str) -> Option<u32> {
    let marker = "Local model group ready:";
    let start = line.find(marker)? + marker.len();
    let digits: String = line[start..]
        .chars()
        .take_while(|c| c.is_ascii_digit() || c.is_whitespace())
        .collect();
    digits.trim().parse().ok()
}

fn parse_compiling(line: &str) -> Option<String> {
    let t = line.trim();
    if !t.starts_with("Compiling ") {
        return None;
    }
    Some(t["Compiling ".len()..].split_whitespace().next()?.to_string())
}

fn handle_line(
    app: &AppHandle,
    tee: &Tee,
    ui: &Arc<Mutex<Ui>>,
    kind: ParseKind,
    line: &str,
    crate_count: &Arc<Mutex<u32>>,
    suspect_lines: &Arc<Mutex<Vec<String>>>,
) {
    tee.line(line);
    if suspicious(line) {
        let mut sl = suspect_lines.lock().unwrap();
        sl.push(line.trim().chars().take(240).collect());
        if sl.len() > 12 {
            sl.remove(0);
        }
    }
    {
        let mut u = ui.lock().unwrap();
        u.activity = line.trim().chars().take(160).collect();
        match kind {
            ParseKind::Pip => {
                let t = line.trim();
                if t.starts_with("Installing collected packages") {
                    u.step_pct = 0.85;
                } else if t.starts_with("Successfully installed") {
                    u.step_pct = 1.0;
                } else if t.starts_with("Collecting ") || t.starts_with("Downloading ") {
                    u.step_pct = (u.step_pct + 0.01).min(0.6);
                }
            }
            ParseKind::Cargo => {
                if let Some(crate_name) = parse_compiling(line) {
                    let mut c = crate_count.lock().unwrap();
                    *c += 1;
                    u.step_pct = ((*c as f64) / 480.0).min(0.95);
                    u.activity = format!("Compiling {} ({}/~480)", crate_name, *c);
                } else if line.contains("Finished") || line.contains("Built application") {
                    u.step_pct = 1.0;
                }
            }
            ParseKind::Setup => {
                if line.contains("[ok]") {
                    u.step_pct = (u.step_pct + 0.1).min(0.95);
                }
            }
            ParseKind::Models => {
                let m = &mut u.models;
                if let Some((file, pct, have, total)) = parse_gguf_progress(line) {
                    m.state = "downloading".into();
                    m.file = file;
                    m.pct = pct;
                    m.detail = format!("{:.2} / {:.2} GB", have, total);
                } else if let Some((label, port)) = parse_starting(line) {
                    m.state = "downloading".into();
                    m.detail = format!("starting {label}");
                    if !m.items.iter().any(|i| i.label == label) {
                        m.items.push(ModelItem {
                            label,
                            port,
                            state: "starting".into(),
                        });
                    }
                } else if let Some(label) = parse_model_ready(line) {
                    if let Some(item) = m.items.iter_mut().find(|i| i.label == label) {
                        item.state = "ready".into();
                    }
                    m.ready_count = m.items.iter().filter(|i| i.state == "ready").count() as u32;
                    m.detail = format!("{label} is ready");
                } else if let Some(n) = parse_group_ready(line) {
                    m.state = "ready".into();
                    m.pct = 100.0;
                    m.ready_count = n;
                    m.detail = format!("{n} models serving");
                    for item in m.items.iter_mut() {
                        item.state = "ready".into();
                    }
                } else if line.contains("Local model setup gave up") {
                    m.state = "failed".into();
                    m.error = line.trim().trim_start_matches("[warn]").trim().to_string();
                }
            }
            ParseKind::Plain => {}
        }
    }
    emit_state(app, ui, false);
}

// ------------------------------------------------------------------
// Process execution
// ------------------------------------------------------------------

struct Fail {
    code: i64,
    msg: String,
    lines: Vec<String>,
}

/// ~/.cargo/bin may hold cargo when rustup was used instead of dnf.
fn path_with_cargo_bin() -> Option<String> {
    let home = std::env::var("HOME").unwrap_or_default();
    let cargo_bin = format!("{home}/.cargo/bin");
    let path = std::env::var("PATH").unwrap_or_default();
    if Path::new(&cargo_bin).is_dir() && !path.contains(&cargo_bin) {
        Some(format!("{cargo_bin}:{path}"))
    } else {
        None
    }
}

fn spawn_child(
    cmdspec: &CmdSpec,
    cwd: &Path,
    env: &[(String, String)],
    children: &Arc<Mutex<Vec<u32>>>,
) -> Result<Child, String> {
    let mut c = Command::new(&cmdspec.argv[0]);
    c.args(&cmdspec.argv[1..])
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (k, v) in env {
        c.env(k, v);
    }
    if let Some(p) = path_with_cargo_bin() {
        c.env("PATH", p);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Own process group so kill_group reaches pip workers, npm, cargo
        // and every llama-server - exactly like run.sh's setsid + cleanup.
        c.process_group(0);
    }
    let child = c
        .spawn()
        .map_err(|e| format!("could not start {}: {e}", cmdspec.argv.join(" ")))?;
    register_child(children, child.id());
    Ok(child)
}

/// Run every command of a step, streaming both output streams through the
/// parser. Returns Err(Fail) on the first hard command failure.
fn exec_step(
    app: &AppHandle,
    ctx: &Arc<CtxShared>,
    spec: &StepSpec,
    tee: &Tee,
    pgid_slot: Option<&Arc<Mutex<Option<u32>>>>,
) -> Result<(), Fail> {
    let crate_count = Arc::new(Mutex::new(0u32));
    let suspect = Arc::new(Mutex::new(Vec::new()));
    let recent = Arc::new(Mutex::new(Vec::<String>::new()));

    {
        let mut ui = ctx.ui.lock().unwrap();
        ui.step_pct = 0.0;
        ui.step_id = spec.id.clone();
    }

    for cmdspec in &spec.cmds {
        let mut attempt = 0u8;
        loop {
            let mut child = match spawn_child(cmdspec, &spec.cwd, &spec.env, &ctx.children) {
                Ok(c) => c,
                Err(msg) => {
                    if cmdspec.soft {
                        tee.line(&format!("[warn] {msg}"));
                        break;
                    }
                    return Err(Fail {
                        code: -1,
                        msg,
                        lines: suspect.lock().unwrap().clone(),
                    });
                }
            };
            let pgid = child.id();
            if let Some(slot) = pgid_slot {
                *slot.lock().unwrap() = Some(pgid);
            }

            let streams: Vec<Box<dyn std::io::Read + Send>> = [
                child.stdout.take().map(|s| Box::new(s) as Box<dyn std::io::Read + Send>),
                child.stderr.take().map(|s| Box::new(s) as Box<dyn std::io::Read + Send>),
            ]
            .into_iter()
            .flatten()
            .collect();

            let mut joins = Vec::new();
            for stream in streams {
                let app_c = app.clone();
                let tee_c = Tee {
                    ui: ctx.ui.clone(),
                    sink: tee.sink.clone(),
                };
                let ui_c = ctx.ui.clone();
                let counters_c = crate_count.clone();
                let suspect_c = suspect.clone();
                let recent_c = recent.clone();
                let kind = spec.kind;
                joins.push(std::thread::spawn(move || {
                    for line in BufReader::new(stream).lines() {
                        let line = match line {
                            Ok(l) => l,
                            Err(_) => break,
                        };
                        {
                            let mut r = recent_c.lock().unwrap();
                            r.push(line.clone());
                            if r.len() > 40 {
                                r.remove(0);
                            }
                        }
                        handle_line(
                            &app_c, &tee_c, &ui_c, kind, &line, &counters_c, &suspect_c,
                        );
                    }
                }));
            }

            let status = child.wait();
            unregister_child(&ctx.children, pgid);
            if let Some(slot) = pgid_slot {
                *slot.lock().unwrap() = None;
            }
            for j in joins {
                let _ = j.join();
            }
            if status.as_ref().map(|s| s.success()).unwrap_or(false) {
                break;
            }
            if cmdspec.soft {
                tee.line(&format!(
                    "[warn] soft command failed ({}), continuing",
                    cmdspec.argv.join(" ")
                ));
                break;
            }
            if attempt < cmdspec.retry && !ctx.quit.load(Ordering::SeqCst) {
                attempt += 1;
                let msg = format!(
                    "{} failed - retrying ({}/{})",
                    cmdspec.argv.first().map(|s| s.as_str()).unwrap_or("command"),
                    attempt,
                    cmdspec.retry + 1
                );
                tee.line(&msg);
                set_step(&ctx.ui, &spec.id, "running", &msg);
                std::thread::sleep(Duration::from_secs(3));
                continue;
            }
            let code = status
                .as_ref()
                .map(|s| s.code().unwrap_or(-1) as i64)
                .unwrap_or(-1);
            let lines = {
                let s = suspect.lock().unwrap();
                if s.is_empty() {
                    let r = recent.lock().unwrap();
                    let start = r.len().saturating_sub(6);
                    r[start..].to_vec()
                } else {
                    s.clone()
                }
            };
            return Err(Fail {
                code,
                msg: format!("{} exited with code {}", cmdspec.argv.join(" "), code),
                lines,
            });
        }
        if ctx.quit.load(Ordering::SeqCst) {
            return Err(Fail {
                code: -1,
                msg: "installer is quitting".into(),
                lines: vec![],
            });
        }
    }

    if let Some(stamp) = &spec.stamp {
        let _ = std::fs::write(stamp, "ok");
    }
    Ok(())
}

// ------------------------------------------------------------------
// The model track (parallel thread, own child process)
// ------------------------------------------------------------------

fn spawn_models_track(app: AppHandle, ctx: Arc<CtxShared>, cfg: Arc<Config>) {
    set_step(&ctx.ui, "models", "running", "downloading & serving the model group");
    {
        let mut ui = ctx.ui.lock().unwrap();
        ui.models.state = "downloading".into();
    }
    *ctx.models_started.lock().unwrap() = Some(Instant::now());
    emit_state(&app, &ctx.ui, true);

    let spec = StepSpec {
        id: "models".into(),
        label: "Local model group".into(),
        cmds: vec![cmd(&[
            &cfg.venvpy.to_string_lossy(),
            "scripts/local_llama.py",
            "--port",
            &cfg.llama_port,
            "--foreground",
        ])],
        cwd: cfg.app_dir.clone(),
        env: vec![],
        fatal: false,
        hint: "psd.ai starts without local models; add one under Settings > Models, or press Retry on the model card.".into(),
        kind: ParseKind::Models,
        stamp: None,
    };

    // mirror model output to BOTH installer.log and local-model.log
    let sink = open_sink(&[
        cfg.log_dir.join("installer.log"),
        cfg.log_dir.join("local-model.log"),
    ]);

    std::thread::spawn(move || {
        let tee = Tee {
            ui: ctx.ui.clone(),
            sink,
        };
        let result = exec_step(&app, &ctx, &spec, &tee, Some(&ctx.models_pgid));
        let state = ctx.ui.lock().unwrap().models.state.clone();
        match result {
            Ok(()) => {
                if state != "ready" {
                    // exited 0 without the ready line (single-model mode etc.)
                    let mut ui = ctx.ui.lock().unwrap();
                    ui.models.state = "ready".into();
                    drop(ui);
                    set_step(&ctx.ui, "models", "done", "model group finished");
                } else {
                    let n = ctx.ui.lock().unwrap().models.ready_count;
                    set_step(&ctx.ui, "models", "done", &format!("{n} local models serving"));
                }
            }
            Err(fail) => {
                if state == "ready" {
                    // servers came up but the wrapper exited nonzero; still usable
                    set_step(&ctx.ui, "models", "done", "models serving");
                } else {
                    set_step(
                        &ctx.ui,
                        "models",
                        "warn",
                        "model group failed - psd.ai still starts",
                    );
                    {
                        let mut ui = ctx.ui.lock().unwrap();
                        ui.models.state = "failed".into();
                        if ui.models.error.is_empty() {
                            ui.models.error = fail.msg.clone();
                        }
                    }
                    push_fault(
                        &ctx.ui,
                        "warn",
                        "Local model group",
                        fail.code,
                        &fail.msg,
                        &spec.hint,
                        fail.lines,
                    );
                }
            }
        }
        emit_state(&app, &ctx.ui, true);
    });
}

fn kill_models(ctx: &CtxShared) {
    let pgid = ctx.models_pgid.lock().unwrap().take();
    if let Some(pgid) = pgid {
        kill_group(pgid);
        unregister_child(&ctx.children, pgid);
    }
}

// ------------------------------------------------------------------
// The runner thread (the install state machine)
// ------------------------------------------------------------------

fn runner(
    app: AppHandle,
    ctx: Arc<CtxShared>,
    cfg: Arc<Config>,
    rx: Receiver<Action>,
    main_sink: Sink,
) {
    let plan = build_plan(&cfg);
    {
        let mut ui = ctx.ui.lock().unwrap();
        ui.steps = display_steps(&plan, &cfg);
    }
    emit_state(&app, &ctx.ui, true);

    let tee = Tee {
        ui: ctx.ui.clone(),
        sink: main_sink,
    };
    let wants_models = !cfg.no_models;
    let mut models_spawned = false;
    let deps_planned = plan.iter().any(|s| s.id == "deps");

    // The model bootstrap registers endpoints through core.database, so it
    // needs the venv dependencies: it starts right after the deps step (or
    // immediately when this run skipped deps because .deps_ok exists).
    if wants_models && !deps_planned {
        spawn_models_track(app.clone(), ctx.clone(), cfg.clone());
        models_spawned = true;
    }

    let mut idx = 0usize;
    'steps: loop {
        if ctx.quit.load(Ordering::SeqCst) {
            return;
        }
        if idx >= plan.len() {
            break;
        }
        let spec = &plan[idx];
        set_step(&ctx.ui, &spec.id, "running", "");
        {
            let mut ui = ctx.ui.lock().unwrap();
            ui.phase = "installing".into();
            ui.headline = format!("{}...", spec.label);
        }
        emit_state(&app, &ctx.ui, true);

        match exec_step(&app, &ctx, spec, &tee, None) {
            Ok(()) => {
                set_step(&ctx.ui, &spec.id, "done", "");
                emit_state(&app, &ctx.ui, true);
                if spec.id == "deps" && wants_models && !models_spawned {
                    spawn_models_track(app.clone(), ctx.clone(), cfg.clone());
                    models_spawned = true;
                }
                idx += 1;
            }
            Err(fail) => {
                push_fault(
                    &ctx.ui,
                    if spec.fatal { "error" } else { "warn" },
                    &spec.label,
                    fail.code,
                    &fail.msg,
                    &spec.hint,
                    fail.lines,
                );
                if !spec.fatal {
                    set_step(&ctx.ui, &spec.id, "warn", "failed (optional) - continuing");
                    emit_state(&app, &ctx.ui, true);
                    idx += 1;
                    continue;
                }
                set_step(&ctx.ui, &spec.id, "failed", &fail.msg);
                {
                    let mut ui = ctx.ui.lock().unwrap();
                    ui.phase = "failed".into();
                    ui.fatal_step = spec.id.clone();
                    ui.headline = format!("{} failed - see the fault list", spec.label);
                }
                emit_state(&app, &ctx.ui, true);
                // wait for Retry / Skip / Quit
                loop {
                    match rx.recv() {
                        Ok(Action::Retry(id)) if id == spec.id => {
                            let mut ui = ctx.ui.lock().unwrap();
                            ui.errors.retain(|e| e.step != spec.label);
                            ui.fatal_step = String::new();
                            drop(ui);
                            continue 'steps;
                        }
                        Ok(Action::Skip(id)) if id == spec.id => {
                            set_step(&ctx.ui, &spec.id, "skipped", "skipped by you");
                            idx += 1;
                            continue 'steps;
                        }
                        Ok(Action::Quit) => quit_now(&ctx),
                        Ok(_) => {}
                        Err(_) => return,
                    }
                }
            }
        }
    }

    // ---- all sequential steps done: wait for the model track ----
    if wants_models && models_spawned {
        {
            let mut ui = ctx.ui.lock().unwrap();
            ui.phase = "waiting_models".into();
            ui.headline = "Waiting for the local model group...".into();
        }
        emit_state(&app, &ctx.ui, true);
        loop {
            let mstate = ctx.ui.lock().unwrap().models.state.clone();
            if mstate == "ready" || mstate == "failed" {
                break;
            }
            let started = *ctx.models_started.lock().unwrap();
            let timed_out = match started {
                Some(t) => t.elapsed() > Duration::from_secs(cfg.model_wait_secs),
                None => false,
            };
            if timed_out {
                {
                    let mut ui = ctx.ui.lock().unwrap();
                    ui.models.state = "timeout".into();
                }
                set_step(
                    &ctx.ui,
                    "models",
                    "warn",
                    "still downloading in the background",
                );
                push_fault(
                    &ctx.ui,
                    "warn",
                    "Local model group",
                    0,
                    "The download is taking longer than the wait budget.",
                    "psd.ai starts anyway; the group registers itself as soon as it finishes. Watch the model card here or logs/local-model.log.",
                    vec![],
                );
                emit_state(&app, &ctx.ui, true);
                break;
            }
            match rx.recv_timeout(Duration::from_millis(400)) {
                Ok(Action::Quit) => quit_now(&ctx),
                Ok(Action::Retry(id)) if id == "models" => {
                    let s = ctx.ui.lock().unwrap().models.state.clone();
                    if s == "failed" || s == "timeout" {
                        spawn_models_track(app.clone(), ctx.clone(), cfg.clone());
                    }
                }
                Err(RecvTimeoutError::Disconnected) => return,
                _ => {}
            }
        }
    }

    // ---- ready ----
    {
        let mut ui = ctx.ui.lock().unwrap();
        ui.phase = "ready".into();
        ui.can_launch = true;
        ui.headline = "psd.ai is ready.".into();
        ui.activity = String::new();
    }
    set_step(&ctx.ui, "launch", "pending", "press Launch");
    emit_state(&app, &ctx.ui, true);

    loop {
        match rx.recv() {
            Ok(Action::Launch) => {
                if do_launch(&app, &ctx, &cfg) {
                    supervise_app(&app, &ctx, &rx);
                }
            }
            Ok(Action::Retry(id)) if id == "models" => {
                let s = ctx.ui.lock().unwrap().models.state.clone();
                if s == "failed" || s == "timeout" {
                    spawn_models_track(app.clone(), ctx.clone(), cfg.clone());
                }
            }
            Ok(Action::Quit) => quit_now(&ctx),
            Err(_) => return,
            _ => {}
        }
    }
}

fn do_launch(app: &AppHandle, ctx: &Arc<CtxShared>, cfg: &Config) -> bool {
    set_step(&ctx.ui, "launch", "running", "starting psd.ai");
    emit_state(app, &ctx.ui, true);

    let bin = find_app_bin(&cfg.desktop_dir);
    let mut headless_url: Option<String> = None;

    let spawn_result: Result<Child, String> = match &bin {
        Some(bin) => {
            let mut c = Command::new(bin);
            c.env("PSD_AI_APP_DIR", &cfg.app_dir)
                .env("PSD_AI_PYTHON", &cfg.venvpy)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            #[cfg(unix)]
            {
                use std::os::unix::process::CommandExt;
                c.process_group(0);
            }
            c.spawn().map_err(|e| format!("{}: {e}", bin.display()))
        }
        None => Err("no built desktop app found".into()),
    };

    let child = match spawn_result {
        Ok(child) => child,
        Err(first_err) => {
            if bin.is_some() {
                push_fault(
                    &ctx.ui,
                    "warn",
                    "Launch psd.ai",
                    -1,
                    &format!("desktop app failed to start ({first_err}) - falling back to the headless server"),
                    "The window app can be rebuilt later with: cd desktop && npm run tauri build",
                    vec![],
                );
            }
            // headless fallback: serve the web app on loopback
            let mut c = Command::new(&cfg.venvpy);
            c.args([
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                cfg.app_bind.as_str(),
                "--port",
                cfg.app_port.as_str(),
            ])
            .current_dir(&cfg.app_dir)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
            #[cfg(unix)]
            {
                use std::os::unix::process::CommandExt;
                c.process_group(0);
            }
            match c.spawn() {
                Ok(child) => {
                    headless_url = Some(format!("http://localhost:{}", cfg.app_port));
                    child
                }
                Err(e) => {
                    push_fault(
                        &ctx.ui,
                        "error",
                        "Launch psd.ai",
                        -1,
                        &format!("could not start the headless server either: {e}"),
                        "Check that psd.ai/venv exists (Python dependencies step) and that the port is free.",
                        vec![],
                    );
                    set_step(&ctx.ui, "launch", "failed", "could not start psd.ai");
                    emit_state(app, &ctx.ui, true);
                    return false;
                }
            }
        }
    };

    let pgid = child.id();
    register_child(&ctx.children, pgid);
    *ctx.app_pgid.lock().unwrap() = Some(pgid);
    {
        let mut ui = ctx.ui.lock().unwrap();
        ui.app_running = true;
        ui.phase = "running_app".into();
        ui.app_url = headless_url.clone().unwrap_or_default();
        ui.headline = match &headless_url {
            Some(url) => format!("Running headless - the app is open in your browser ({url})"),
            None => "psd.ai is running - close its window to stop.".into(),
        };
    }
    if let Some(url) = &headless_url {
        let _ = Command::new("xdg-open")
            .arg(url)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();
    }
    emit_state(app, &ctx.ui, true);

    // monitor exit in its own thread so the runner stays responsive
    let mut child = child;
    let app_c = app.clone();
    let ctx_c = ctx.clone();
    std::thread::spawn(move || {
        let _ = child.wait();
        unregister_child(&ctx_c.children, pgid);
        *ctx_c.app_pgid.lock().unwrap() = None;
        kill_models(&ctx_c);
        {
            let mut ui = ctx_c.ui.lock().unwrap();
            ui.app_running = false;
            ui.phase = "closed".into();
            ui.can_launch = true;
            ui.headline = "psd.ai has closed. Relaunch it or quit the installer.".into();
        }
        set_step(&ctx_c.ui, "launch", "done", "app closed");
        emit_state(&app_c, &ctx_c.ui, true);
    });
    true
}

fn supervise_app(app: &AppHandle, ctx: &CtxShared, rx: &Receiver<Action>) {
    loop {
        let phase = ctx.ui.lock().unwrap().phase.clone();
        if phase != "running_app" {
            return; // app closed (the monitor thread flipped the phase)
        }
        match rx.recv_timeout(Duration::from_millis(300)) {
            Ok(Action::Stop) => {
                let pgid = ctx.app_pgid.lock().unwrap().take();
                if let Some(pgid) = pgid {
                    kill_group(pgid);
                    unregister_child(&ctx.children, pgid);
                }
                kill_models(ctx);
                {
                    let mut ui = ctx.ui.lock().unwrap();
                    ui.app_running = false;
                    ui.phase = "closed".into();
                    ui.headline = "Stopped. Relaunch psd.ai or quit the installer.".into();
                }
                set_step(&ctx.ui, "launch", "done", "stopped");
                emit_state(app, &ctx.ui, true);
                return;
            }
            Ok(Action::Quit) => quit_now(ctx),
            Err(RecvTimeoutError::Disconnected) => return,
            _ => {}
        }
    }
}

// ------------------------------------------------------------------
// Tauri commands
// ------------------------------------------------------------------

#[tauri::command]
fn get_state(ctx: State<'_, Arc<CtxShared>>) -> Ui {
    ctx.ui.lock().unwrap().clone()
}

#[tauri::command]
fn send_action(
    action: String,
    step: Option<String>,
    ctx: State<'_, Arc<CtxShared>>,
) -> Result<(), String> {
    let a = match action.as_str() {
        "retry" => Action::Retry(step.unwrap_or_default()),
        "skip" => Action::Skip(step.unwrap_or_default()),
        "launch" => Action::Launch,
        "stop" => Action::Stop,
        "quit" => Action::Quit,
        other => return Err(format!("unknown action: {other}")),
    };
    let tx = ctx.tx.lock().unwrap();
    tx.send(a).map_err(|e| e.to_string())
}

// ------------------------------------------------------------------
// Bootstrap
// ------------------------------------------------------------------

fn main() {
    let cfg = Arc::new(load_config());
    let ui = Arc::new(Mutex::new(initial_ui()));
    let (tx, rx) = channel::<Action>();
    let shared = Arc::new(CtxShared {
        ui: ui.clone(),
        tx: Arc::new(Mutex::new(tx)),
        children: Arc::new(Mutex::new(Vec::new())),
        models_pgid: Arc::new(Mutex::new(None)),
        app_pgid: Arc::new(Mutex::new(None)),
        models_started: Arc::new(Mutex::new(None)),
        quit: Arc::new(AtomicBool::new(false)),
    });

    std::fs::create_dir_all(&cfg.log_dir).ok();
    let main_sink = open_sink(&[cfg.log_dir.join("installer.log")]);

    let shared_setup = shared.clone();
    let cfg_setup = cfg.clone();
    let sink_setup = main_sink.clone();
    let shared_close = shared.clone();
    let shared_exit = shared.clone();

    tauri::Builder::default()
        .manage(shared.clone())
        .invoke_handler(tauri::generate_handler![get_state, send_action])
        .setup(move |app| {
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                runner(handle, shared_setup, cfg_setup, rx, sink_setup)
            });
            Ok(())
        })
        .on_window_event(move |_window, event| {
            if let tauri::WindowEvent::CloseRequested { .. } = event {
                quit_now(&shared_close);
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building the psd.ai installer")
        .run(move |_app, event| {
            if let tauri::RunEvent::Exit = event {
                kill_all(&shared_exit);
            }
        });
}

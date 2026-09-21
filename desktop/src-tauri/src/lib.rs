//! psd.ai desktop shell.
//!
//! Responsibilities:
//!   1. Spawn the Python backend (`psd.ai/desktop_server.py`) as a private
//!      sidecar bound to a random loopback port.
//!   2. Read the `PSD_AI_READY port=NNNN` handshake from its stdout.
//!   3. Expose two IPC commands to the React frontend – `api_request`
//!      (request/response JSON, multipart or raw) and `api_stream`
//!      (server-sent events forwarded as Tauri events) – so the WebView
//!      never touches a URL: the frontend calls Python through IPC only.
//!   4. Kill the sidecar when the last window closes.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::Engine;
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager, State};

// ------------------------------------------------------------------
// Sidecar lifecycle
// ------------------------------------------------------------------

struct Backend {
    child: Mutex<Option<Child>>,
    port: Mutex<Option<u16>>,
    status: Mutex<String>,
    log: Mutex<Vec<String>>,
    client: reqwest::Client,
}

impl Backend {
    fn new() -> Self {
        Self {
            child: Mutex::new(None),
            port: Mutex::new(None),
            status: Mutex::new("starting".into()),
            log: Mutex::new(Vec::new()),
            client: reqwest::Client::builder()
                .cookie_store(true)
                .timeout(Duration::from_secs(600))
                .build()
                .expect("http client"),
        }
    }

    fn base(&self) -> Result<String, String> {
        match *self.port.lock().unwrap() {
            Some(p) => Ok(format!("http://127.0.0.1:{}", p)),
            None => Err("backend not ready".into()),
        }
    }

    fn push_log(&self, line: String) {
        let mut log = self.log.lock().unwrap();
        log.push(line);
        if log.len() > 400 {
            let drain = log.len() - 400;
            log.drain(0..drain);
        }
    }

    fn kill(&self) {
        if let Some(mut child) = self.child.lock().unwrap().take() {
            // Closing stdin tells desktop_server.py to exit on its own;
            // signals are the fallback.
            drop(child.stdin.take());
            std::thread::sleep(Duration::from_millis(300));

            // The sidecar leads its own process group (see spawn_backend), so
            // its pid is the group id: TERM the group first to give the model
            // server a chance to release the GPU, then SIGKILL whatever is
            // left, then reap the direct child.
            let pid = child.id();
            #[cfg(unix)]
            if pid > 0 {
                signal_group(pid, "TERM");
                std::thread::sleep(Duration::from_millis(400));
                signal_group(pid, "KILL");
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// Find the `psd.ai` project directory. Order:
///   PSD_AI_APP_DIR env → next to the executable → repo layout during `tauri dev`.
fn find_app_dir() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("PSD_AI_APP_DIR") {
        let p = PathBuf::from(p);
        if p.join("desktop_server.py").exists() {
            return Some(p);
        }
    }
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent().map(Path::to_path_buf);
        for _ in 0..6 {
            if let Some(d) = dir.clone() {
                candidates.push(d.join("psd.ai"));
                candidates.push(d.clone());
                dir = d.parent().map(Path::to_path_buf);
            }
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd.join("psd.ai"));
        candidates.push(cwd.join("..").join("psd.ai"));
        candidates.push(cwd.join("..").join("..").join("psd.ai"));
    }
    candidates
        .into_iter()
        .find(|c| c.join("desktop_server.py").exists())
}

/// Find the interpreter that should run the sidecar.
///
/// Order: `PSD_AI_PYTHON` (an explicit override always wins, which is how the
/// RPM and a dev checkout can disagree without either breaking), then the
/// project venv, then the packaged venv the RPM installs, then `python3`.
/// Every filesystem candidate has to be an executable file — a venv left
/// half-built by an interrupted `run.sh` is exactly the case that must not be
/// picked up.
fn find_python(app_dir: &Path) -> Vec<String> {
    if let Ok(p) = std::env::var("PSD_AI_PYTHON") {
        if !p.trim().is_empty() {
            return vec![p];
        }
    }
    let candidates = [
        app_dir.join("venv").join("bin").join("python"),
        app_dir.join("venv").join("bin").join("python3"),
        app_dir.join(".venv").join("bin").join("python"),
        // packaging/psd-ai.spec installs the venv here, out of $HOME.
        PathBuf::from("/usr/lib/psd.ai/venv/bin/python"),
        PathBuf::from("/usr/bin/python3"),
    ];
    for candidate in candidates {
        if is_executable(&candidate) {
            return vec![candidate.to_string_lossy().to_string()];
        }
    }
    vec!["python3".into()]
}

/// Signal every process in `pgid` (negative pid = process group).
///
/// `kill(1)` is used rather than libc so the desktop crate keeps no unsafe
/// dependencies; failures are ignored because the group is usually already
/// gone by the time this runs.
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

/// True when `path` is a regular file with at least one execute bit set.
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .map(|m| m.is_file() && (m.permissions().mode() & 0o111) != 0)
        .unwrap_or(false)
}

fn spawn_backend(app: AppHandle) {
    let backend = app.state::<Arc<Backend>>().inner().clone();
    std::thread::spawn(move || {
        let Some(app_dir) = find_app_dir() else {
            *backend.status.lock().unwrap() = "error".into();
            backend.push_log("Could not locate the psd.ai folder (desktop_server.py).".into());
            let _ = app.emit("backend-status", "error");
            return;
        };
        let py = find_python(&app_dir);
        backend.push_log(format!("app dir: {}", app_dir.display()));
        backend.push_log(format!("python: {}", py.join(" ")));

        let mut cmd = Command::new(&py[0]);
        for a in &py[1..] {
            cmd.arg(a);
        }
        cmd.arg("desktop_server.py")
            .current_dir(&app_dir)
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONIOENCODING", "utf-8")
            // See desktop_server.py: one BLAS thread per process. numpy,
            // OpenBLAS and llama.cpp each spawn their own thread pool, and
            // left alone they oversubscribe the cores and fight the model
            // server for the same CPUs.
            .env("OPENBLAS_NUM_THREADS", "1")
            .env("OMP_NUM_THREADS", "1")
            .env("MKL_NUM_THREADS", "1")
            .env("OPENBLAS_MAIN_FREE", "1")
            .env("TOKENIZERS_PARALLELISM", "false")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            // Give the sidecar its own process group. desktop_server.py starts
            // a llama.cpp server of its own, and without this that grandchild
            // survives the window closing and keeps the VRAM.
            cmd.process_group(0);
        }

        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                *backend.status.lock().unwrap() = "error".into();
                backend.push_log(format!("failed to start python: {e}"));
                let _ = app.emit("backend-status", "error");
                return;
            }
        };

        let stdout = child.stdout.take().unwrap();
        let stderr = child.stderr.take().unwrap();
        *backend.child.lock().unwrap() = Some(child);

        // stderr → log buffer (uvicorn logs there)
        {
            let b = backend.clone();
            let a = app.clone();
            std::thread::spawn(move || {
                for line in BufReader::new(stderr).lines().flatten() {
                    b.push_log(line.clone());
                    let _ = a.emit("backend-log", line);
                }
            });
        }

        // stdout → handshake + log
        for line in BufReader::new(stdout).lines().flatten() {
            if let Some(rest) = line.strip_prefix("PSD_AI_READY port=") {
                if let Ok(port) = rest.trim().parse::<u16>() {
                    *backend.port.lock().unwrap() = Some(port);
                    backend.push_log(format!("backend ready on loopback port {port}"));
                    // wait for /api/health so the first IPC call never races uvicorn
                    // Use a throwaway client on a throwaway runtime here so the
                    // long-lived IPC client's connection pool never belongs to a
                    // runtime that is about to be dropped.
                    let base = format!("http://127.0.0.1:{port}");
                    let rt = tokio::runtime::Runtime::new().unwrap();
                    rt.block_on(async {
                        let client = reqwest::Client::new();
                        for _ in 0..600 {
                            if client.get(format!("{base}/api/health")).send().await.is_ok() {
                                break;
                            }
                            tokio::time::sleep(Duration::from_millis(250)).await;
                        }
                    });
                    *backend.status.lock().unwrap() = "ready".into();
                    let _ = app.emit("backend-status", "ready");
                    continue;
                }
            }
            backend.push_log(line.clone());
            let _ = app.emit("backend-log", line);
        }
        // stdout closed → process exited
        if backend.status.lock().unwrap().as_str() != "stopping" {
            *backend.status.lock().unwrap() = "error".into();
            backend.push_log("backend process exited".into());
            let _ = app.emit("backend-status", "error");
        }
    });
}

// ------------------------------------------------------------------
// IPC commands
// ------------------------------------------------------------------

#[derive(Deserialize)]
pub struct FilePart {
    field: String,
    name: String,
    mime: Option<String>,
    /// base64 encoded bytes
    data: String,
}

#[derive(Deserialize)]
pub struct ApiRequest {
    method: String,
    path: String,
    #[serde(default)]
    json: Option<serde_json::Value>,
    /// multipart/form-data text fields
    #[serde(default)]
    form: Option<HashMap<String, String>>,
    #[serde(default)]
    files: Option<Vec<FilePart>>,
    #[serde(default)]
    headers: Option<HashMap<String, String>>,
}

#[derive(Serialize)]
pub struct ApiResponse {
    status: u16,
    headers: HashMap<String, String>,
    /// JSON body when parseable, else `null`
    json: serde_json::Value,
    /// raw text body (empty for binary)
    text: String,
    /// base64 body for binary responses (images, downloads)
    base64: Option<String>,
}

fn build_request(
    backend: &Backend,
    req: &ApiRequest,
) -> Result<reqwest::RequestBuilder, String> {
    let base = backend.base()?;
    let url = format!("{base}{}", req.path);
    let method = reqwest::Method::from_bytes(req.method.to_uppercase().as_bytes())
        .map_err(|e| e.to_string())?;
    let mut rb = backend.client.request(method, url);
    if let Some(h) = &req.headers {
        for (k, v) in h {
            rb = rb.header(k, v);
        }
    }
    if let Some(files) = &req.files {
        let mut mp = reqwest::multipart::Form::new();
        if let Some(form) = &req.form {
            for (k, v) in form {
                mp = mp.text(k.clone(), v.clone());
            }
        }
        for f in files {
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(&f.data)
                .map_err(|e| e.to_string())?;
            let mut part = reqwest::multipart::Part::bytes(bytes).file_name(f.name.clone());
            if let Some(m) = &f.mime {
                part = part.mime_str(m).map_err(|e| e.to_string())?;
            }
            mp = mp.part(f.field.clone(), part);
        }
        rb = rb.multipart(mp);
    } else if let Some(form) = &req.form {
        let mut mp = reqwest::multipart::Form::new();
        for (k, v) in form {
            mp = mp.text(k.clone(), v.clone());
        }
        rb = rb.multipart(mp);
    } else if let Some(j) = &req.json {
        rb = rb.json(j);
    }
    Ok(rb)
}

#[tauri::command]
async fn api_request(
    backend: State<'_, Arc<Backend>>,
    req: ApiRequest,
) -> Result<ApiResponse, String> {
    let rb = build_request(&backend, &req)?;
    let resp = rb.send().await.map_err(|e| e.to_string())?;
    let status = resp.status().as_u16();
    let mut headers = HashMap::new();
    for (k, v) in resp.headers() {
        headers.insert(k.to_string(), v.to_str().unwrap_or("").to_string());
    }
    let ctype = headers.get("content-type").cloned().unwrap_or_default();
    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    let is_text = ctype.starts_with("text/")
        || ctype.contains("json")
        || ctype.contains("xml")
        || ctype.contains("javascript")
        || ctype.is_empty();
    if is_text {
        let text = String::from_utf8_lossy(&bytes).to_string();
        let json = serde_json::from_str(&text).unwrap_or(serde_json::Value::Null);
        Ok(ApiResponse { status, headers, json, text, base64: None })
    } else {
        Ok(ApiResponse {
            status,
            headers,
            json: serde_json::Value::Null,
            text: String::new(),
            base64: Some(base64::engine::general_purpose::STANDARD.encode(&bytes)),
        })
    }
}

#[derive(Serialize, Clone)]
struct StreamEvent {
    id: String,
    kind: String, // "open" | "data" | "error" | "done"
    data: String,
    status: u16,
    run_id: String,
}

/// Forward an SSE endpoint as Tauri events named `api-stream-<id>`.
#[tauri::command]
async fn api_stream(
    app: AppHandle,
    backend: State<'_, Arc<Backend>>,
    id: String,
    req: ApiRequest,
) -> Result<(), String> {
    let event = format!("api-stream-{id}");
    let rb = build_request(&backend, &req)?;
    let resp = match rb.send().await {
        Ok(r) => r,
        Err(e) => {
            let _ = app.emit(&event, StreamEvent { id, kind: "error".into(), data: e.to_string(), status: 0, run_id: String::new() });
            return Ok(());
        }
    };
    let status = resp.status().as_u16();
    let run_id = resp
        .headers()
        .get("x-psd.ai-run-id")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let _ = app.emit(&event, StreamEvent { id: id.clone(), kind: "open".into(), data: String::new(), status, run_id: run_id.clone() });

    if status >= 400 {
        let body = resp.text().await.unwrap_or_default();
        let _ = app.emit(&event, StreamEvent { id: id.clone(), kind: "error".into(), data: body, status, run_id });
        return Ok(());
    }

    let mut stream = resp.bytes_stream();
    let mut buf = String::new();
    while let Some(chunk) = stream.next().await {
        match chunk {
            Ok(bytes) => {
                buf.push_str(&String::from_utf8_lossy(&bytes));
                // SSE frames are separated by a blank line
                while let Some(idx) = buf.find("\n\n") {
                    let frame = buf[..idx].to_string();
                    buf = buf[idx + 2..].to_string();
                    let mut ev_name = String::new();
                    let mut data_lines: Vec<&str> = Vec::new();
                    for line in frame.lines() {
                        if let Some(d) = line.strip_prefix("data:") {
                            data_lines.push(d.strip_prefix(' ').unwrap_or(d));
                        } else if let Some(n) = line.strip_prefix("event:") {
                            ev_name = n.trim().to_string();
                        }
                        // comment lines (": heartbeat") are ignored
                    }
                    if data_lines.is_empty() {
                        continue;
                    }
                    let data = data_lines.join("\n");
                    let kind = if ev_name == "error" { "error" } else { "data" };
                    let _ = app.emit(&event, StreamEvent { id: id.clone(), kind: kind.into(), data, status, run_id: run_id.clone() });
                }
            }
            Err(e) => {
                let _ = app.emit(&event, StreamEvent { id: id.clone(), kind: "error".into(), data: e.to_string(), status, run_id: run_id.clone() });
                break;
            }
        }
    }
    let _ = app.emit(&event, StreamEvent { id, kind: "done".into(), data: String::new(), status, run_id });
    Ok(())
}

#[derive(Serialize)]
struct BackendInfo {
    status: String,
    log: Vec<String>,
}

#[tauri::command]
fn backend_status(backend: State<'_, Arc<Backend>>) -> BackendInfo {
    BackendInfo {
        status: backend.status.lock().unwrap().clone(),
        log: backend.log.lock().unwrap().clone(),
    }
}

#[tauri::command]
fn restart_backend(app: AppHandle, backend: State<'_, Arc<Backend>>) {
    *backend.status.lock().unwrap() = "stopping".into();
    backend.kill();
    *backend.port.lock().unwrap() = None;
    *backend.status.lock().unwrap() = "starting".into();
    let _ = app.emit("backend-status", "starting");
    spawn_backend(app);
}

// ------------------------------------------------------------------
// App bootstrap
// ------------------------------------------------------------------

pub fn run() {
    let backend = Arc::new(Backend::new());
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(backend.clone())
        .invoke_handler(tauri::generate_handler![
            api_request,
            api_stream,
            backend_status,
            restart_backend
        ])
        .setup(|app| {
            spawn_backend(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if window.app_handle().webview_windows().is_empty() {
                    let b = window.app_handle().state::<Arc<Backend>>();
                    *b.status.lock().unwrap() = "stopping".into();
                    b.kill();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building psd.ai desktop")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                let b = app.state::<Arc<Backend>>();
                *b.status.lock().unwrap() = "stopping".into();
                b.kill();
                let _ = std::io::stdout().flush();
            }
        });
}

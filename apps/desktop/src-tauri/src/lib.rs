use std::fs;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use base64::Engine;
use serde::Deserialize;
use serde_json::{json, Value};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::sync::Mutex;

mod browser;
mod os;

const STDOUT_LIMIT: usize = 200_000;
const FILE_TEXT_LIMIT: usize = 80_000;
const PUBLISH_LIMIT: usize = 8 * 1024 * 1024;

struct Sidecar {
    token: Mutex<Option<String>>,
    api_base: Mutex<Option<String>>,
    chrome: Mutex<Option<browser::Chrome>>,
    started: AtomicBool,
}

fn lumio_url() -> String {
    if let Ok(url) = std::env::var("LUMIO_URL") {
        if !url.trim().is_empty() {
            return url.trim().to_string();
        }
    }
    if cfg!(debug_assertions) {
        "http://localhost:3000".into()
    } else {
        "https://checkme-web-kw68.onrender.com".into()
    }
}

fn jail_dir() -> PathBuf {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
    let jail = home.join("Lumio");
    let _ = fs::create_dir_all(&jail);
    jail
}

fn strip_unc(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    PathBuf::from(text.strip_prefix(r"\\?\").unwrap_or(&text))
}

fn home_dir() -> PathBuf {
    dirs::home_dir().unwrap_or_else(|| PathBuf::from("."))
}

fn resolve_under(root: PathBuf, raw: &str, err: &str) -> Result<PathBuf, String> {
    let root = strip_unc(&root.canonicalize().unwrap_or(root));
    let given = raw.trim();
    let path = if given.is_empty() || given == "." {
        root.clone()
    } else {
        let candidate = PathBuf::from(given);
        if candidate.is_absolute() {
            candidate
        } else {
            root.join(candidate)
        }
    };
    let resolved = if path.exists() {
        strip_unc(&path.canonicalize().map_err(|e| e.to_string())?)
    } else {
        let parent = path.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(&root);
        let _ = fs::create_dir_all(parent);
        let parent_c = strip_unc(&parent.canonicalize().map_err(|e| e.to_string())?);
        parent_c.join(path.file_name().unwrap_or_default())
    };
    if !resolved.starts_with(&root) {
        return Err(err.into());
    }
    Ok(resolved)
}

fn resolve_in_jail(raw: &str) -> Result<PathBuf, String> {
    resolve_under(jail_dir(), raw, "path is outside ~/Lumio")
}

fn resolve_in_home(raw: &str) -> Result<PathBuf, String> {
    resolve_under(home_dir(), raw, "path is outside your home folder")
}

fn clip(text: String, limit: usize) -> String {
    if text.len() <= limit {
        text
    } else {
        let mut cut = text;
        cut.truncate(limit);
        cut.push_str("\n…truncated");
        cut
    }
}

fn run_list_dir(args: &Value) -> Value {
    let path = args.get("path").and_then(Value::as_str).unwrap_or("");
    match resolve_in_home(path) {
        Ok(dir) => match fs::read_dir(&dir) {
            Ok(entries) => {
                let mut names: Vec<String> = entries
                    .filter_map(|entry| entry.ok())
                    .map(|entry| {
                        let name = entry.file_name().to_string_lossy().into_owned();
                        if entry.path().is_dir() {
                            format!("{name}/")
                        } else {
                            name
                        }
                    })
                    .collect();
                names.sort();
                json!({ "ok": true, "path": dir.to_string_lossy(), "entries": names, "ran_on": "local_computer" })
            }
            Err(err) => json!({ "ok": false, "error": err.to_string() }),
        },
        Err(err) => json!({ "ok": false, "error": err }),
    }
}

fn run_read_file(args: &Value) -> Value {
    let Some(path) = args.get("path").and_then(Value::as_str) else {
        return json!({ "ok": false, "error": "path is required" });
    };
    match resolve_in_home(path) {
        Ok(file) => match fs::read(&file) {
            Ok(bytes) => json!({
                "ok": true,
                "path": file.to_string_lossy(),
                "content": clip(String::from_utf8_lossy(&bytes).into_owned(), FILE_TEXT_LIMIT),
                "ran_on": "local_computer"
            }),
            Err(err) => json!({ "ok": false, "error": err.to_string() }),
        },
        Err(err) => json!({ "ok": false, "error": err }),
    }
}

fn run_write_file(args: &Value) -> Value {
    let Some(path) = args.get("path").and_then(Value::as_str) else {
        return json!({ "ok": false, "error": "path is required" });
    };
    let content = args.get("content").and_then(Value::as_str).unwrap_or("");
    match resolve_in_jail(path) {
        Ok(file) => {
            if let Some(parent) = file.parent() {
                let _ = fs::create_dir_all(parent);
            }
            match fs::write(&file, content.as_bytes()) {
                Ok(()) => json!({
                    "ok": true,
                    "path": file.to_string_lossy(),
                    "bytes": content.len(),
                    "ran_on": "local_computer"
                }),
                Err(err) => json!({ "ok": false, "error": err.to_string() }),
            }
        }
        Err(err) => json!({ "ok": false, "error": err }),
    }
}

fn run_publish_file(args: &Value) -> Value {
    let Some(path) = args.get("path").and_then(Value::as_str) else {
        return json!({ "ok": false, "error": "path is required" });
    };
    match resolve_in_home(path) {
        Ok(file) => match fs::read(&file) {
            Ok(bytes) if bytes.len() > PUBLISH_LIMIT => {
                json!({ "ok": false, "error": format!("{} is larger than {PUBLISH_LIMIT} bytes", file.display()) })
            }
            Ok(bytes) if bytes.is_empty() => json!({ "ok": false, "error": "file is empty" }),
            Ok(bytes) => {
                let filename = args
                    .get("filename")
                    .and_then(Value::as_str)
                    .map(str::to_string)
                    .unwrap_or_else(|| file.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "file".into()));
                json!({
                    "ok": true,
                    "filename": filename,
                    "content_b64": base64::engine::general_purpose::STANDARD.encode(bytes),
                    "ran_on": "local_computer"
                })
            }
            Err(err) => json!({ "ok": false, "error": err.to_string() }),
        },
        Err(err) => json!({ "ok": false, "error": err }),
    }
}

fn run_open_path(args: &Value) -> Value {
    let path = args.get("path").and_then(Value::as_str).unwrap_or("");
    match resolve_in_home(path) {
        Ok(file) => match os::open_path(&file) {
            Ok(()) => json!({ "ok": true, "path": file.to_string_lossy(), "ran_on": "local_computer" }),
            Err(err) => json!({ "ok": false, "error": err }),
        },
        Err(err) => json!({ "ok": false, "error": err }),
    }
}

fn run_launch_app(args: &Value) -> Value {
    let name = args.get("name").and_then(Value::as_str).unwrap_or("");
    match os::launch_app(name) {
        Ok(app) => json!({ "ok": true, "app": app, "ran_on": "local_computer" }),
        Err(err) => json!({ "ok": false, "error": err, "ran_on": "local_computer" }),
    }
}

fn python_bin() -> &'static str {
    let candidates: &[&str] = if cfg!(windows) {
        &["python", "py", "python3"]
    } else {
        &["python3", "python"]
    };
    for name in candidates {
        if std::process::Command::new(name).arg("-V").output().is_ok() {
            return name;
        }
    }
    candidates[0]
}

async fn run_exec(command: &str, cwd: Option<&str>, timeout_s: u64) -> Value {
    if command.trim().is_empty() {
        return json!({ "ok": false, "error": "command is required" });
    }
    let dir = match cwd {
        Some(raw) => resolve_in_jail(raw).unwrap_or_else(|_| jail_dir()),
        None => jail_dir(),
    };
    let timeout_s = timeout_s.clamp(10, 300);
    let mut cmd = if cfg!(windows) {
        let mut c = Command::new("cmd");
        c.args(["/C", command]);
        c
    } else {
        let mut c = Command::new("bash");
        c.args(["-lc", command]);
        c
    };
    cmd.current_dir(&dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    #[cfg(windows)]
    {
        cmd.creation_flags(0x0800_0000);
    }
    let mut child = match cmd.spawn() {
        Ok(child) => child,
        Err(err) => return json!({ "ok": false, "error": err.to_string() }),
    };
    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();
    let read_task = tokio::spawn(async move {
        let mut out = Vec::new();
        let mut err = Vec::new();
        if let Some(mut pipe) = stdout_pipe {
            let _ = pipe.read_to_end(&mut out).await;
        }
        if let Some(mut pipe) = stderr_pipe {
            let _ = pipe.read_to_end(&mut err).await;
        }
        (out, err)
    });
    match tokio::time::timeout(Duration::from_secs(timeout_s), child.wait()).await {
        Ok(status) => {
            let (out, err) = match read_task.await {
                Ok(pair) => pair,
                Err(_) => (Vec::new(), Vec::new()),
            };
            let code = status.ok().and_then(|s| s.code()).unwrap_or(1);
            let mut stdout_s = String::from_utf8_lossy(&out).into_owned();
            let stderr_s = String::from_utf8_lossy(&err);
            if !stderr_s.is_empty() {
                if !stdout_s.is_empty() {
                    stdout_s.push('\n');
                }
                stdout_s.push_str(&stderr_s);
            }
            json!({
                "ok": code == 0,
                "exit_code": code,
                "stdout": clip(stdout_s, STDOUT_LIMIT),
                "ran_on": "local_computer"
            })
        }
        Err(_) => {
            let _ = child.start_kill();
            let _ = child.wait().await;
            read_task.abort();
            json!({ "ok": false, "error": "timed out", "exit_code": 124, "ran_on": "local_computer" })
        }
    }
}

async fn run_code(code: &str, timeout_s: u64) -> Value {
    if code.trim().is_empty() {
        return json!({ "ok": false, "error": "code is required" });
    }
    let file = jail_dir().join(".lumio_run.py");
    if fs::write(&file, code.as_bytes()).is_err() {
        return json!({ "ok": false, "error": "could not write temp script" });
    }
    let quoted = file.to_string_lossy().replace('\'', "'\\''");
    let command = format!("{} '{}'", python_bin(), quoted);
    let out = run_exec(&command, None, timeout_s).await;
    let _ = fs::remove_file(&file);
    out
}

async fn ensure_chrome(slot: &Mutex<Option<browser::Chrome>>) -> Result<(), String> {
    if slot.lock().await.is_some() {
        return Ok(());
    }
    let chrome = browser::launch().await?;
    *slot.lock().await = Some(chrome);
    Ok(())
}

async fn run_browser(slot: &Mutex<Option<browser::Chrome>>, tool: &str, args: &Value) -> Value {
    if let Err(err) = ensure_chrome(slot).await {
        return json!({ "ok": false, "error": err, "ran_on": "local_computer" });
    }
    let chrome = slot.lock().await;
    let Some(chrome) = chrome.as_ref() else {
        return json!({ "ok": false, "error": "Chrome is not open" });
    };
    let out = match tool {
        "browser_navigate" | "browse_open" => {
            let url = args.get("url").and_then(Value::as_str).unwrap_or("");
            let wait_ms = args.get("wait_ms").and_then(Value::as_u64).unwrap_or(1200);
            chrome.navigate(url, wait_ms).await
        }
        "browser_snapshot" | "browse_screenshot" => chrome.snapshot().await,
        "browser_act" | "browse_act" => {
            let actions = args.get("actions").and_then(Value::as_array).cloned().unwrap_or_default();
            let one = args.get("action").cloned();
            let steps = if !actions.is_empty() {
                actions
            } else if let Some(action) = one {
                vec![action]
            } else {
                vec![args.clone()]
            };
            chrome.act(&steps).await
        }
        "browser_search" => {
            let q = args.get("query").and_then(Value::as_str).unwrap_or("");
            chrome.search(q).await
        }
        "browser_handoff" => chrome.snapshot().await.map(|mut data| {
            data["handoff"] = json!(true);
            data["note"] = json!("The Chrome window on this computer is yours. Finish the step there, then tell Lumio to continue.");
            data
        }),
        "browser_tabs" | "browser_new_tab" | "browser_switch_tab" | "browser_close_tab" => chrome.snapshot().await,
        _ => Ok(json!({ "ok": false, "error": format!("{tool} is not supported on this computer") })),
    };
    match out {
        Ok(v) => v,
        Err(err) => json!({ "ok": false, "error": err, "ran_on": "local_computer" }),
    }
}

async fn run_job(tool: &str, args: &Value, chrome: &Mutex<Option<browser::Chrome>>) -> Value {
    match tool {
        "exec" => {
            let command = args.get("command").and_then(Value::as_str).unwrap_or("");
            let cwd = args.get("cwd").and_then(Value::as_str);
            let timeout = args.get("timeout").and_then(Value::as_u64).unwrap_or(60);
            run_exec(command, cwd, timeout).await
        }
        "run_code" => {
            let code = args.get("code").and_then(Value::as_str).unwrap_or("");
            let timeout = args.get("timeout").and_then(Value::as_u64).unwrap_or(120);
            run_code(code, timeout).await
        }
        "list_dir" => run_list_dir(args),
        "read_file" => run_read_file(args),
        "write_file" => run_write_file(args),
        "publish_file" => run_publish_file(args),
        "open_path" => run_open_path(args),
        "launch_app" => run_launch_app(args),
        "boot" => match os::boot().await {
            Ok(v) => v,
            Err(err) => json!({ "ok": false, "error": err, "ran_on": "local_computer" }),
        },
        "status" | "touch" | "screen_png" | "computer_observe" => match os::snapshot().await {
            Ok(v) => v,
            Err(err) => json!({ "ok": false, "error": err, "ran_on": "local_computer" }),
        },
        "computer_act" | "computer_control" => {
            let actions = args.get("actions").and_then(Value::as_array).cloned().unwrap_or_else(|| vec![args.clone()]);
            match os::act(&actions).await {
                Ok(v) => v,
                Err(err) => json!({ "ok": false, "error": err, "ran_on": "local_computer" }),
            }
        }
        other if other.starts_with("browser") || other.starts_with("browse_") => run_browser(chrome, other, args).await,
        _ => json!({ "ok": false, "fallback_cloud": true, "error": format!("{tool} runs on the cloud computer") }),
    }
}

#[derive(Deserialize)]
struct JobsBody {
    job: Option<Job>,
}

#[derive(Deserialize)]
struct Job {
    id: String,
    tool: String,
    #[serde(default)]
    args: Value,
}

async fn poll_loop(state: Arc<Sidecar>) {
    let client = match reqwest::Client::builder().timeout(Duration::from_secs(40)).build() {
        Ok(c) => c,
        Err(_) => return,
    };
    loop {
        let (token, api_base) = {
            let token = state.token.lock().await.clone();
            let api_base = state.api_base.lock().await.clone();
            (token, api_base)
        };
        let (Some(token), Some(api_base)) = (token, api_base) else {
            tokio::time::sleep(Duration::from_secs(2)).await;
            continue;
        };
        let url = format!("{api_base}/api/desktop/jobs?wait=20");
        let response = client.get(&url).header("X-Lumio-Agent", &token).send().await;
        match response {
            Ok(res) if res.status().is_success() => {
                let body = res.json::<JobsBody>().await.ok();
                if let Some(job) = body.and_then(|b| b.job) {
                    let result = run_job(&job.tool, &job.args, &state.chrome).await;
                    let put = format!("{api_base}/api/desktop/jobs/result");
                    let _ = client
                        .post(&put)
                        .header("X-Lumio-Agent", &token)
                        .json(&json!({ "id": job.id, "result": result }))
                        .send()
                        .await;
                }
            }
            Ok(res) if res.status().as_u16() == 401 => {
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
            _ => {
                let _ = client
                    .post(format!("{api_base}/api/desktop/heartbeat"))
                    .header("X-Lumio-Agent", &token)
                    .send()
                    .await;
                tokio::time::sleep(Duration::from_secs(3)).await;
            }
        }
    }
}

#[tauri::command]
fn machine_info() -> Value {
    let host = hostname::get()
        .ok()
        .and_then(|h| h.into_string().ok())
        .unwrap_or_else(|| "Lumio desktop".into());
    json!({ "hostname": host, "os": std::env::consts::OS })
}

#[tauri::command]
async fn start_sidecar(app: tauri::AppHandle, token: String, api_base: String) -> Result<(), String> {
    let state = app.state::<Arc<Sidecar>>();
    *state.token.lock().await = Some(token);
    *state.api_base.lock().await = Some(api_base.trim_end_matches('/').to_string());
    if state.started.swap(true, Ordering::SeqCst) {
        return Ok(());
    }
    os::start_input_listen();
    let clone = state.inner().clone();
    let shots = state.inner().clone();
    tauri::async_runtime::spawn(async move {
        poll_loop(clone).await;
    });
    tauri::async_runtime::spawn(async move {
        screenshot_loop(shots).await;
    });
    Ok(())
}

async fn screenshot_loop(state: Arc<Sidecar>) {
    let client = match reqwest::Client::builder().timeout(Duration::from_secs(20)).build() {
        Ok(c) => c,
        Err(_) => return,
    };
    loop {
        tokio::time::sleep(Duration::from_millis(400)).await;
        let (token, api_base) = {
            let token = state.token.lock().await.clone();
            let api_base = state.api_base.lock().await.clone();
            (token, api_base)
        };
        let (Some(token), Some(api_base)) = (token, api_base) else { continue };
        let mut events = os::drain_teach();
        {
            let chrome = state.chrome.lock().await;
            if let Some(c) = chrome.as_ref() {
                events.extend(c.drain_teach().await);
            }
        }
        if events.is_empty() {
            continue;
        }
        let title = tokio::task::spawn_blocking(os::front_title)
            .await
            .unwrap_or_else(|_| "This computer".into());
        let _ = client
            .post(format!("{api_base}/api/desktop/teach-events"))
            .header("X-Lumio-Agent", &token)
            .json(&json!({ "events": events, "url": "", "title": title }))
            .send()
            .await;
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let _ = jail_dir();
    tauri::Builder::default()
        .manage(Arc::new(Sidecar {
            token: Mutex::new(None),
            api_base: Mutex::new(None),
            chrome: Mutex::new(None),
            started: AtomicBool::new(false),
        }))
        .invoke_handler(tauri::generate_handler![start_sidecar, machine_info])
        .setup(|app| {
            let url = lumio_url();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url.parse()?))
                .title("Lumio")
                .inner_size(1280.0, 840.0)
                .min_inner_size(900.0, 600.0)
                .initialization_script(include_str!("inject.js"))
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Lumio");
}

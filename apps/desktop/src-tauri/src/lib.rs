use std::fs;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use base64::Engine;
use serde::Deserialize;
use serde_json::{json, Value};
use tauri::webview::NewWindowResponse;
use tauri::{Manager, Url, WebviewUrl, WebviewWindowBuilder};
use tokio::io::AsyncReadExt;
use tokio::process::Command;
use tokio::sync::Mutex;

mod browser;
mod grants;
mod os;

const STDOUT_LIMIT: usize = 200_000;
const FILE_TEXT_LIMIT: usize = 80_000;
const TABLE_MAX: u64 = 64 * 1024 * 1024;
const PUBLISH_LIMIT: usize = 8 * 1024 * 1024;

struct Sidecar {
    token: Mutex<Option<String>>,
    api_base: Mutex<Option<String>>,
    chrome: Mutex<Option<browser::Chrome>>,
    started: AtomicBool,
}

fn app_host() -> String {
    Url::parse(&lumio_url())
        .ok()
        .and_then(|url| url.host_str().map(str::to_string))
        .unwrap_or_default()
}

fn stays_in_lumio(url: &Url) -> bool {
    let Some(host) = url.host_str() else {
        return true;
    };
    host == app_host() || host == "localhost" || host == "127.0.0.1"
}

fn open_in_browser(url: &str) {
    if !(url.starts_with("https://") || url.starts_with("http://")) {
        return;
    }
    #[cfg(target_os = "macos")]
    {
        let _ = std::process::Command::new("open").arg(url).spawn();
    }
    #[cfg(windows)]
    {
        let _ = std::process::Command::new("cmd").args(["/C", "start", "", url]).spawn();
    }
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

fn listed(dir: &Path) -> Value {
    match fs::read_dir(dir) {
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
        Err(err) if err.kind() == std::io::ErrorKind::PermissionDenied || err.raw_os_error() == Some(1) => match os::finder_entries(dir) {
            Ok(rows) => {
                let names: Vec<String> = rows
                    .into_iter()
                    .map(|(name, is_dir)| if is_dir { format!("{name}/") } else { name })
                    .collect();
                json!({ "ok": true, "path": dir.to_string_lossy(), "entries": names, "via": "finder", "ran_on": "local_computer" })
            }
            Err(msg) => json!({ "ok": false, "error": msg, "ran_on": "local_computer" }),
        },
        Err(err) => json!({ "ok": false, "error": err.to_string(), "ran_on": "local_computer" }),
    }
}

fn run_list_dir(args: &Value) -> Value {
    let path = args.get("path").and_then(Value::as_str).unwrap_or("");
    if path.trim().is_empty() {
        return grants::list_roots();
    }
    match grants::resolve_existing(path) {
        Ok(dir) => listed(&dir),
        Err(err) => err.to_json(),
    }
}

fn run_read_file(args: &Value) -> Value {
    let Some(path) = args.get("path").and_then(Value::as_str) else {
        return json!({ "ok": false, "error": "path is required", "ran_on": "local_computer" });
    };
    match grants::resolve_existing(path) {
        Ok(file) => read_granted(&file),
        Err(err) => err.to_json(),
    }
}

fn is_table(path: &Path) -> bool {
    matches!(
        path.extension().and_then(|ext| ext.to_str()).map(|ext| ext.to_ascii_lowercase()).as_deref(),
        Some("csv" | "tsv")
    )
}

fn read_granted(file: &Path) -> Value {
    let table = is_table(file);
    let max = if table { TABLE_MAX } else { grants::max_read_bytes() };
    let bytes = match fs::metadata(file) {
        Ok(meta) if meta.len() > max => {
            return json!({
                "ok": false,
                "error": format!("{} is larger than {max} bytes", file.display()),
                "ran_on": "local_computer"
            });
        }
        Ok(_) => match fs::read(file) {
            Ok(bytes) => Ok(bytes),
            Err(err) if err.kind() == std::io::ErrorKind::PermissionDenied || err.raw_os_error() == Some(1) => {
                os::finder_read(file, max)
            }
            Err(err) => Err(err.to_string()),
        },
        Err(err) if err.kind() == std::io::ErrorKind::PermissionDenied || err.raw_os_error() == Some(1) => os::finder_read(file, max),
        Err(err) => Err(err.to_string()),
    };
    match bytes {
        Ok(bytes) if table => table_report(file, &bytes),
        Ok(bytes) => json!({
            "ok": true,
            "path": file.to_string_lossy(),
            "bytes": bytes.len(),
            "content": clip(String::from_utf8_lossy(&bytes).into_owned(), FILE_TEXT_LIMIT),
            "ran_on": "local_computer"
        }),
        Err(err) => json!({ "ok": false, "error": err, "ran_on": "local_computer" }),
    }
}

fn table_report(file: &Path, bytes: &[u8]) -> Value {
    let text = String::from_utf8_lossy(bytes);
    let text = text.trim_start_matches('\u{feff}');
    let delim = if file.extension().and_then(|ext| ext.to_str()).unwrap_or("").eq_ignore_ascii_case("tsv") { '\t' } else { ',' };
    let summary = summarize_table(text, delim);
    json!({
        "ok": true,
        "path": file.to_string_lossy(),
        "bytes": bytes.len(),
        "summarized": true,
        "summary": summary,
        "note": "This table was summarized on this computer, including files too large to paste into chat. Use summary for row counts, status breakdowns, and numeric totals. Do not ask the user to enable Terminal.",
        "ran_on": "local_computer"
    })
}

fn summarize_table(text: &str, delim: char) -> Value {
    let mut rows = Vec::new();
    let mut current = String::new();
    let mut in_quotes = false;
    for ch in text.chars() {
        if ch == '"' {
            in_quotes = !in_quotes;
            current.push(ch);
        } else if ch == '\n' && !in_quotes {
            if !current.trim().is_empty() {
                rows.push(std::mem::take(&mut current));
            } else {
                current.clear();
            }
        } else if ch != '\r' {
            current.push(ch);
        }
        if rows.len() >= 400_000 {
            break;
        }
    }
    if !current.trim().is_empty() && rows.len() < 400_000 {
        rows.push(current);
    }
    if rows.is_empty() {
        return json!({ "rows": 0, "columns": [] });
    }
    let header = split_delimited(&rows[0], delim);
    let mut cols: Vec<ColStat> = header.iter().map(|name| ColStat::new(name.clone())).collect();
    let mut samples = Vec::new();
    let data_rows = &rows[1..];
    for (i, row) in data_rows.iter().enumerate() {
        let cells = split_delimited(row, delim);
        if i < 3 {
            let mut obj = serde_json::Map::new();
            for (idx, name) in header.iter().enumerate() {
                obj.insert(name.clone(), json!(cells.get(idx).map(|c| clip(c.clone(), 80)).unwrap_or_default()));
            }
            samples.push(Value::Object(obj));
        }
        for (idx, col) in cols.iter_mut().enumerate() {
            col.add(cells.get(idx).map(String::as_str).unwrap_or(""));
        }
    }
    json!({
        "rows": data_rows.len(),
        "truncated": rows.len() >= 400_000,
        "columns": cols.iter().map(ColStat::to_json).collect::<Vec<_>>(),
        "sample_rows": samples,
    })
}

struct ColStat {
    name: String,
    empty: u64,
    filled: u64,
    numeric_n: u64,
    sum: f64,
    min: f64,
    max: f64,
    counts: std::collections::BTreeMap<String, u64>,
    too_many: bool,
}

impl ColStat {
    fn new(name: String) -> Self {
        Self {
            name,
            empty: 0,
            filled: 0,
            numeric_n: 0,
            sum: 0.0,
            min: f64::MAX,
            max: f64::MIN,
            counts: std::collections::BTreeMap::new(),
            too_many: false,
        }
    }

    fn add(&mut self, raw: &str) {
        let value = raw.trim();
        if value.is_empty() {
            self.empty += 1;
            return;
        }
        self.filled += 1;
        if let Some(n) = parse_number(value) {
            self.numeric_n += 1;
            self.sum += n;
            self.min = self.min.min(n);
            self.max = self.max.max(n);
        }
        if self.too_many {
            return;
        }
        if self.counts.len() >= 40 && !self.counts.contains_key(value) {
            self.too_many = true;
            self.counts.clear();
            return;
        }
        *self.counts.entry(value.to_string()).or_insert(0) += 1;
    }

    fn to_json(&self) -> Value {
        let mut row = json!({
            "name": self.name,
            "filled": self.filled,
            "empty": self.empty,
        });
        if self.numeric_n > 0 && self.numeric_n * 2 >= self.filled {
            row["numeric"] = json!({
                "count": self.numeric_n,
                "sum": self.sum,
                "min": self.min,
                "max": self.max,
            });
        }
        if !self.too_many && !self.counts.is_empty() {
            let mut pairs: Vec<_> = self.counts.iter().collect();
            pairs.sort_by(|a, b| b.1.cmp(a.1).then(a.0.cmp(b.0)));
            row["breakdown"] = json!(pairs.into_iter().take(20).map(|(k, v)| json!({ "value": k, "count": v })).collect::<Vec<_>>());
        }
        row
    }
}

fn parse_number(raw: &str) -> Option<f64> {
    let cleaned: String = raw.chars().filter(|ch| *ch != ',' && *ch != '₹' && *ch != '$' && !ch.is_whitespace()).collect();
    if cleaned.is_empty() || cleaned.chars().any(|ch| !(ch.is_ascii_digit() || ch == '.' || ch == '-' || ch == '+')) {
        return None;
    }
    cleaned.parse().ok()
}

fn split_delimited(line: &str, delim: char) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut quotes = false;
    for ch in line.chars() {
        if ch == '"' {
            quotes = !quotes;
        } else if ch == delim && !quotes {
            out.push(cur.trim().trim_matches('"').to_string());
            cur.clear();
        } else {
            cur.push(ch);
        }
    }
    out.push(cur.trim().trim_matches('"').to_string());
    out
}

fn run_write_file(args: &Value) -> Value {
    let Some(path) = args.get("path").and_then(Value::as_str) else {
        return json!({ "ok": false, "error": "path is required", "ran_on": "local_computer" });
    };
    let content = args.get("content").and_then(Value::as_str).unwrap_or("");
    let overwrite = args.get("overwrite").and_then(Value::as_bool).unwrap_or(false);
    let bytes = if let Some(encoded) = args.get("content_base64").and_then(Value::as_str).filter(|s| !s.is_empty()) {
        match base64::engine::general_purpose::STANDARD.decode(encoded) {
            Ok(decoded) => decoded,
            Err(_) => {
                return json!({
                    "ok": false,
                    "error": "content_base64 is not valid base64",
                    "ran_on": "local_computer",
                })
            }
        }
    } else {
        content.as_bytes().to_vec()
    };
    match grants::resolve_writable(path) {
        Ok(file) => {
            if file.exists() && !overwrite {
                return json!({
                    "ok": false,
                    "need": "overwrite_confirm",
                    "path": file.to_string_lossy(),
                    "error": "File exists. Call write_file again with overwrite=true after the user confirms.",
                    "ran_on": "local_computer",
                });
            }
            if let Some(parent) = file.parent() {
                let _ = fs::create_dir_all(parent);
            }
            match fs::write(&file, &bytes) {
                Ok(()) if file.exists() => json!({
                    "ok": true,
                    "path": file.to_string_lossy(),
                    "bytes": bytes.len(),
                    "verified": true,
                    "ran_on": "local_computer"
                }),
                Ok(()) => json!({ "ok": false, "error": "write reported success but the file is missing", "ran_on": "local_computer" }),
                Err(err) => json!({ "ok": false, "error": err.to_string(), "ran_on": "local_computer" }),
            }
        }
        Err(err) => err.to_json(),
    }
}

fn run_publish_file(args: &Value) -> Value {
    let Some(path) = args.get("path").and_then(Value::as_str) else {
        return json!({ "ok": false, "error": "path is required", "ran_on": "local_computer" });
    };
    match grants::resolve_existing(path) {
        Ok(file) => match fs::read(&file) {
            Ok(bytes) if bytes.len() > PUBLISH_LIMIT => {
                json!({ "ok": false, "error": format!("{} is larger than {PUBLISH_LIMIT} bytes", file.display()), "ran_on": "local_computer" })
            }
            Ok(bytes) if bytes.is_empty() => json!({ "ok": false, "error": "file is empty", "ran_on": "local_computer" }),
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
                    "consent": "attach_to_chat",
                    "note": "This copies local file bytes into chat storage. Ask the user before calling publish_file.",
                    "ran_on": "local_computer"
                })
            }
            Err(err) => json!({ "ok": false, "error": err.to_string(), "ran_on": "local_computer" }),
        },
        Err(err) => err.to_json(),
    }
}

fn run_open_path(args: &Value) -> Value {
    let path = args.get("path").and_then(Value::as_str).unwrap_or("");
    match grants::resolve_existing(path) {
        Ok(file) => match os::open_path(&file) {
            Ok(()) => json!({ "ok": true, "path": file.to_string_lossy(), "ran_on": "local_computer" }),
            Err(err) => json!({ "ok": false, "error": err, "ran_on": "local_computer" }),
        },
        Err(err) => err.to_json(),
    }
}

fn run_search_files(args: &Value) -> Value {
    let q = args.get("query").and_then(Value::as_str).unwrap_or("");
    let under = args.get("path").and_then(Value::as_str);
    match grants::search(q, under) {
        Ok(v) => v,
        Err(err) => err.to_json(),
    }
}

fn run_copy_move(args: &Value, moving: bool) -> Value {
    let Some(src) = args.get("src").and_then(Value::as_str).or_else(|| args.get("from").and_then(Value::as_str)) else {
        return json!({ "ok": false, "error": "src is required", "ran_on": "local_computer" });
    };
    let Some(dest) = args.get("dest").and_then(Value::as_str).or_else(|| args.get("to").and_then(Value::as_str)) else {
        return json!({ "ok": false, "error": "dest is required", "ran_on": "local_computer" });
    };
    let overwrite = args.get("overwrite").and_then(Value::as_bool).unwrap_or(false);
    let from = match grants::resolve_existing(src) {
        Ok(p) => p,
        Err(err) => return err.to_json(),
    };
    let to = match grants::resolve_writable(dest) {
        Ok(p) => p,
        Err(err) => return err.to_json(),
    };
    if to.exists() && !overwrite {
        return json!({
            "ok": false,
            "need": "overwrite_confirm",
            "path": to.to_string_lossy(),
            "error": "Destination exists. Confirm overwrite with the user, then retry with overwrite=true.",
            "ran_on": "local_computer",
        });
    }
    let result = if moving { fs::rename(&from, &to) } else { fs::copy(&from, &to).map(|_| ()) };
    match result {
        Ok(()) if to.exists() => json!({
            "ok": true,
            "src": from.to_string_lossy(),
            "dest": to.to_string_lossy(),
            "moved": moving,
            "verified": true,
            "ran_on": "local_computer"
        }),
        Ok(()) => json!({ "ok": false, "error": "operation reported success but the destination is missing", "ran_on": "local_computer" }),
        Err(err) => json!({ "ok": false, "error": err.to_string(), "ran_on": "local_computer" }),
    }
}

fn run_delete_file(args: &Value) -> Value {
    let Some(path) = args.get("path").and_then(Value::as_str) else {
        return json!({ "ok": false, "error": "path is required", "ran_on": "local_computer" });
    };
    if !args.get("confirm").and_then(Value::as_bool).unwrap_or(false) {
        return json!({
            "ok": false,
            "need": "delete_confirm",
            "path": path,
            "error": "Deleting requires the user to confirm. Ask, then retry with confirm=true.",
            "ran_on": "local_computer",
        });
    }
    match grants::resolve_existing(path) {
        Ok(file) => {
            let result = if file.is_dir() { fs::remove_dir_all(&file) } else { fs::remove_file(&file) };
            match result {
                Ok(()) if !file.exists() => json!({ "ok": true, "path": file.to_string_lossy(), "verified": true, "ran_on": "local_computer" }),
                Ok(()) => json!({ "ok": false, "error": "delete reported success but the path still exists", "ran_on": "local_computer" }),
                Err(err) => json!({ "ok": false, "error": err.to_string(), "ran_on": "local_computer" }),
            }
        }
        Err(err) => err.to_json(),
    }
}

fn run_grant_folder(args: &Value) -> Value {
    grants::ensure_common_folders();
    let raw = args.get("path").and_then(Value::as_str).unwrap_or("").trim().to_string();
    if raw.is_empty() || grants::is_common_home_folder(&raw) {
        return json!({
            "ok": true,
            "folders": grants::snapshot().folders,
            "note": "Desktop, Documents, and Downloads are already allowed. List or read the file directly. Do not ask again and do not open a dialog.",
            "ran_on": "local_computer",
        });
    }
    json!({
        "ok": false,
        "need": "ask_user",
        "path": raw,
        "error": "Ask the user in one sentence whether Lumio may use this folder, then wait for their answer. Do not open a system dialog.",
        "ran_on": "local_computer",
    })
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
        return json!({ "ok": false, "error": "command is required", "ran_on": "local_computer" });
    }
    if !grants::terminal_allowed() {
        return json!({
            "ok": false,
            "need": "terminal_grant",
            "error": "Terminal access is off. File work uses list_dir, search_files, read_file, and write_file inside granted folders — not shell. The user can enable Terminal in This computer settings.",
            "ran_on": "local_computer",
        });
    }
    let dir = match cwd {
        Some(raw) => match grants::resolve_existing(raw) {
            Ok(p) if p.is_dir() => p,
            Ok(p) => p.parent().map(Path::to_path_buf).unwrap_or(p),
            Err(err) => return err.to_json(),
        },
        None => {
            let g = grants::snapshot();
            match g.folders.first() {
                Some(folder) => PathBuf::from(&folder.path),
                None => {
                    return grants::AccessError::NeedGrant { path: String::new() }.to_json();
                }
            }
        }
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
    if !grants::terminal_allowed() {
        return json!({
            "ok": false,
            "need": "terminal_grant",
            "error": "Full Python is off. Read a CSV or TSV with read_file: this computer summarizes row counts, status breakdowns, and numeric totals without Terminal.",
            "ran_on": "local_computer",
        });
    }
    let file = grants::data_dir().join(".lumio_run.py");
    if fs::write(&file, code.as_bytes()).is_err() {
        return json!({ "ok": false, "error": "could not write temp script" });
    }
    let quoted = file.to_string_lossy().replace('\'', "'\\''");
    let command = format!("{} '{}'", python_bin(), quoted);
    let out = run_exec(&command, None, timeout_s).await;
    let _ = fs::remove_file(&file);
    out
}

fn web_url(raw: &str) -> Option<&str> {
    let url = raw.trim();
    if url.starts_with("https://") || url.starts_with("http://") {
        Some(url)
    } else {
        None
    }
}

async fn ensure_chrome(slot: &Mutex<Option<browser::Chrome>>, start_url: Option<&str>) -> Result<(), String> {
    if let Some(url) = start_url.and_then(web_url) {
        let chrome = browser::launch(url).await?;
        *slot.lock().await = Some(chrome);
        return Ok(());
    }
    if slot.lock().await.is_some() {
        return Ok(());
    }
    if let Ok(chrome) = browser::attach().await {
        *slot.lock().await = Some(chrome);
        return Ok(());
    }
    Err("Open a site with browser_navigate and a full https address. Do not open a blank page.".into())
}

async fn run_browser(slot: &Mutex<Option<browser::Chrome>>, tool: &str, args: &Value) -> Value {
    let start_url = match tool {
        "browser_navigate" | "browse_open" => args.get("url").and_then(Value::as_str).and_then(web_url),
        "browser_search" => Some("https://www.google.com"),
        _ => None,
    };
    if matches!(tool, "browser_navigate" | "browse_open") && start_url.is_none() {
        return json!({
            "ok": false,
            "error": "A full https address is required. Do not open Chrome on a blank page.",
            "ran_on": "local_computer"
        });
    }
    if let Err(err) = ensure_chrome(slot, start_url).await {
        return json!({ "ok": false, "error": err, "ran_on": "local_computer" });
    }
    let chrome = slot.lock().await;
    let Some(chrome) = chrome.as_ref() else {
        return json!({ "ok": false, "error": "Chrome is not open" });
    };
    let out = match tool {
        "browser_navigate" | "browse_open" => {
            let url = start_url.unwrap_or("");
            let wait_ms = args.get("wait_ms").and_then(Value::as_u64).unwrap_or(1200);
            chrome.navigate(url, wait_ms).await
        }
        "browser_snapshot" | "browse_screenshot" => chrome.snapshot().await,
        "browser_collect" => chrome.collect(args).await,
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
        "list_grants" | "list_folder_grants" => grants::list_roots(),
        "grant_folder" => run_grant_folder(args),
        "search_files" => run_search_files(args),
        "read_file" => run_read_file(args),
        "write_file" => run_write_file(args),
        "copy_file" => run_copy_move(args, false),
        "move_file" | "rename_file" => run_copy_move(args, true),
        "delete_file" => run_delete_file(args),
        "publish_file" => run_publish_file(args),
        "open_path" => run_open_path(args),
        "launch_app" => run_launch_app(args),
        "app_discover" => os::discover_apps(),
        "boot" => {
            // Missing `teach` means an older server: recording starts with boot so a teaching
            // session still captures this computer instead of a leftover browser page.
            let on = args.get("teach").and_then(Value::as_bool).unwrap_or(true);
            os::set_teaching(on);
            match os::boot().await {
                Ok(v) => v,
                Err(err) => json!({ "ok": false, "error": err, "ran_on": "local_computer" }),
            }
        }
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
                    let state = state.clone();
                    let client = client.clone();
                    let token = token.clone();
                    let api_base = api_base.clone();
                    tokio::spawn(async move {
                        let result = run_job(&job.tool, &job.args, &state.chrome).await;
                        let put = format!("{api_base}/api/desktop/jobs/result");
                        let _ = client
                            .post(&put)
                            .header("X-Lumio-Agent", &token)
                            .json(&json!({ "id": job.id, "result": result }))
                            .send()
                            .await;
                    });
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
    json!({ "hostname": host, "os": std::env::consts::OS, "folders": grants::snapshot().folders, "terminal": grants::terminal_allowed() })
}

#[tauri::command]
fn list_folder_grants() -> Value {
    grants::list_roots()
}

#[tauri::command]
fn open_external(url: String) -> Result<(), String> {
    let url = url.trim();
    if web_url(url).is_none() || url.chars().any(|c| c.is_control()) {
        return Err("Only http and https links can be opened.".into());
    }
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open").arg(url).spawn().map_err(|err| err.to_string())?;
    }
    #[cfg(windows)]
    {
        std::process::Command::new("cmd").args(["/C", "start", "", url]).spawn().map_err(|err| err.to_string())?;
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        return Err("Opening links is not supported here.".into());
    }
    Ok(())
}

#[tauri::command]
fn grant_folder_ui() -> Result<Value, String> {
    let grant = grants::pick_folder(None)?;
    Ok(json!({ "ok": true, "grant": grant, "folders": grants::snapshot().folders, "terminal": grants::terminal_allowed() }))
}

#[tauri::command]
fn revoke_folder_grant(id: String) -> Result<Value, String> {
    let g = grants::revoke(&id)?;
    Ok(json!({ "ok": true, "folders": g.folders, "terminal": g.terminal }))
}

#[tauri::command]
fn set_terminal_grant(enabled: bool) -> Result<Value, String> {
    let g = grants::set_terminal(enabled)?;
    Ok(json!({ "ok": true, "terminal": g.terminal, "folders": g.folders }))
}

#[tauri::command]
fn reset_browser_profile() -> Result<Value, String> {
    let dir = grants::browser_profile_dir();
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(json!({ "ok": true, "cleared": true, "path": dir.to_string_lossy() }))
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
    let _ = tokio::task::spawn_blocking(grants::ensure_common_folders).await;
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
    let mut last_shot = tokio::time::Instant::now() - Duration::from_secs(30);
    loop {
        tokio::time::sleep(Duration::from_millis(400)).await;
        let (token, api_base) = {
            let token = state.token.lock().await.clone();
            let api_base = state.api_base.lock().await.clone();
            (token, api_base)
        };
        let (Some(token), Some(api_base)) = (token, api_base) else { continue };
        let mut events = os::drain_teach();
        if let Ok(chrome) = state.chrome.try_lock() {
            if let Some(c) = chrome.as_ref() {
                if let Ok(extra) = tokio::time::timeout(Duration::from_millis(800), c.drain_teach()).await {
                    events.extend(extra);
                }
            }
        }
        if os::teaching() {
            let title = os::cached_title();
            if last_shot.elapsed() >= Duration::from_secs(6) {
                last_shot = tokio::time::Instant::now();
                let client = client.clone();
                let token = token.clone();
                let api_base = api_base.clone();
                let title = title.clone();
                tokio::spawn(async move {
                    let shot = tokio::time::timeout(Duration::from_secs(5), tokio::task::spawn_blocking(os::screenshot_b64)).await;
                    if let Ok(Ok(Ok(png))) = shot {
                        let _ = client
                            .post(format!("{api_base}/api/desktop/screen"))
                            .header("X-Lumio-Agent", &token)
                            .json(&json!({ "png_b64": png, "url": "", "title": title }))
                            .send()
                            .await;
                    }
                });
            }
        }
        if events.is_empty() {
            continue;
        }
        let title = os::cached_title();
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
    tauri::Builder::default()
        .manage(Arc::new(Sidecar {
            token: Mutex::new(None),
            api_base: Mutex::new(None),
            chrome: Mutex::new(None),
            started: AtomicBool::new(false),
        }))
        .invoke_handler(tauri::generate_handler![
            start_sidecar,
            machine_info,
            list_folder_grants,
            grant_folder_ui,
            open_external,
            revoke_folder_grant,
            set_terminal_grant,
            reset_browser_profile
        ])
        .setup(|app| {
            let _ = grants::data_dir();
            let url = lumio_url();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url.parse()?))
                .title("Lumio")
                .inner_size(1280.0, 840.0)
                .min_inner_size(900.0, 600.0)
                .initialization_script(include_str!("inject.js"))
                .on_navigation(|url| {
                    if stays_in_lumio(url) {
                        true
                    } else {
                        open_in_browser(url.as_str());
                        false
                    }
                })
                .on_new_window(|url, _features| {
                    if !stays_in_lumio(&url) {
                        open_in_browser(url.as_str());
                    }
                    NewWindowResponse::Deny
                })
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Lumio");
}

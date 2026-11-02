//! The user's real desktop: full-screen capture, OS mouse/keys, Finder/Explorer, teaching input.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use base64::Engine;
use enigo::{Enigo, Key, KeyboardControllable, MouseButton, MouseControllable};
use serde_json::{json, Value};

static TEACH: OnceLock<Mutex<Vec<Value>>> = OnceLock::new();
static MOUSE: OnceLock<Mutex<(i32, i32)>> = OnceLock::new();
static TITLE: OnceLock<Mutex<String>> = OnceLock::new();
static LISTENING: AtomicBool = AtomicBool::new(false);
static TEACHING: AtomicBool = AtomicBool::new(false);
static WATCHING: AtomicBool = AtomicBool::new(false);

fn teach_buf() -> &'static Mutex<Vec<Value>> {
    TEACH.get_or_init(|| Mutex::new(Vec::new()))
}

fn mouse_pos() -> &'static Mutex<(i32, i32)> {
    MOUSE.get_or_init(|| Mutex::new((0, 0)))
}

fn push_event(row: Value) {
    if let Ok(mut buf) = teach_buf().lock() {
        if buf.len() > 800 {
            buf.drain(..400);
        }
        buf.push(row);
    }
}

pub fn start_input_listen() {
    if LISTENING.swap(true, Ordering::SeqCst) {
        return;
    }
    std::thread::spawn(|| {
        loop {
            let title = front_title();
            if let Ok(mut slot) = TITLE.get_or_init(|| Mutex::new(String::new())).lock() {
                *slot = title;
            }
            let pause = if teaching() { 800 } else { 2000 };
            std::thread::sleep(Duration::from_millis(pause));
        }
    });
    std::thread::spawn(|| {
        if let Err(err) = rdev::listen(move |event| {
            match event.event_type {
                rdev::EventType::MouseMove { x, y } => {
                    if let Ok(mut pos) = mouse_pos().lock() {
                        *pos = (x as i32, y as i32);
                    }
                }
                rdev::EventType::ButtonPress(button) => {
                    let (x, y) = mouse_pos().lock().map(|p| *p).unwrap_or((0, 0));
                    let button = match button {
                        rdev::Button::Left => 1,
                        rdev::Button::Right => 3,
                        _ => 2,
                    };
                    push_event(json!({
                        "kind": "click",
                        "x": x,
                        "y": y,
                        "button": button,
                        "window": cached_title(),
                    }));
                }
                rdev::EventType::Wheel { delta_y, .. } => {
                    let direction = if delta_y < 0 { "down" } else { "up" };
                    push_event(json!({ "kind": "scroll", "direction": direction, "window": cached_title() }));
                }
                rdev::EventType::KeyPress(key) => {
                    if let Some(row) = map_key(key) {
                        push_event(row);
                    }
                }
                _ => {}
            }
        }) {
            push_event(json!({
                "kind": "capture_unavailable",
                "reason": format!("input capture needs Accessibility for Lumio ({err:?})"),
            }));
        }
    });
}

pub fn set_teaching(on: bool) {
    TEACHING.store(on, Ordering::SeqCst);
    if on {
        start_input_listen();
        start_activity_watch();
    }
}

pub fn teaching() -> bool {
    TEACHING.load(Ordering::SeqCst)
}

pub fn cached_title() -> String {
    TITLE
        .get_or_init(|| Mutex::new(String::new()))
        .lock()
        .map(|s| s.clone())
        .unwrap_or_default()
}

fn map_key(key: rdev::Key) -> Option<Value> {
    let window = cached_title();
    match key {
        rdev::Key::Return => Some(json!({ "kind": "key", "key": "Enter", "window": window })),
        rdev::Key::Tab => Some(json!({ "kind": "key", "key": "Tab", "window": window })),
        rdev::Key::Escape => Some(json!({ "kind": "key", "key": "Escape", "window": window })),
        rdev::Key::Backspace => Some(json!({ "kind": "key", "key": "Backspace", "window": window })),
        rdev::Key::Space => Some(json!({ "kind": "char", "char": " ", "window": window })),
        other => {
            let name = format!("{other:?}");
            if let Some(ch) = name.strip_prefix("Key").and_then(|s| s.chars().next()) {
                if ch.is_ascii_alphabetic() {
                    return Some(json!({ "kind": "char", "char": ch.to_ascii_lowercase().to_string(), "window": window }));
                }
            }
            if let Some(n) = name.strip_prefix("Num") {
                if n.len() == 1 {
                    return Some(json!({ "kind": "char", "char": n, "window": window }));
                }
            }
            None
        }
    }
}

fn run_osascript_fast(script: &str, args: &[&str]) -> Result<String, String> {
    use std::io::Write;
    use std::process::Stdio;
    use std::sync::mpsc;
    let mut cmd = Command::new("osascript");
    cmd.arg("-");
    for arg in args {
        cmd.arg(arg);
    }
    let mut child = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| err.to_string())?;
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(script.as_bytes());
    }
    let pid = child.id();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(child.wait_with_output());
    });
    let output = match rx.recv_timeout(Duration::from_secs(2)) {
        Ok(Ok(output)) => output,
        _ => {
            let _ = Command::new("kill").arg(pid.to_string()).status();
            return Err("timed out".into());
        }
    };
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn browser_app(name: &str) -> Option<&'static str> {
    match name.to_ascii_lowercase().as_str() {
        "google chrome" | "chrome" => Some("Google Chrome"),
        "chromium" => Some("Chromium"),
        "microsoft edge" | "edge" => Some("Microsoft Edge"),
        "brave browser" | "brave" => Some("Brave Browser"),
        "arc" => Some("Arc"),
        "safari" => Some("Safari"),
        _ => None,
    }
}

fn front_browser(app: &str) -> Option<(String, String)> {
    let script = if app == "Safari" {
        "tell application \"Safari\"\n  if (count of windows) is 0 then return \"\"\n  set u to URL of current tab of front window\n  set t to name of current tab of front window\n  return u & linefeed & t\nend tell".to_string()
    } else {
        format!(
            "tell application \"{app}\"\n  if (count of windows) is 0 then return \"\"\n  set u to URL of active tab of front window\n  set t to title of active tab of front window\n  return u & linefeed & t\nend tell"
        )
    };
    let out = run_osascript_fast(&script, &[]).ok()?;
    let mut lines = out.lines();
    let url = lines.next().unwrap_or("").trim();
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return None;
    }
    let title = lines.next().unwrap_or("").trim();
    Some((url.to_string(), if title.is_empty() { app.to_string() } else { title.to_string() }))
}

fn front_finder_folder() -> Option<String> {
    let script = r#"tell application "Finder"
  if (count of Finder windows) is 0 then return ""
  POSIX path of (target of front Finder window as alias)
end tell"#;
    let path = run_osascript_fast(script, &[]).ok()?;
    let path = path.trim().trim_end_matches('/').to_string();
    if path.starts_with('/') { Some(path) } else { None }
}

fn watch_roots() -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Some(home) = dirs::home_dir() {
        for name in ["Desktop", "Documents", "Downloads"] {
            let path = home.join(name);
            if path.is_dir() {
                roots.push(path);
            }
        }
    }
    roots
}

fn list_tree(root: &Path, depth: u8, into: &mut std::collections::BTreeSet<String>) {
    if depth > 5 || into.len() > 4000 {
        return;
    }
    if let Ok(entries) = std::fs::read_dir(root) {
        for entry in entries.flatten() {
            if into.len() > 4000 {
                return;
            }
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.starts_with('.') {
                continue;
            }
            let path = entry.path();
            let text = path.to_string_lossy().to_string();
            if path.is_dir() {
                into.insert(format!("{text}/"));
                list_tree(&path, depth + 1, into);
            } else {
                into.insert(text);
            }
        }
        return;
    }
    let script = r#"on run argv
  set rootPath to item 1 of argv
  do shell script "find " & quoted form of rootPath & " -maxdepth 5 \\( -type f -o -type d \\) ! -name '.*' 2>/dev/null"
end run"#;
    let Ok(out) = run_osascript_fast(script, &[&root.to_string_lossy()]) else { return };
    for line in out.lines() {
        if into.len() > 4000 {
            return;
        }
        let line = line.trim();
        if line.is_empty() || line == root.to_string_lossy() {
            continue;
        }
        into.insert(line.to_string());
    }
}

fn snapshot_files(extra: Option<&Path>) -> std::collections::BTreeSet<String> {
    let mut files = std::collections::BTreeSet::new();
    let mut roots = watch_roots();
    if let Some(extra) = extra {
        if extra.is_dir() && !roots.iter().any(|root| extra.starts_with(root)) {
            roots.push(extra.to_path_buf());
        }
    }
    for root in roots {
        list_tree(&root, 0, &mut files);
    }
    files
}

fn emit_file_changes(before: &std::collections::BTreeSet<String>, after: &std::collections::BTreeSet<String>) {
    let created: Vec<&String> = after.difference(before).collect();
    let deleted: Vec<&String> = before.difference(after).collect();
    let mut used_created = vec![false; created.len()];
    for gone in &deleted {
        let gone_name = Path::new(gone.trim_end_matches('/')).file_name().and_then(|n| n.to_str()).unwrap_or("");
        if let Some(idx) = created.iter().enumerate().position(|(idx, path)| {
            !used_created[idx]
                && !gone_name.is_empty()
                && Path::new(path.trim_end_matches('/')).file_name().and_then(|n| n.to_str()) == Some(gone_name)
        }) {
            if !used_created[idx] {
                used_created[idx] = true;
                push_event(json!({
                    "kind": "file",
                    "action": "renamed",
                    "path": created[idx],
                    "text": gone,
                }));
                continue;
            }
        }
        push_event(json!({ "kind": "file", "action": "deleted", "path": gone }));
    }
    for (idx, path) in created.iter().enumerate() {
        if used_created[idx] {
            continue;
        }
        push_event(json!({ "kind": "file", "action": "created", "path": path }));
    }
}

fn own_app(app: &str) -> bool {
    let name = app.to_ascii_lowercase();
    name == "lumio" || name == "checkme"
}

fn safe_app_name(app: &str) -> Option<&str> {
    if app.is_empty() || app.len() > 80 {
        return None;
    }
    if app.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, ' ' | '.' | '-' | '_')) {
        Some(app)
    } else {
        None
    }
}

fn context_script(app: &str) -> Option<String> {
    let name = safe_app_name(app)?;
    let lower = name.to_ascii_lowercase();
    let body = if lower == "microsoft excel" || lower == "excel" {
        "tell application \"Microsoft Excel\"\n  if (count of workbooks) is 0 then return \"\"\n  return (name of active workbook) & \" — \" & (name of active sheet)\nend tell".to_string()
    } else if lower == "microsoft word" || lower == "word" {
        "tell application \"Microsoft Word\"\n  if (count of documents) is 0 then return \"\"\n  return name of active document\nend tell".to_string()
    } else if lower == "microsoft powerpoint" || lower == "powerpoint" {
        "tell application \"Microsoft PowerPoint\"\n  if (count of presentations) is 0 then return \"\"\n  return name of active presentation\nend tell".to_string()
    } else if lower == "numbers" || lower == "pages" || lower == "keynote" {
        format!("tell application \"{name}\"\n  if (count of documents) is 0 then return \"\"\n  return name of front document\nend tell")
    } else if browser_app(name).is_some() || lower == "finder" {
        return None;
    } else {
        format!("tell application \"{name}\"\n  try\n    return name of front window\n  end try\n  try\n    return name of document 1\n  end try\n  return \"\"\nend tell")
    };
    Some(body)
}

fn front_work_context(app: &str) -> Option<String> {
    let script = context_script(app)?;
    match run_osascript_fast(&script, &[]) {
        Ok(text) => Some(text.trim().to_string()),
        Err(_) => None,
    }
}

fn start_activity_watch() {
    if WATCHING.swap(true, Ordering::SeqCst) {
        return;
    }
    std::thread::spawn(|| {
        let mut files = std::collections::BTreeSet::new();
        let mut last_url = String::new();
        let mut last_folder = String::new();
        let mut last_app = String::new();
        let mut last_context = String::new();
        let mut quiet_apps: std::collections::HashSet<String> = std::collections::HashSet::new();
        let mut tick: u32 = 0;
        let mut was_teaching = false;
        loop {
            let on = teaching();
            if on && !was_teaching {
                files = snapshot_files(None);
                last_url.clear();
                last_folder.clear();
                last_app.clear();
                last_context.clear();
                quiet_apps.clear();
                tick = 0;
            }
            was_teaching = on;
            if on {
                tick = tick.wrapping_add(1);
                let app = lsappinfo_front().unwrap_or_else(cached_title);
                if !app.is_empty() && app != "This computer" && !own_app(&app) && app != last_app {
                    last_app = app.clone();
                    last_context.clear();
                    push_event(json!({
                        "kind": "work",
                        "text": app,
                        "action": "opened",
                    }));
                }
                let due = !last_app.is_empty()
                    && context_script(&last_app).is_some()
                    && (!quiet_apps.contains(&last_app) || tick % 15 == 0)
                    && (last_context.is_empty() || tick % 3 == 0);
                if due {
                    match front_work_context(&last_app) {
                        Some(ctx) => {
                            quiet_apps.remove(&last_app);
                            if !ctx.is_empty() && ctx != last_context {
                                last_context = ctx.clone();
                                push_event(json!({
                                    "kind": "work",
                                    "text": last_app,
                                    "window": ctx,
                                    "action": "using",
                                }));
                            }
                        }
                        None => {
                            quiet_apps.insert(last_app.clone());
                        }
                    }
                }
                if let Some(browser) = browser_app(&app) {
                    if let Some((url, title)) = front_browser(browser) {
                        if url != last_url {
                            last_url = url.clone();
                            push_event(json!({
                                "kind": "navigate",
                                "url": url,
                                "window": title,
                                "text": browser,
                            }));
                        }
                    }
                }
                let folder = if app.eq_ignore_ascii_case("finder") { front_finder_folder() } else { None };
                if let Some(path) = folder.clone() {
                    if path != last_folder {
                        last_folder = path.clone();
                        push_event(json!({ "kind": "folder", "path": path, "window": "Finder" }));
                    }
                }
                let now = snapshot_files(folder.as_deref().map(Path::new));
                emit_file_changes(&files, &now);
                files = now;
            }
            std::thread::sleep(Duration::from_secs(1));
        }
    });
}

pub fn drain_teach() -> Vec<Value> {
    teach_buf().lock().map(|mut buf| buf.drain(..).take(200).collect()).unwrap_or_default()
}

fn lsappinfo_front() -> Option<String> {
    let asn = Command::new("lsappinfo").arg("front").output().ok()?;
    let asn = String::from_utf8_lossy(&asn.stdout).trim().to_string();
    if asn.is_empty() || asn.contains("NULL") {
        return None;
    }
    let info = Command::new("lsappinfo").args(["info", "-only", "name", &asn]).output().ok()?;
    let text = String::from_utf8_lossy(&info.stdout).to_string();
    let marker = "\"LSDisplayName\"=\"";
    let start = text.find(marker)? + marker.len();
    let name = text[start..].split('"').next()?.trim();
    if name.is_empty() { None } else { Some(name.to_string()) }
}

pub fn front_title() -> String {
    #[cfg(target_os = "macos")]
    {
        // lsappinfo does not need Accessibility. System Events does, and that prompt stalls teaching.
        if let Some(name) = lsappinfo_front() {
            return name;
        }
        "This computer".into()
    }
    #[cfg(windows)]
    {
        "This computer".into()
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        "This computer".into()
    }
}

fn capture_png() -> Result<Vec<u8>, String> {
    let path = std::env::temp_dir().join(format!("lumio-desktop-{}.png", std::process::id()));
    #[cfg(target_os = "macos")]
    {
        use std::sync::mpsc;
        let mut child = Command::new("screencapture")
            .args(["-x", "-t", "png"])
            .arg(&path)
            .spawn()
            .map_err(|err| err.to_string())?;
        let pid = child.id();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let _ = tx.send(child.wait());
        });
        match rx.recv_timeout(Duration::from_secs(4)) {
            Ok(Ok(status)) if status.success() => {}
            Ok(Ok(_)) | Err(_) => {
                let _ = Command::new("kill").arg(pid.to_string()).status();
                return Err("Allow Screen Recording for Lumio in System Settings → Privacy & Security.".into());
            }
            Ok(Err(err)) => return Err(err.to_string()),
        }
        // Retina PNGs freeze the app if we ship them raw. 1280px is enough for skill frames.
        let _ = Command::new("sips").args(["-Z", "1280"]).arg(&path).status();
    }
    #[cfg(windows)]
    {
        let quoted = path.to_string_lossy().replace('\'', "''");
        let script = format!(
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; $s=[Windows.Forms.Screen]::PrimaryScreen; $b=New-Object Drawing.Bitmap $s.Bounds.Width,$s.Bounds.Height; $g=[Drawing.Graphics]::FromImage($b); $g.CopyFromScreen($s.Bounds.Location,[Drawing.Point]::Empty,$s.Bounds.Size); $b.Save('{quoted}');"
        );
        let status = Command::new("powershell")
            .args(["-NoProfile", "-Command", &script])
            .status()
            .map_err(|err| err.to_string())?;
        if !status.success() {
            return Err("Could not capture the Windows desktop".into());
        }
    }
    let bytes = std::fs::read(&path).map_err(|err| {
        if cfg!(target_os = "macos") {
            "Allow Screen Recording for Lumio in System Settings → Privacy & Security.".into()
        } else {
            err.to_string()
        }
    })?;
    if bytes.len() < 32 {
        return Err("empty screenshot".into());
    }
    Ok(bytes)
}

pub fn screenshot_b64() -> Result<String, String> {
    let png = capture_png()?;
    Ok(base64::engine::general_purpose::STANDARD.encode(png))
}

pub fn is_denied_message(err: &str) -> bool {
    let lower = err.to_lowercase();
    lower.contains("operation not permitted") || lower.contains("permission denied") || lower.contains("os error 1")
}

fn run_osascript(script: &str, args: &[&Path]) -> Result<String, String> {
    use std::io::Write;
    use std::process::Stdio;
    use std::sync::mpsc;
    let mut cmd = Command::new("osascript");
    cmd.arg("-");
    for arg in args {
        cmd.arg(arg);
    }
    let mut child = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| err.to_string())?;
    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(script.as_bytes()).map_err(|err| err.to_string())?;
    }
    let pid = child.id();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(child.wait_with_output());
    });
    let output = match rx.recv_timeout(Duration::from_secs(25)) {
        Ok(Ok(output)) => output,
        Ok(Err(err)) => return Err(err.to_string()),
        Err(_) => {
            let _ = Command::new("kill").arg(pid.to_string()).status();
            return Err("That file took too long to open. Ask again and I will retry it here.".into());
        }
    };
    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr);
        return Err(if err.trim().is_empty() { "Could not open that file on this computer.".into() } else { err.trim().to_string() });
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

pub fn finder_entries(path: &Path) -> Result<Vec<(String, bool)>, String> {
    let script = r#"on run argv
  set p to item 1 of argv
  set AppleScript's text item delimiters to linefeed
  tell application "Finder"
    set folderRef to POSIX file p as alias
    set rows to {}
    set theItems to every item of folderRef
    repeat with i from 1 to count of theItems
      set itRef to item i of theItems
      set n to name of itRef
      set k to class of itRef as text
      set end of rows to (n as text) & tab & k
    end repeat
    return rows as text
  end tell
end run"#;
    let text = run_osascript(script, &[path])?;
    let mut rows = Vec::new();
    for line in text.split(|c| c == '\n' || c == '\r') {
        if line.trim().is_empty() {
            continue;
        }
        let (name, kind) = line.split_once('\t').unwrap_or((line, ""));
        rows.push((name.to_string(), kind == "folder"));
    }
    rows.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(rows)
}

pub fn finder_read(path: &Path, max: u64) -> Result<Vec<u8>, String> {
    let dest = std::env::temp_dir().join(format!(
        "lumio-read-{}-{}",
        std::process::id(),
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0)
    ));
    let script = r#"on run argv
  set srcPath to item 1 of argv
  set destPath to item 2 of argv
  do shell script "ditto " & quoted form of srcPath & " " & quoted form of destPath
  return "ok"
end run"#;
    let copied = run_osascript(script, &[path, dest.as_path()]);
    if copied.is_err() {
        let _ = std::fs::remove_file(&dest);
        return copied.map(|_| Vec::new());
    }
    if dest.is_dir() {
        let _ = std::fs::remove_dir_all(&dest);
        return Err("That path is a folder. List it instead of reading it as a file.".into());
    }
    let bytes = std::fs::read(&dest).map_err(|err| err.to_string());
    let _ = std::fs::remove_file(&dest);
    let bytes = bytes?;
    if bytes.len() as u64 > max {
        return Err(format!("{} is larger than {max} bytes", path.display()));
    }
    Ok(bytes)
}

pub fn reveal_home() {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
    #[cfg(target_os = "macos")]
    {
        let _ = Command::new("open").arg(&home).spawn();
    }
    #[cfg(windows)]
    {
        let _ = Command::new("explorer").arg(&home).spawn();
    }
}

fn collect_apps(dir: &Path, into: &mut Vec<(String, bool)>) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        if into.len() >= 120 {
            return;
        }
        let name = entry.file_name().to_string_lossy().to_string();
        let Some(stem) = name.strip_suffix(".app") else { continue };
        if stem.is_empty() {
            continue;
        }
        let electron = entry.path().join("Contents/Frameworks/Electron Framework.framework").is_dir();
        if !into.iter().any(|(have, _)| have == stem) {
            into.push((stem.to_string(), electron));
        }
    }
}

fn running_apps() -> Vec<String> {
    let Ok(out) = Command::new("lsappinfo").arg("list").output() else { return Vec::new() };
    let text = String::from_utf8_lossy(&out.stdout);
    let mut names = Vec::new();
    let mut current = String::new();
    for line in text.lines() {
        let trimmed = line.trim();
        if let Some(start) = trimmed.find(") \"") {
            if let Some(end) = trimmed[start + 3..].find('"') {
                current = trimmed[start + 3..start + 3 + end].to_string();
            }
        }
        if trimmed.contains("type=\"Foreground\"") && !current.is_empty() && !names.contains(&current) {
            names.push(current.clone());
        }
        if names.len() >= 40 {
            break;
        }
    }
    names
}

fn lumio_tabs() -> Vec<Value> {
    use std::io::{Read, Write};
    use std::net::{TcpStream, SocketAddr};
    use std::time::Duration;
    let addr: SocketAddr = "127.0.0.1:9333".parse().unwrap_or(SocketAddr::from(([127, 0, 0, 1], 9333)));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(400)) else { return Vec::new() };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(400)));
    if stream.write_all(b"GET /json/list HTTP/1.1\r\nHost: 127.0.0.1:9333\r\nConnection: close\r\n\r\n").is_err() {
        return Vec::new();
    }
    let mut buf = Vec::new();
    let _ = stream.read_to_end(&mut buf);
    let text = String::from_utf8_lossy(&buf);
    let Some(body) = text.split("\r\n\r\n").nth(1) else { return Vec::new() };
    let Ok(pages) = serde_json::from_str::<Vec<Value>>(body) else { return Vec::new() };
    let mut tabs = Vec::new();
    for page in pages {
        if page.get("type").and_then(Value::as_str) != Some("page") {
            continue;
        }
        let url = page.get("url").and_then(Value::as_str).unwrap_or("").to_string();
        if !(url.starts_with("http://") || url.starts_with("https://")) {
            continue;
        }
        let title = page.get("title").and_then(Value::as_str).unwrap_or("").to_string();
        let blob = format!("{url} {title}").to_lowercase();
        let session = if blob.contains("signin") || blob.contains("sign in") || blob.contains("accounts.google.com") || blob.contains("/login") {
            "sign_in"
        } else {
            "visible"
        };
        tabs.push(json!({"url": url, "title": title, "session": session, "browser": "Lumio Chrome"}));
        if tabs.len() >= 12 {
            break;
        }
    }
    tabs
}

pub fn discover_apps() -> Value {
    #[cfg(target_os = "macos")]
    {
        let mut found: Vec<(String, bool)> = Vec::new();
        collect_apps(Path::new("/Applications"), &mut found);
        if let Some(home) = dirs::home_dir() {
            collect_apps(&home.join("Applications"), &mut found);
        }
        found.sort_by(|a, b| a.0.cmp(&b.0));
        let installed: Vec<String> = found.iter().map(|(name, _)| name.clone()).collect();
        let electron: Vec<String> = found.iter().filter(|(_, elec)| *elec).map(|(name, _)| name.clone()).collect();
        let front = lsappinfo_front().unwrap_or_default();
        let tabs = lumio_tabs();
        return json!({
            "ok": true,
            "platform": "macos",
            "front": front,
            "installed": installed,
            "running": running_apps(),
            "electron": electron,
            "tabs": tabs,
            "limitations": [
                "Installed apps come from /Applications and ~/Applications, up to 120.",
                "Running apps are the foreground ones lsappinfo reports.",
                "Tabs are only from Lumio's own Chrome. Everyday Chrome and Safari tabs are not read, and cookies are never read.",
                "A visible page is not proof of who is signed in. Passwords and session cookies stay on this Mac.",
                "Accessibility and Screen Recording are separate permissions and are not bypassed.",
            ],
        });
    }
    #[cfg(windows)]
    {
        return json!({
            "ok": true,
            "platform": "windows",
            "front": "",
            "installed": [],
            "limitations": ["Windows app discovery is not in this build. Do not claim an app was found."],
        });
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        let mut installed = Vec::new();
        if let Ok(entries) = std::fs::read_dir("/usr/share/applications") {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().and_then(|ext| ext.to_str()) != Some("desktop") {
                    continue;
                }
                if let Ok(text) = std::fs::read_to_string(&path) {
                    if let Some(name) = text.lines().find_map(|line| line.strip_prefix("Name=")) {
                        let name = name.trim();
                        if !name.is_empty() && !installed.contains(&name.to_string()) {
                            installed.push(name.to_string());
                        }
                    }
                }
                if installed.len() >= 40 {
                    break;
                }
            }
        }
        installed.sort();
        return json!({
            "ok": true,
            "platform": "linux",
            "front": "",
            "installed": installed,
            "limitations": [
                "Lists names from /usr/share/applications, up to 40.",
                "Wayland may block global input and screen capture. Use the desktop portal. Do not weaken it.",
            ],
        });
    }
}

pub fn launch_app(name: &str) -> Result<String, String> {
    let app = map_app_name(name);
    if app.is_empty() {
        return Err("app name is required".into());
    }
    #[cfg(target_os = "macos")]
    {
        let status = Command::new("open")
            .args(["-a", &app])
            .status()
            .map_err(|err| err.to_string())?;
        if !status.success() {
            return Err(format!("Could not open {app}. Try the exact app name from /Applications."));
        }
        return Ok(app);
    }
    #[cfg(windows)]
    {
        let status = Command::new("cmd")
            .args(["/C", "start", "", &app])
            .status()
            .map_err(|err| err.to_string())?;
        if !status.success() {
            return Err(format!("Could not open {app}"));
        }
        return Ok(app);
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        Err("launch_app is not supported".into())
    }
}

fn map_app_name(raw: &str) -> String {
    let key = raw.trim().to_ascii_lowercase();
    match key.as_str() {
        "chrome" | "google chrome" | "browser" | "google-chrome" => "Google Chrome".into(),
        "finder" | "files" | "file explorer" | "explorer" => {
            if cfg!(windows) {
                "explorer".into()
            } else {
                "Finder".into()
            }
        }
        "safari" => "Safari".into(),
        "code" | "vscode" | "vs code" | "visual studio code" => "Visual Studio Code".into(),
        "excel" | "microsoft excel" => "Microsoft Excel".into(),
        "word" | "microsoft word" => "Microsoft Word".into(),
        "preview" => "Preview".into(),
        "terminal" => "Terminal".into(),
        _ => raw.trim().to_string(),
    }
}

pub fn open_path(path: &Path) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        Command::new("open").arg(path).spawn().map_err(|err| err.to_string())?;
        Ok(())
    }
    #[cfg(windows)]
    {
        Command::new("cmd")
            .args(["/C", "start", "", &path.to_string_lossy()])
            .spawn()
            .map_err(|err| err.to_string())?;
        Ok(())
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        Err("open is not supported".into())
    }
}

fn play_actions(actions: &[Value]) -> Result<(), String> {
    let mut enigo = Enigo::new();
    for step in actions {
        let kind = step.get("kind").and_then(Value::as_str).unwrap_or("click");
        match kind {
            "wait" => {
                let ms = step.get("ms").and_then(Value::as_u64).unwrap_or(400);
                std::thread::sleep(Duration::from_millis(ms));
            }
            "type" | "fill" => {
                let text = step.get("text").and_then(Value::as_str).unwrap_or("");
                enigo.key_sequence(text);
            }
            "key" | "press" => {
                let raw = step.get("key").and_then(Value::as_str).or_else(|| step.get("text").and_then(Value::as_str)).unwrap_or("Return");
                press_key(&mut enigo, raw);
            }
            "scroll" => {
                let dy = step.get("dy").and_then(Value::as_i64).or_else(|| step.get("amount").and_then(Value::as_i64)).unwrap_or(3);
                enigo.mouse_scroll_y(dy as i32);
            }
            _ => {
                if let (Some(x), Some(y)) = (step.get("x").and_then(Value::as_i64), step.get("y").and_then(Value::as_i64)) {
                    enigo.mouse_move_to(x as i32, y as i32);
                    std::thread::sleep(Duration::from_millis(30));
                    enigo.mouse_click(MouseButton::Left);
                }
            }
        }
        std::thread::sleep(Duration::from_millis(80));
    }
    Ok(())
}

fn press_key(enigo: &mut Enigo, raw: &str) {
    let lower = raw.to_lowercase();
    let key = match lower.as_str() {
        "enter" | "return" => Key::Return,
        "tab" => Key::Tab,
        "escape" | "esc" => Key::Escape,
        "backspace" => Key::Backspace,
        "space" => Key::Space,
        "up" | "arrowup" => Key::UpArrow,
        "down" | "arrowdown" => Key::DownArrow,
        "left" | "arrowleft" => Key::LeftArrow,
        "right" | "arrowright" => Key::RightArrow,
        "meta" | "cmd" | "super" => Key::Meta,
        "ctrl" | "control" => Key::Control,
        "alt" | "option" => Key::Alt,
        other if other.len() == 1 => {
            if let Some(ch) = other.chars().next() {
                enigo.key_click(Key::Layout(ch));
            }
            return;
        }
        _ => Key::Return,
    };
    enigo.key_click(key);
}

pub async fn snapshot() -> Result<Value, String> {
    let shot = tokio::task::spawn_blocking(screenshot_b64)
        .await
        .map_err(|err| err.to_string())??;
    let title = tokio::task::spawn_blocking(front_title)
        .await
        .unwrap_or_else(|_| "This computer".into());
    Ok(json!({
        "ok": true,
        "ran_on": "local_computer",
        "provider": "local",
        "state": "running",
        "url": "",
        "title": title,
        "text": title,
        "screenshot_b64": shot,
        "note": "This is the real desktop on this computer (files, apps, browser).",
    }))
}

pub async fn boot() -> Result<Value, String> {
    start_input_listen();
    let _ = tokio::task::spawn_blocking(crate::grants::ensure_common_folders).await;
    Ok(json!({
        "ok": true,
        "ran_on": "local_computer",
        "provider": "local",
        "state": "running",
        "url": "",
        "title": "This computer",
        "folders": crate::grants::snapshot().folders,
        "note": "Desktop, Documents, and Downloads are granted. Use list_dir on ~/Desktop. Do not say access is limited to ~/Lumio. Teaching records this computer's own apps and does not open a page.",
    }))
}

pub async fn act(actions: &[Value]) -> Result<Value, String> {
    let steps = actions.to_vec();
    tokio::task::spawn_blocking(move || play_actions(&steps))
        .await
        .map_err(|err| err.to_string())??;
    snapshot().await
}


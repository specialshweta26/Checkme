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
            std::thread::sleep(Duration::from_secs(2));
        }
    });
    std::thread::spawn(|| {
        let _ = rdev::listen(move |event| {
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
        });
    });
}

pub fn set_teaching(on: bool) {
    TEACHING.store(on, Ordering::SeqCst);
    if on {
        start_input_listen();
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

pub fn drain_teach() -> Vec<Value> {
    teach_buf().lock().map(|mut buf| buf.drain(..).take(200).collect()).unwrap_or_default()
}

pub fn front_title() -> String {
    #[cfg(target_os = "macos")]
    {
        let out = Command::new("osascript")
            .args(["-e", "tell application \"System Events\" to get name of first application process whose frontmost is true"])
            .output()
            .ok();
        if let Some(out) = out {
            let text = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if !text.is_empty() {
                return text;
            }
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
    let path = std::env::temp_dir().join("lumio-desktop.png");
    #[cfg(target_os = "macos")]
    {
        let status = Command::new("screencapture")
            .args(["-x", "-t", "png"])
            .arg(&path)
            .status()
            .map_err(|err| err.to_string())?;
        if !status.success() {
            return Err("Allow Screen Recording for Lumio in System Settings → Privacy & Security.".into());
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


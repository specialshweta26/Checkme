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
static LISTENING: AtomicBool = AtomicBool::new(false);

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
                        "window": front_title(),
                    }));
                }
                rdev::EventType::Wheel { delta_y, .. } => {
                    let direction = if delta_y < 0 { "down" } else { "up" };
                    push_event(json!({ "kind": "scroll", "direction": direction, "window": front_title() }));
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

fn map_key(key: rdev::Key) -> Option<Value> {
    let window = front_title();
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
    Ok(json!({
        "ok": true,
        "ran_on": "local_computer",
        "provider": "local",
        "state": "running",
        "url": "",
        "title": "This computer",
        "note": "This computer is ready. Teaching records what you do here; no live mirror.",
    }))
}

pub async fn act(actions: &[Value]) -> Result<Value, String> {
    let steps = actions.to_vec();
    tokio::task::spawn_blocking(move || play_actions(&steps))
        .await
        .map_err(|err| err.to_string())??;
    snapshot().await
}


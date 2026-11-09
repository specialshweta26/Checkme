//! The rest of the computer: its windows, screens and clipboard. Everything goes through the operating system's own
//! scripting, so macOS asks the user once for Accessibility permission and nothing runs without it.

use std::io::Write;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

use serde_json::{json, Value};

const MAX_CLIPBOARD_CHARS: usize = 20_000;
const WINDOWS_SCRIPT: &str = r#"
set out to ""
tell application "System Events"
  repeat with p in (processes whose background only is false)
    set appName to name of p
    set isFront to frontmost of p
    try
      repeat with w in windows of p
        try
          set {x, y} to position of w
          set {wide, high} to size of w
          set out to out & appName & tab & (name of w as text) & tab & x & tab & y & tab & wide & tab & high & tab & isFront & linefeed
        end try
      end repeat
    end try
  end repeat
end tell
return out
"#;
const FOCUS_SCRIPT: &str = r#"
on run argv
  set appName to item 1 of argv
  set wanted to item 2 of argv
  tell application "System Events" to tell process appName
    set frontmost to true
    if wanted is not "" then
      repeat with w in windows
        if (name of w as text) contains wanted then
          perform action "AXRaise" of w
          return name of w as text
        end if
      end repeat
      error "No window of " & appName & " has that title"
    end if
  end tell
  return appName
end run
"#;
const PLACE_SCRIPT: &str = r#"
on run argv
  set appName to item 1 of argv
  set wanted to item 2 of argv
  tell application "System Events" to tell process appName
    repeat with w in windows
      if wanted is "" or (name of w as text) contains wanted then
        if item 3 of argv is not "" then set position of w to {(item 3 of argv) as integer, (item 4 of argv) as integer}
        if item 5 of argv is not "" then set size of w to {(item 5 of argv) as integer, (item 6 of argv) as integer}
        return name of w as text
      end if
    end repeat
  end tell
  error "No window of " & appName & " has that title"
end run
"#;
const MINIMIZE_SCRIPT: &str = r#"
on run argv
  set appName to item 1 of argv
  set wanted to item 2 of argv
  tell application "System Events" to tell process appName
    repeat with w in windows
      if wanted is "" or (name of w as text) contains wanted then
        set value of attribute "AXMinimized" of w to true
        return name of w as text
      end if
    end repeat
  end tell
  error "No window of " & appName & " has that title"
end run
"#;
const SCREENS_JXA: &str = "ObjC.import('AppKit');const s=$.NSScreen.screens;const out=[];for(let i=0;i<s.count;i++){const n=s.objectAtIndex(i);const f=n.frame;out.push({x:f.origin.x,y:f.origin.y,width:f.size.width,height:f.size.height,scale:n.backingScaleFactor,main:i===0});}JSON.stringify(out)";
const WIN_TYPES: &str = "Add-Type -Namespace L -Name W -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool GetWindowRect(IntPtr h, out RECT r); [DllImport(\"user32.dll\")] public static extern bool MoveWindow(IntPtr h,int x,int y,int w,int t,bool p); [DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h,int c); [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h); public struct RECT { public int Left, Top, Right, Bottom; }';";

fn run_with_timeout(mut cmd: Command, input: Option<&str>, seconds: u64) -> Result<String, String> {
    let mut child = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| err.to_string())?;
    if let Some(mut stdin) = child.stdin.take() {
        if let Some(text) = input {
            let _ = stdin.write_all(text.as_bytes());
        }
    }
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(child.wait_with_output());
    });
    let output = match rx.recv_timeout(Duration::from_secs(seconds)) {
        Ok(Ok(output)) => output,
        Ok(Err(err)) => return Err(err.to_string()),
        Err(_) => return Err("the system took too long to answer".into()),
    };
    if !output.status.success() {
        return Err(friendly(String::from_utf8_lossy(&output.stderr).trim()));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim_end().to_string())
}

fn friendly(message: &str) -> String {
    if message.contains("not allowed assistive access") || message.contains("-1719") || message.contains("-25211") {
        return "Allow Lumio in System Settings → Privacy & Security → Accessibility, then try again.".into();
    }
    message.lines().last().unwrap_or("the system refused").chars().take(300).collect()
}

fn applescript(script: &str, args: &[&str]) -> Result<String, String> {
    let mut cmd = Command::new("osascript");
    cmd.arg("-");
    cmd.args(args);
    run_with_timeout(cmd, Some(script), 15)
}

fn powershell(script: &str, input: Option<&str>) -> Result<String, String> {
    let mut cmd = Command::new("powershell");
    cmd.args(["-NoProfile", "-NonInteractive", "-Command", script]);
    run_with_timeout(cmd, input, 15)
}

fn parse_windows(out: &str) -> Vec<Value> {
    out.lines()
        .filter_map(|line| {
            let cols: Vec<&str> = line.split('\t').collect();
            if cols.len() < 7 {
                return None;
            }
            let num = |i: usize| cols[i].trim().parse::<i64>().ok();
            Some(json!({
                "app": cols[0],
                "title": cols[1],
                "x": num(2),
                "y": num(3),
                "width": num(4),
                "height": num(5),
                "front": cols[6].trim() == "true",
            }))
        })
        .collect()
}

fn text_arg<'a>(args: &'a Value, key: &str) -> &'a str {
    args.get(key).and_then(Value::as_str).unwrap_or("")
}

fn number_arg(args: &Value, key: &str) -> String {
    args.get(key).and_then(Value::as_i64).map(|n| n.to_string()).unwrap_or_default()
}

fn windows_tool(args: &Value) -> Result<Value, String> {
    let action = text_arg(args, "action");
    let app = text_arg(args, "app");
    let title = text_arg(args, "title");
    if action != "list" && app.is_empty() {
        return Err("app is required (take it from action=list)".into());
    }
    let win_title = title.replace('\'', "''");
    let win_app = app.replace('\'', "''");
    let win_find = format!(
        "{WIN_TYPES} $p = Get-Process | Where-Object {{ $_.MainWindowHandle -ne 0 -and $_.ProcessName -like '*{win_app}*' -and $_.MainWindowTitle -like '*{win_title}*' }} | Select-Object -First 1; if (-not $p) {{ throw 'No window with that app and title' }};"
    );
    match action {
        "list" => {
            if cfg!(target_os = "macos") {
                return Ok(json!({ "ok": true, "windows": parse_windows(&applescript(WINDOWS_SCRIPT, &[])?) }));
            }
            let script = format!(
                "{WIN_TYPES} Get-Process | Where-Object {{ $_.MainWindowHandle -ne 0 }} | ForEach-Object {{ $r = New-Object L.W+RECT; [void][L.W]::GetWindowRect($_.MainWindowHandle, [ref]$r); \"$($_.ProcessName)`t$($_.MainWindowTitle)`t$($r.Left)`t$($r.Top)`t$($r.Right - $r.Left)`t$($r.Bottom - $r.Top)`tfalse\" }}"
            );
            Ok(json!({ "ok": true, "windows": parse_windows(&powershell(&script, None)?) }))
        }
        "focus" => {
            if cfg!(target_os = "macos") {
                applescript(FOCUS_SCRIPT, &[app, title])?;
            } else {
                powershell(&format!("{win_find} [void][L.W]::ShowWindow($p.MainWindowHandle, 9); [void][L.W]::SetForegroundWindow($p.MainWindowHandle)"), None)?;
            }
            Ok(json!({ "ok": true, "app": app, "title": title }))
        }
        "move" | "resize" | "move_resize" => {
            let (x, y) = (number_arg(args, "x"), number_arg(args, "y"));
            let (width, height) = (number_arg(args, "width"), number_arg(args, "height"));
            if cfg!(target_os = "macos") {
                applescript(PLACE_SCRIPT, &[app, title, &x, &y, &width, &height])?;
            } else {
                let keep = |value: &str, fallback: &str| if value.is_empty() { fallback.to_string() } else { value.to_string() };
                let script = format!(
                    "{win_find} $r = New-Object L.W+RECT; [void][L.W]::GetWindowRect($p.MainWindowHandle, [ref]$r); [void][L.W]::MoveWindow($p.MainWindowHandle, {}, {}, {}, {}, $true)",
                    keep(&x, "$r.Left"),
                    keep(&y, "$r.Top"),
                    keep(&width, "($r.Right - $r.Left)"),
                    keep(&height, "($r.Bottom - $r.Top)"),
                );
                powershell(&script, None)?;
            }
            Ok(json!({ "ok": true, "app": app, "title": title }))
        }
        "minimize" => {
            if cfg!(target_os = "macos") {
                applescript(MINIMIZE_SCRIPT, &[app, title])?;
            } else {
                powershell(&format!("{win_find} [void][L.W]::ShowWindow($p.MainWindowHandle, 6)"), None)?;
            }
            Ok(json!({ "ok": true, "app": app, "title": title }))
        }
        other => Err(format!("unknown window action {other}; use list, focus, move_resize or minimize")),
    }
}

fn screens_tool() -> Result<Value, String> {
    let raw = if cfg!(target_os = "macos") {
        let mut cmd = Command::new("osascript");
        cmd.args(["-l", "JavaScript", "-e", SCREENS_JXA]);
        run_with_timeout(cmd, None, 15)?
    } else {
        powershell(
            "Add-Type -AssemblyName System.Windows.Forms; ConvertTo-Json -InputObject @([Windows.Forms.Screen]::AllScreens | ForEach-Object { @{ x = $_.Bounds.X; y = $_.Bounds.Y; width = $_.Bounds.Width; height = $_.Bounds.Height; main = $_.Primary } })",
            None,
        )?
    };
    let screens: Value = serde_json::from_str(raw.trim()).map_err(|err| err.to_string())?;
    Ok(json!({ "ok": true, "screens": screens }))
}

fn clipboard_tool(args: &Value) -> Result<Value, String> {
    match text_arg(args, "action") {
        "read" => {
            let text = if cfg!(target_os = "macos") {
                run_with_timeout(Command::new("pbpaste"), None, 5)?
            } else {
                powershell("Get-Clipboard -Raw", None)?
            };
            let clipped: String = text.chars().take(MAX_CLIPBOARD_CHARS).collect();
            Ok(json!({ "ok": true, "text": clipped }))
        }
        "write" => {
            let text = text_arg(args, "text");
            if cfg!(target_os = "macos") {
                run_with_timeout(Command::new("pbcopy"), Some(text), 5)?;
            } else {
                powershell("$input | Out-String | Set-Clipboard", Some(text))?;
            }
            Ok(json!({ "ok": true, "chars": text.chars().count() }))
        }
        other => Err(format!("unknown clipboard action {other}; use read or write")),
    }
}

pub fn run(tool: &str, args: &Value) -> Value {
    let out = match tool {
        "computer_windows" => windows_tool(args),
        "computer_screens" => screens_tool(),
        "computer_clipboard" => clipboard_tool(args),
        other => Err(format!("{other} is not supported on this computer")),
    };
    let mut result = out.unwrap_or_else(|err| json!({ "ok": false, "error": err }));
    result["ran_on"] = json!("local_computer");
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn window_lines_become_rows_and_bad_lines_are_skipped() {
        let rows = parse_windows("Mail\tInbox\t10\t20\t800\t600\ttrue\nnot a window\nExcel\tBook1\t0\t0\t100\t100\tfalse");
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["app"], "Mail");
        assert_eq!(rows[0]["width"], 800);
        assert_eq!(rows[0]["front"], true);
        assert_eq!(rows[1]["front"], false);
    }

    #[test]
    fn permission_errors_say_what_to_do() {
        assert!(friendly("execution error: osascript is not allowed assistive access. (-1719)").contains("Accessibility"));
        assert_eq!(friendly("line one\nline two"), "line two");
    }

    #[test]
    fn unknown_actions_are_refused() {
        assert_eq!(run("computer_clipboard", &json!({ "action": "steal" }))["ok"], false);
        assert_eq!(run("computer_windows", &json!({ "action": "close", "app": "Mail" }))["ok"], false);
    }
}

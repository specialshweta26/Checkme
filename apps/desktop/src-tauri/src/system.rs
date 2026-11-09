//! The rest of the computer: its windows, screens, clipboard and the controls inside apps. Everything goes through the operating system's own
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
/// One line per control: id, role, name, value, x, y, width, height. Password fields never give their value.
const UI_READ_SCRIPT: &str = r#"
global out, seen, maxNodes, maxDepth
on clean(raw)
  try
    set t to raw as text
  on error
    return ""
  end try
  set AppleScript's text item delimiters to {tab, linefeed, return}
  set bits to text items of t
  set AppleScript's text item delimiters to " "
  set t to bits as text
  set AppleScript's text item delimiters to ""
  if (count of t) > 120 then set t to text 1 thru 120 of t
  return t
end clean
on walk(el, pathText, depth)
  if seen >= maxNodes then return
  set seen to seen + 1
  tell application "System Events"
    set r to ""
    set sr to ""
    set n to ""
    set v to ""
    set x to ""
    set y to ""
    set wide to ""
    set high to ""
    try
      set r to role of el
    end try
    try
      set sr to subrole of el
    end try
    try
      set n to title of el
      if n is missing value then set n to ""
    end try
    -- "description" in System Events is only the generic role ("button"): the real label is in these attributes.
    repeat with attributeName in {"AXDescription", "AXIdentifier", "AXHelp"}
      if n is "" then
        try
          set n to value of attribute attributeName of el
          if n is missing value then set n to ""
        end try
      end if
    end repeat
    if sr is not "" and sr is not missing value then set r to sr
    if sr is not "AXSecureTextField" then
      try
        set v to value of el
        if v is missing value then set v to ""
      end try
    end if
    try
      set {x, y} to position of el
      set {wide, high} to size of el
    end try
  end tell
  set out to out & pathText & tab & r & tab & my clean(n) & tab & my clean(v) & tab & x & tab & y & tab & wide & tab & high & linefeed
  if depth < maxDepth then
    tell application "System Events" to set kids to UI elements of el
    repeat with i from 1 to count of kids
      my walk(item i of kids, pathText & "." & i, depth + 1)
    end repeat
  end if
end walk
on run argv
  set appName to item 1 of argv
  set wanted to item 2 of argv
  set maxDepth to (item 3 of argv) as integer
  set maxNodes to 250
  set seen to 0
  set out to ""
  tell application "System Events" to tell process appName
    if (count of windows) is 0 then error appName & " has no open window"
    set found to 1
    if wanted is not "" then
      set found to 0
      repeat with i from 1 to count of windows
        if (name of window i as text) contains wanted then
          set found to i
          exit repeat
        end if
      end repeat
      if found is 0 then error "No window of " & appName & " has that title"
    end if
    set w to window found
  end tell
  my walk(w, found as text, 0)
  return out
end run
"#;
const UI_ACT_SCRIPT: &str = r#"
on run argv
  set appName to item 1 of argv
  set pathText to item 2 of argv
  set verb to item 3 of argv
  set AppleScript's text item delimiters to "."
  set parts to text items of pathText
  set AppleScript's text item delimiters to ""
  tell application "System Events" to tell process appName
    set frontmost to true
    set el to window ((item 1 of parts) as integer)
    repeat with i from 2 to count of parts
      set el to UI element ((item i of parts) as integer) of el
    end repeat
    if verb is "press" then
      try
        perform action "AXPress" of el
      on error
        click el
      end try
    else if verb is "set_value" then
      set value of el to (item 4 of argv)
    else if verb is "focus" then
      set focused of el to true
    end if
    try
      return value of el as text
    on error
      return ""
    end try
  end tell
end run
"#;
const UI_MENU_SCRIPT: &str = r#"
on run argv
  set appName to item 1 of argv
  tell application "System Events" to tell process appName
    set frontmost to true
    set el to menu bar item (item 2 of argv) of menu bar 1
    repeat with i from 3 to count of argv
      set el to menu item (item i of argv) of menu 1 of el
    end repeat
    click el
  end tell
  return "ok"
end run
"#;
const WIN_UI_COMMON: &str = "Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes; $A=[Windows.Automation.AutomationElement]; $T=[Windows.Automation.TreeWalker]::ControlViewWalker;";
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
    if message.contains("not allowed assistive access") || message.contains("-25211") {
        return "Allow Lumio in System Settings → Privacy & Security → Accessibility, then try again.".into();
    }
    if message.contains("-1719") {
        return "That control or window is no longer there. Read the window again and use a fresh id.".into();
    }
    if message.contains("-600") || message.contains("-1728") || message.contains("-609") {
        return "That app isn't responding or has quit. Open it with launch_app, check what it shows, and carry on from there.".into();
    }
    message.lines().last().unwrap_or("the system refused").chars().take(300).collect()
}

fn applescript(script: &str, args: &[&str]) -> Result<String, String> {
    applescript_for(script, args, 15)
}

fn applescript_for(script: &str, args: &[&str], seconds: u64) -> Result<String, String> {
    let mut cmd = Command::new("osascript");
    cmd.arg("-");
    cmd.args(args);
    run_with_timeout(cmd, Some(script), seconds)
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

fn parse_controls(out: &str) -> Vec<Value> {
    out.lines()
        .filter_map(|line| {
            let cols: Vec<&str> = line.split('\t').collect();
            if cols.len() < 8 {
                return None;
            }
            let num = |i: usize| cols[i].trim().parse::<i64>().ok();
            Some(json!({
                "id": cols[0],
                "role": cols[1],
                "name": cols[2],
                "value": cols[3],
                "x": num(4),
                "y": num(5),
                "width": num(6),
                "height": num(7),
            }))
        })
        .collect()
}

/// Control ids are dotted numbers, so one can't smuggle anything into a script.
fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.split('.').all(|part| !part.is_empty() && part.chars().all(|c| c.is_ascii_digit()))
}

fn win_window(app: &str, title: &str) -> String {
    let (app, title) = (app.replace('\'', "''"), title.replace('\'', "''"));
    format!(
        "{WIN_UI_COMMON} $p = Get-Process | Where-Object {{ $_.MainWindowHandle -ne 0 -and $_.ProcessName -like '*{app}*' -and $_.MainWindowTitle -like '*{title}*' }} | Select-Object -First 1; if (-not $p) {{ throw 'No window with that app and title' }}; $root = $A::FromHandle($p.MainWindowHandle);"
    )
}

fn ui_read(app: &str, title: &str, depth: i64) -> Result<Value, String> {
    let raw = if cfg!(target_os = "macos") {
        applescript_for(UI_READ_SCRIPT, &[app, title, &depth.to_string()], 40)?
    } else {
        let script = format!(
            "{} $script:n = 0; function Walk($e, $id, $d) {{ if ($script:n -ge 250) {{ return }}; $script:n++; $c = $e.Current; $v = ''; $vp = $null; if (-not $c.IsPassword -and $e.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {{ $v = $vp.Current.Value }}; $b = $c.BoundingRectangle; $clean = {{ param($t) ([string]$t -replace '[\t\r\n]', ' ') }}; \"$id`t$($c.ControlType.ProgrammaticName)`t$(& $clean $c.Name)`t$(& $clean $v)`t$([int]$b.X)`t$([int]$b.Y)`t$([int]$b.Width)`t$([int]$b.Height)\"; if ($d -lt {depth}) {{ $k = $T.GetFirstChild($e); $i = 1; while ($k) {{ Walk $k \"$id.$i\" ($d + 1); $i++; $k = $T.GetNextSibling($k) }} }} }}; Walk $root '1' 0",
            win_window(app, title)
        );
        powershell(&script, None)?
    };
    Ok(json!({ "ok": true, "controls": parse_controls(&raw) }))
}

fn ui_act(app: &str, title: &str, id: &str, verb: &str, text: &str) -> Result<Value, String> {
    if !valid_id(id) {
        return Err("id must come from action=read".into());
    }
    let value = if cfg!(target_os = "macos") {
        applescript(UI_ACT_SCRIPT, &[app, id, verb, text])?
    } else {
        let steps: Vec<&str> = id.split('.').skip(1).collect();
        let quoted = text.replace('\'', "''");
        let action = match verb {
            "press" => "$o = $null; if ($e.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$o)) { $o.Invoke() } elseif ($e.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern, [ref]$o)) { $o.Toggle() } elseif ($e.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$o)) { $o.Select() } else { throw 'This control cannot be pressed' }".to_string(),
            "set_value" => format!("$o = $null; if ($e.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern, [ref]$o)) {{ $o.SetValue('{quoted}') }} else {{ throw 'This control takes no value' }}"),
            _ => "$e.SetFocus()".to_string(),
        };
        let script = format!(
            "{} $e = $root; foreach ($s in @({})) {{ $k = $T.GetFirstChild($e); for ($i = 1; $i -lt $s; $i++) {{ $k = $T.GetNextSibling($k) }}; if (-not $k) {{ throw 'That control has gone; read the window again' }}; $e = $k }}; {action}",
            win_window(app, title),
            steps.join(","),
        );
        powershell(&script, None)?
    };
    Ok(json!({ "ok": true, "id": id, "value": value }))
}

fn ui_menu(app: &str, menu: &str) -> Result<Value, String> {
    if !cfg!(target_os = "macos") {
        return Err("On Windows, menus are controls: read the window and press them".into());
    }
    let parts: Vec<&str> = menu.split('>').map(str::trim).filter(|p| !p.is_empty()).collect();
    if parts.len() < 2 {
        return Err("menu needs a path such as File > Save".into());
    }
    let mut args = vec![app];
    args.extend(parts.iter().copied());
    applescript(UI_MENU_SCRIPT, &args)?;
    Ok(json!({ "ok": true, "menu": menu }))
}

fn ui_tool(args: &Value) -> Result<Value, String> {
    let app = text_arg(args, "app");
    if app.is_empty() {
        return Err("app is required (take it from computer_windows)".into());
    }
    let title = text_arg(args, "title");
    match text_arg(args, "action") {
        "read" => ui_read(app, title, args.get("depth").and_then(Value::as_i64).unwrap_or(6).clamp(1, 10)),
        "press" => ui_act(app, title, text_arg(args, "id"), "press", ""),
        "set_value" => ui_act(app, title, text_arg(args, "id"), "set_value", text_arg(args, "text")),
        "focus" => ui_act(app, title, text_arg(args, "id"), "focus", ""),
        "menu" => ui_menu(app, text_arg(args, "menu")),
        other => Err(format!("unknown action {other}; use read, press, set_value, focus or menu")),
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

fn notify_tool(args: &Value) -> Result<Value, String> {
    let title = text_arg(args, "title");
    let text = text_arg(args, "text");
    if cfg!(target_os = "macos") {
        applescript("on run argv\n  display notification (item 2 of argv) with title (item 1 of argv)\nend run", &[title, text])?;
    } else {
        let (title, text) = (title.replace('\'', "''"), text.replace('\'', "''"));
        let script = format!(
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; $n = New-Object Windows.Forms.NotifyIcon; $n.Icon = [Drawing.SystemIcons]::Information; $n.Visible = $true; $n.ShowBalloonTip(5000, '{title}', '{text}', 'Info'); Start-Sleep -Seconds 1; $n.Dispose()"
        );
        powershell(&script, None)?;
    }
    Ok(json!({ "ok": true }))
}

pub fn run(tool: &str, args: &Value) -> Value {
    let out = match tool {
        "computer_notify" => notify_tool(args),
        "computer_windows" => windows_tool(args),
        "computer_screens" => screens_tool(),
        "computer_clipboard" => clipboard_tool(args),
        "computer_ui" => ui_tool(args),
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
    fn control_lines_become_rows() {
        let rows = parse_controls("1.2\tAXButton\tSave\t\t10\t20\t60\t24\nbad line");
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["id"], "1.2");
        assert_eq!(rows[0]["name"], "Save");
        assert_eq!(rows[0]["height"], 24);
    }

    #[test]
    fn control_ids_are_only_dotted_numbers() {
        assert!(valid_id("1.2.10"));
        for bad in ["", "1..2", "1.a", "1; rm -rf", ".1"] {
            assert!(!valid_id(bad), "{bad}");
        }
    }

    #[test]
    fn permission_errors_say_what_to_do() {
        assert!(friendly("execution error: osascript is not allowed assistive access. (-1719)").contains("Accessibility"));
        assert_eq!(friendly("line one\nline two"), "line two");
        assert!(friendly("execution error: Invalid index. (-1719)").contains("Read the window again"));
        assert!(friendly("execution error: Application isn't running. (-600)").contains("launch_app"));
    }

    #[test]
    fn unknown_actions_are_refused() {
        assert_eq!(run("computer_clipboard", &json!({ "action": "steal" }))["ok"], false);
        assert_eq!(run("computer_windows", &json!({ "action": "close", "app": "Mail" }))["ok"], false);
    }
}

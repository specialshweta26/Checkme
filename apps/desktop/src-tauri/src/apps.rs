//! Office apps through their own scripting: faster than pressing controls, and each command names its document, so it
//! can't land on another file the user has open. Numbers, Pages and Keynote are tested on macOS. Excel, Word and
//! PowerPoint are written the same way but have not yet been run against the real apps.

use serde_json::{json, Value};

use crate::system::{applescript_for, powershell};

const WAIT: u64 = 60;

// Values arrive as arguments, never inside the script: n:40 is a number, s:Tea is text, and text starting with = is a
// formula in the spreadsheet apps.
const VALUE_HANDLER: &str = r#"
on typed(raw)
  if raw starts with "n:" then return (text 3 thru -1 of raw) as real
  if (count of raw) > 2 then return text 3 thru -1 of raw
  return ""
end typed
on joined(values, separator)
  set AppleScript's text item delimiters to separator
  set out to values as text
  set AppleScript's text item delimiters to ""
  return out
end joined
on clean(raw)
  if raw is missing value then return ""
  set AppleScript's text item delimiters to {tab, linefeed, return}
  set bits to text items of (raw as text)
  set AppleScript's text item delimiters to " "
  set out to bits as text
  set AppleScript's text item delimiters to ""
  return out
end clean
"#;
const LIST_SCRIPT: &str = r#"
on run argv
  set appId to item 1 of argv
  if not (application id appId is running) then return ""
  tell application id appId
    set out to ""
    repeat with d in documents
      set out to out & (name of d) & linefeed
    end repeat
    return out
  end tell
end run
"#;
const NEW_IWORK: &str = r#"
on run argv
  tell application id (item 1 of argv)
    activate
    set d to make new document
    return name of d
  end tell
end run
"#;
const NUMBERS_SET: &str = r#"
on run argv
  set docName to item 1 of argv
  tell application id "com.apple.Numbers"
    tell table 1 of active sheet of document docName
      repeat with i from 2 to (count of argv) by 2
        set value of cell (item i of argv) to my typed(item (i + 1) of argv)
      end repeat
    end tell
  end tell
  return "ok"
end run
"#;
const NUMBERS_READ: &str = r#"
on run argv
  set out to ""
  tell application id "com.apple.Numbers"
    tell table 1 of active sheet of document (item 1 of argv)
      repeat with r in rows
        set line_ to {}
        repeat with c in cells of r
          set end of line_ to my clean(formatted value of c)
        end repeat
        set out to out & my joined(line_, tab) & linefeed
      end repeat
    end tell
  end tell
  return out
end run
"#;
const NUMBERS_EXPORT: &str = r#"
on run argv
  set target to POSIX file (item 2 of argv)
  tell application id "com.apple.Numbers"
    set d to document (item 1 of argv)
    if item 3 of argv is "csv" then
      export d to target as CSV
    else if item 3 of argv is "xlsx" then
      export d to target as Microsoft Excel
    else
      export d to target as PDF
    end if
  end tell
  return "ok"
end run
"#;
const PAGES_SET: &str = r#"
on run argv
  tell application id "com.apple.Pages"
    set body text of document (item 1 of argv) to (item 2 of argv)
  end tell
  return "ok"
end run
"#;
const PAGES_READ: &str = r#"
on run argv
  tell application id "com.apple.Pages"
    return body text of document (item 1 of argv)
  end tell
end run
"#;
const PAGES_EXPORT: &str = r#"
on run argv
  set target to POSIX file (item 2 of argv)
  tell application id "com.apple.Pages"
    set d to document (item 1 of argv)
    if item 3 of argv is "docx" then
      export d to target as Microsoft Word
    else
      export d to target as PDF
    end if
  end tell
  return "ok"
end run
"#;
const KEYNOTE_ADD: &str = r#"
on run argv
  tell application id "com.apple.Keynote"
    tell document (item 1 of argv)
      set s to make new slide
      try
        set object text of default title item of s to (item 2 of argv)
      end try
      try
        set object text of default body item of s to (item 3 of argv)
      end try
      if item 4 of argv is not "" then set presenter notes of s to (item 4 of argv)
      return (count of slides) as text
    end tell
  end tell
end run
"#;
const KEYNOTE_READ: &str = r#"
on run argv
  set out to ""
  tell application id "com.apple.Keynote"
    repeat with s in slides of document (item 1 of argv)
      set t to ""
      set b to ""
      try
        set t to object text of default title item of s
      end try
      try
        set b to object text of default body item of s
      end try
      set out to out & my clean(t) & tab & my clean(b) & linefeed
    end repeat
  end tell
  return out
end run
"#;
const KEYNOTE_EXPORT: &str = r#"
on run argv
  set target to POSIX file (item 2 of argv)
  tell application id "com.apple.Keynote"
    set d to document (item 1 of argv)
    if item 3 of argv is "pptx" then
      export d to target as Microsoft PowerPoint
    else
      export d to target as PDF
    end if
  end tell
  return "ok"
end run
"#;
const EXCEL_MAC_NEW: &str = r#"
on run argv
  tell application id "com.microsoft.Excel"
    activate
    set w to make new workbook
    return name of w
  end tell
end run
"#;
const EXCEL_MAC_SET: &str = r#"
on run argv
  tell application id "com.microsoft.Excel"
    tell active sheet of workbook (item 1 of argv)
      repeat with i from 2 to (count of argv) by 2
        set raw to my typed(item (i + 1) of argv)
        if class of raw is text and raw starts with "=" then
          set formula of range (item i of argv) to raw
        else
          set value of range (item i of argv) to raw
        end if
      end repeat
    end tell
  end tell
  return "ok"
end run
"#;
const EXCEL_MAC_READ: &str = r#"
on run argv
  set out to ""
  tell application id "com.microsoft.Excel"
    set grid to value of used range of active sheet of workbook (item 1 of argv)
  end tell
  repeat with r in grid
    set line_ to {}
    repeat with c in r
      set end of line_ to my clean(c as text)
    end repeat
    set out to out & my joined(line_, tab) & linefeed
  end repeat
  return out
end run
"#;
const EXCEL_MAC_SAVE: &str = r#"
on run argv
  tell application id "com.microsoft.Excel"
    save workbook as workbook (item 1 of argv) filename (item 2 of argv)
  end tell
  return "ok"
end run
"#;
const WORD_MAC_NEW: &str = r#"
on run argv
  tell application id "com.microsoft.Word"
    activate
    set d to make new document
    return name of d
  end tell
end run
"#;
const WORD_MAC_SET: &str = r#"
on run argv
  tell application id "com.microsoft.Word"
    set content of text object of document (item 1 of argv) to (item 2 of argv)
  end tell
  return "ok"
end run
"#;
const WORD_MAC_READ: &str = r#"
on run argv
  tell application id "com.microsoft.Word"
    return content of text object of document (item 1 of argv)
  end tell
end run
"#;
const WORD_MAC_SAVE: &str = r#"
on run argv
  tell application id "com.microsoft.Word"
    save as document (item 1 of argv) file name (item 2 of argv)
  end tell
  return "ok"
end run
"#;
// Windows: arguments come as JSON on standard input, so nothing is pasted into the script.
const WIN_ARGS: &str = "$a = ($input | Out-String) | ConvertFrom-Json;";
const WIN_EXCEL: &str = "$x = try { [Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application') } catch { New-Object -ComObject Excel.Application }; $x.Visible = $true;";
const WIN_WORD: &str = "$x = try { [Runtime.InteropServices.Marshal]::GetActiveObject('Word.Application') } catch { New-Object -ComObject Word.Application }; $x.Visible = $true;";
const WIN_POWERPOINT: &str = "$x = try { [Runtime.InteropServices.Marshal]::GetActiveObject('PowerPoint.Application') } catch { New-Object -ComObject PowerPoint.Application }; $x.Visible = -1;";

fn text<'a>(args: &'a Value, key: &str) -> &'a str {
    args.get(key).and_then(Value::as_str).unwrap_or("")
}

/// A cell value as an argument: numbers keep their type, and everything else is text (a leading = makes it a formula).
fn typed_value(value: &Value) -> String {
    match value {
        Value::Number(n) => format!("n:{n}"),
        Value::Bool(b) => format!("s:{b}"),
        Value::String(s) => format!("s:{s}"),
        _ => "s:".to_string(),
    }
}

fn valid_cell(cell: &str) -> bool {
    let letters = cell.chars().take_while(char::is_ascii_alphabetic).count();
    let digits = cell[letters..].chars().all(|c| c.is_ascii_digit());
    (1..=3).contains(&letters) && cell.len() > letters && digits
}

fn cell_args(args: &Value) -> Result<Vec<String>, String> {
    let cells = args.get("cells").and_then(Value::as_array).ok_or_else(|| "cells is required: [{\"cell\": \"A1\", \"value\": 40}]".to_string())?;
    let mut out = Vec::with_capacity(cells.len() * 2);
    for row in cells {
        let cell = text(row, "cell");
        if !valid_cell(cell) {
            return Err(format!("{cell} isn't a cell reference such as A1"));
        }
        out.push(cell.to_string());
        out.push(typed_value(row.get("value").unwrap_or(&Value::Null)));
    }
    Ok(out)
}

fn run_script(script: &str, args: &[&str]) -> Result<String, String> {
    applescript_for(&format!("{VALUE_HANDLER}\n{script}"), args, WAIT)
}

fn with_document(script: &str, document: &str, extra: &[String]) -> Result<String, String> {
    let mut all = vec![document];
    all.extend(extra.iter().map(String::as_str));
    run_script(script, &all)
}

fn rows_of(raw: &str) -> Vec<Vec<String>> {
    raw.lines().map(|line| line.split('\t').map(str::to_string).collect()).collect()
}

/// A place the user has granted, and never over an existing file.
fn export_target(args: &Value, allowed: &[&str]) -> Result<(String, String), String> {
    let format = text(args, "format").to_ascii_lowercase();
    if !allowed.contains(&format.as_str()) {
        return Err(format!("format must be one of {}", allowed.join(", ")));
    }
    let path = match crate::grants::resolve_writable(text(args, "path")) {
        Ok(path) => path,
        Err(err) => return Err(err.to_json().get("error").and_then(Value::as_str).unwrap_or("that location can't be used").to_string()),
    };
    if path.exists() {
        return Err(format!("{} already exists, and this won't overwrite it. Choose another name.", path.display()));
    }
    Ok((path.to_string_lossy().to_string(), format))
}

fn macos_app_id(app: &str) -> Option<&'static str> {
    match app {
        "numbers" => Some("com.apple.Numbers"),
        "pages" => Some("com.apple.Pages"),
        "keynote" => Some("com.apple.Keynote"),
        "excel" => Some("com.microsoft.Excel"),
        "word" => Some("com.microsoft.Word"),
        _ => None,
    }
}

fn macos(app: &str, action: &str, args: &Value) -> Result<Value, String> {
    let id = macos_app_id(app).ok_or_else(|| format!("{app} can't be scripted on this Mac"))?;
    let document = text(args, "document");
    if !matches!(action, "list" | "new") && document.is_empty() {
        return Err("document is required".into());
    }
    let done = || json!({ "ok": true, "document": document });
    match (app, action) {
        (_, "list") => {
            let names: Vec<String> = run_script(LIST_SCRIPT, &[id])?.lines().map(str::to_string).collect();
            Ok(json!({ "ok": true, "documents": names }))
        }
        ("numbers" | "pages" | "keynote", "new") => Ok(json!({ "ok": true, "document": run_script(NEW_IWORK, &[id])?.trim() })),
        ("excel", "new") => Ok(json!({ "ok": true, "document": run_script(EXCEL_MAC_NEW, &[])?.trim() })),
        ("word", "new") => Ok(json!({ "ok": true, "document": run_script(WORD_MAC_NEW, &[])?.trim() })),
        ("numbers", "set_cells") => with_document(NUMBERS_SET, document, &cell_args(args)?).map(|_| done()),
        ("excel", "set_cells") => with_document(EXCEL_MAC_SET, document, &cell_args(args)?).map(|_| done()),
        ("numbers", "read") => Ok(json!({ "ok": true, "rows": rows_of(&run_script(NUMBERS_READ, &[document])?) })),
        ("excel", "read") => Ok(json!({ "ok": true, "rows": rows_of(&run_script(EXCEL_MAC_READ, &[document])?) })),
        ("pages", "set_text") => run_script(PAGES_SET, &[document, text(args, "text")]).map(|_| done()),
        ("word", "set_text") => run_script(WORD_MAC_SET, &[document, text(args, "text")]).map(|_| done()),
        ("pages", "read") => Ok(json!({ "ok": true, "text": run_script(PAGES_READ, &[document])? })),
        ("word", "read") => Ok(json!({ "ok": true, "text": run_script(WORD_MAC_READ, &[document])? })),
        ("keynote", "add_slide") => {
            let count = run_script(KEYNOTE_ADD, &[document, text(args, "title"), text(args, "text"), text(args, "notes")])?;
            Ok(json!({ "ok": true, "document": document, "slides": count.trim() }))
        }
        ("keynote", "read") => Ok(json!({ "ok": true, "slides": rows_of(&run_script(KEYNOTE_READ, &[document])?) })),
        ("numbers", "export") => {
            let (path, format) = export_target(args, &["csv", "xlsx", "pdf"])?;
            run_script(NUMBERS_EXPORT, &[document, &path, &format])?;
            Ok(json!({ "ok": true, "path": path }))
        }
        ("pages", "export") => {
            let (path, format) = export_target(args, &["docx", "pdf"])?;
            run_script(PAGES_EXPORT, &[document, &path, &format])?;
            Ok(json!({ "ok": true, "path": path }))
        }
        ("keynote", "export") => {
            let (path, format) = export_target(args, &["pptx", "pdf"])?;
            run_script(KEYNOTE_EXPORT, &[document, &path, &format])?;
            Ok(json!({ "ok": true, "path": path }))
        }
        ("excel", "export") => {
            let (path, _) = export_target(args, &["xlsx"])?;
            run_script(EXCEL_MAC_SAVE, &[document, &path])?;
            Ok(json!({ "ok": true, "path": path }))
        }
        ("word", "export") => {
            let (path, _) = export_target(args, &["docx"])?;
            run_script(WORD_MAC_SAVE, &[document, &path])?;
            Ok(json!({ "ok": true, "path": path }))
        }
        _ => Err(format!("{app} can't {action} here")),
    }
}

fn windows(app: &str, action: &str, args: &Value) -> Result<Value, String> {
    let document = text(args, "document");
    let export = match (app, action) {
        ("excel", "export") => Some(export_target(args, &["xlsx"])?),
        ("word", "export") => Some(export_target(args, &["docx"])?),
        ("powerpoint", "export") => Some(export_target(args, &["pptx"])?),
        _ => None,
    };
    let input = json!({
        "document": document,
        "text": text(args, "text"),
        "title": text(args, "title"),
        "notes": text(args, "notes"),
        "cells": args.get("cells").cloned().unwrap_or(json!([])),
        "path": export.as_ref().map(|(path, _)| path.clone()).unwrap_or_default(),
    })
    .to_string();
    let body = match (app, action) {
        ("excel", "new") => format!("{WIN_EXCEL} $x.Workbooks.Add().Name"),
        ("excel", "list") => format!("{WIN_EXCEL} $x.Workbooks | ForEach-Object {{ $_.Name }}"),
        ("excel", "set_cells") => format!("{WIN_EXCEL} $s = $x.Workbooks.Item($a.document).ActiveSheet; foreach ($c in $a.cells) {{ if (\"$($c.value)\".StartsWith('=')) {{ $s.Range($c.cell).Formula = $c.value }} else {{ $s.Range($c.cell).Value2 = $c.value }} }}; 'ok'"),
        ("excel", "read") => format!("{WIN_EXCEL} $u = $x.Workbooks.Item($a.document).ActiveSheet.UsedRange; for ($r = 1; $r -le $u.Rows.Count; $r++) {{ (1..$u.Columns.Count | ForEach-Object {{ ([string]$u.Cells.Item($r, $_).Text) -replace '[\\t\\r\\n]', ' ' }}) -join \"`t\" }}"),
        ("excel", "export") => format!("{WIN_EXCEL} $x.Workbooks.Item($a.document).SaveAs($a.path, 51); 'ok'"),
        ("word", "new") => format!("{WIN_WORD} $x.Documents.Add().Name"),
        ("word", "list") => format!("{WIN_WORD} $x.Documents | ForEach-Object {{ $_.Name }}"),
        ("word", "set_text") => format!("{WIN_WORD} $x.Documents.Item($a.document).Content.Text = $a.text; 'ok'"),
        ("word", "read") => format!("{WIN_WORD} $x.Documents.Item($a.document).Content.Text"),
        ("word", "export") => format!("{WIN_WORD} $x.Documents.Item($a.document).SaveAs2($a.path, 16); 'ok'"),
        ("powerpoint", "new") => format!("{WIN_POWERPOINT} $x.Presentations.Add().Name"),
        ("powerpoint", "list") => format!("{WIN_POWERPOINT} $x.Presentations | ForEach-Object {{ $_.Name }}"),
        ("powerpoint", "add_slide") => format!("{WIN_POWERPOINT} $p = $x.Presentations.Item($a.document); $s = $p.Slides.Add($p.Slides.Count + 1, 2); $s.Shapes.Item(1).TextFrame.TextRange.Text = $a.title; $s.Shapes.Item(2).TextFrame.TextRange.Text = $a.text; if ($a.notes) {{ $s.NotesPage.Shapes.Placeholders.Item(2).TextFrame.TextRange.Text = $a.notes }}; $p.Slides.Count"),
        ("powerpoint", "export") => format!("{WIN_POWERPOINT} $x.Presentations.Item($a.document).SaveAs($a.path, 24); 'ok'"),
        _ => return Err(format!("{app} can't {action} on Windows")),
    };
    let raw = powershell(&format!("{WIN_ARGS} {body}"), Some(&input))?;
    let raw = raw.trim();
    Ok(match action {
        "list" => json!({ "ok": true, "documents": raw.lines().collect::<Vec<_>>() }),
        "new" => json!({ "ok": true, "document": raw }),
        "read" if app == "excel" => json!({ "ok": true, "rows": rows_of(raw) }),
        "read" => json!({ "ok": true, "text": raw }),
        "export" => json!({ "ok": true, "path": export.map(|(path, _)| path).unwrap_or_default() }),
        _ => json!({ "ok": true, "document": document, "result": raw }),
    })
}

pub fn run(args: &Value) -> Value {
    let app = text(args, "app").to_ascii_lowercase();
    let action = text(args, "action");
    let out = if cfg!(target_os = "macos") {
        macos(&app, action, args)
    } else if cfg!(target_os = "windows") {
        windows(&app, action, args)
    } else {
        Err("Office apps can be scripted on macOS and Windows".to_string())
    };
    let mut result = out.unwrap_or_else(|err| json!({ "ok": false, "error": err }));
    result["ran_on"] = json!("local_computer");
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cell_references_are_checked() {
        for good in ["A1", "AB12", "ZZZ9"] {
            assert!(valid_cell(good), "{good}");
        }
        for bad in ["", "1", "A", "A1; delete", "AAAA1", "A-1"] {
            assert!(!valid_cell(bad), "{bad}");
        }
    }

    #[test]
    fn values_keep_their_type() {
        assert_eq!(typed_value(&json!(40)), "n:40");
        assert_eq!(typed_value(&json!("Tea")), "s:Tea");
        assert_eq!(typed_value(&json!("=SUM(B2:B4)")), "s:=SUM(B2:B4)");
    }

    #[test]
    fn rows_split_on_tabs() {
        assert_eq!(rows_of("a\tb\nc\td"), vec![vec!["a", "b"], vec!["c", "d"]]);
    }
}

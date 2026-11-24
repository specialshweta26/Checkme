//! Shell and Python run directly by default: the user chose not to be asked. With asking turned on
//! (`LUMIO_ASK_BEFORE_RUN=1`, or an `ask-before-running` file in Lumio's data folder) they run only after the user says
//! yes in a native dialog on this computer.
//! The dialog is native, not part of the web page, so the page (or anything injected into it) cannot answer it.
//! "Allow until Lumio quits" (macOS) is kept in memory only; quitting the app asks again.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::sync::Mutex;

/// How long the dialog waits. The server adds this to its own wait for `exec` and `run_code` (`shell-approval` feature).
pub const WAIT: Duration = Duration::from_secs(120);
const SHOWN_LIMIT: usize = 1500;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Decision {
    Once,
    Session,
    Deny,
    NoAnswer,
}

static SESSION: AtomicBool = AtomicBool::new(false);
static APP: OnceLock<tauri::AppHandle> = OnceLock::new();

/// One dialog at a time, so two commands never stack modal windows.
fn one_at_a_time() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

pub fn set_app(app: tauri::AppHandle) {
    let _ = APP.set(app);
}

/// What the user sees. `kind` is "command" or "Python code".
pub fn message(kind: &str, body: &str, dir: &str) -> String {
    let mut shown: String = body.trim().chars().take(SHOWN_LIMIT).collect();
    if body.trim().chars().count() > SHOWN_LIMIT {
        shown.push_str("\n…");
    }
    format!(
        "An agent wants to run this {kind} on this computer:\n\n{shown}\n\nFolder: {dir}\n\nIt runs with your account's access. If you don't answer in {} seconds, it is skipped.",
        WAIT.as_secs()
    )
}

#[cfg(test)]
pub static TEST_ASKING: AtomicBool = AtomicBool::new(true);

/// Whether shell and Python ask first. Off unless the user turned it on.
pub fn asking() -> bool {
    #[cfg(test)]
    {
        return TEST_ASKING.load(Ordering::SeqCst);
    }
    #[allow(unreachable_code)]
    {
        std::env::var("LUMIO_ASK_BEFORE_RUN").map(|v| v.trim() == "1").unwrap_or(false)
            || crate::grants::data_dir().join("ask-before-running").exists()
    }
}

/// Ok(()) to run. Err is the tool result to send back instead.
pub async fn approve(kind: &str, body: &str, dir: &str) -> Result<(), Value> {
    if !asking() {
        return Ok(());
    }
    if SESSION.load(Ordering::SeqCst) {
        return Ok(());
    }
    let _turn = one_at_a_time().lock().await;
    if SESSION.load(Ordering::SeqCst) {
        return Ok(());
    }
    let text = message(kind, body, dir);
    let decision = match tokio::time::timeout(WAIT, ask(text)).await {
        Ok(d) => d,
        Err(_) => Decision::NoAnswer,
    };
    outcome(decision)
}

pub fn outcome(decision: Decision) -> Result<(), Value> {
    match decision {
        Decision::Once => Ok(()),
        Decision::Session => {
            SESSION.store(true, Ordering::SeqCst);
            Ok(())
        }
        Decision::Deny => Err(json!({
            "ok": false,
            "need": "shell_approval",
            "denied": true,
            "error": "The user chose not to run this on their computer. Do not retry it, and do not do the same thing another way. Ask what they want instead.",
            "ran_on": "local_computer",
        })),
        Decision::NoAnswer => Err(json!({
            "ok": false,
            "need": "shell_approval",
            "denied": true,
            "error": "The user did not answer the permission prompt on their computer, so nothing ran. Say so and ask whether to try again.",
            "ran_on": "local_computer",
        })),
    }
}

#[cfg(test)]
pub fn reset_session() {
    SESSION.store(false, Ordering::SeqCst);
}

#[cfg(test)]
pub static TEST_ANSWER: std::sync::Mutex<Option<Decision>> = std::sync::Mutex::new(None);

#[cfg(test)]
async fn ask(_text: String) -> Decision {
    TEST_ANSWER.lock().unwrap().unwrap_or(Decision::Deny)
}

#[cfg(not(test))]
async fn ask(text: String) -> Decision {
    if let Some(app) = APP.get() {
        use tauri::Manager;
        if let Some(win) = app.get_webview_window("main") {
            let _ = win.unminimize();
            let _ = win.set_focus();
        }
    }
    decide(&show(text).await)
}

const RUN: &str = "Run";
const UNTIL_QUIT: &str = "Allow until Lumio quits";
const DENY: &str = "Don't run";

/// Only a clear yes runs anything; closing the dialog or any other answer is a no.
fn decide(got: &rfd::MessageDialogResult) -> Decision {
    use rfd::MessageDialogResult;
    match got {
        MessageDialogResult::Custom(label) if label == RUN => Decision::Once,
        MessageDialogResult::Custom(label) if label == UNTIL_QUIT => Decision::Session,
        MessageDialogResult::Yes => Decision::Once,
        _ => Decision::Deny,
    }
}

#[cfg(all(not(test), target_os = "macos"))]
async fn show(text: String) -> rfd::MessageDialogResult {
    use rfd::{AsyncMessageDialog, MessageButtons, MessageLevel};
    AsyncMessageDialog::new()
        .set_level(MessageLevel::Warning)
        .set_title("Allow this on your computer?")
        .set_description(text)
        .set_buttons(MessageButtons::YesNoCancelCustom(RUN.into(), DENY.into(), UNTIL_QUIT.into()))
        .show()
        .await
}

/// Windows needs common-controls-v6 for custom labels, so it gets plain Yes / No (one command at a time).
#[cfg(all(not(test), not(target_os = "macos")))]
async fn show(text: String) -> rfd::MessageDialogResult {
    use rfd::{AsyncMessageDialog, MessageButtons, MessageLevel};
    AsyncMessageDialog::new()
        .set_level(MessageLevel::Warning)
        .set_title("Allow this on your computer?")
        .set_description(format!("{text}\n\nYes runs it. No skips it."))
        .set_buttons(MessageButtons::YesNo)
        .show()
        .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn message_shows_the_command_and_folder() {
        let text = message("command", "ls -la", "/Users/me/Desktop");
        assert!(text.contains("ls -la"));
        assert!(text.contains("/Users/me/Desktop"));
    }

    #[test]
    fn long_commands_are_cut_in_the_dialog() {
        let text = message("command", &"x".repeat(5000), "/d");
        assert!(text.len() < 2200);
        assert!(text.contains('…'));
    }

    #[test]
    fn only_a_clear_yes_runs() {
        use rfd::MessageDialogResult as R;
        assert_eq!(decide(&R::Custom(RUN.into())), Decision::Once);
        assert_eq!(decide(&R::Yes), Decision::Once);
        assert_eq!(decide(&R::Custom(UNTIL_QUIT.into())), Decision::Session);
        for no in [R::Custom(DENY.into()), R::No, R::Cancel, R::Ok, R::Custom("other".into())] {
            assert_eq!(decide(&no), Decision::Deny);
        }
    }

    #[test]
    fn deny_and_no_answer_say_nothing_ran() {
        for d in [Decision::Deny, Decision::NoAnswer] {
            let out = outcome(d).unwrap_err();
            assert_eq!(out["need"], "shell_approval");
            assert_eq!(out["denied"], true);
            assert_eq!(out["ok"], false);
        }
    }
}

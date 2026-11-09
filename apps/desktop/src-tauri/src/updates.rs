//! Offers new Lumio releases from inside the app. The site in the window can't reach any of this: the check, download
//! and install all run here, and the updater refuses anything not signed with the key whose public half ships in
//! tauri.conf.json.

use std::time::Duration;

use rfd::{AsyncMessageDialog, MessageButtons, MessageDialogResult, MessageLevel};
use tauri::AppHandle;
use tauri_plugin_updater::UpdaterExt;

const FIRST_CHECK_AFTER: Duration = Duration::from_secs(20);
const CHECK_EVERY: Duration = Duration::from_secs(60 * 60);
const CHECK_AGAIN_AFTER_FAILURE: Duration = Duration::from_secs(5 * 60);
const INSTALL_TRIES: u32 = 3;
const UPDATE_NOW: &str = "Update now";
const LATER: &str = "Later";

fn prompt_text(version: &str) -> String {
    format!("Lumio {version} is available. Updating restarts the app, which stops anything it's doing on this computer.")
}

/// "Later" holds for that version until the app restarts, so it doesn't ask again every few hours.
fn should_offer(version: &str, declined: Option<&str>) -> bool {
    declined != Some(version)
}

fn accepted(result: &MessageDialogResult) -> bool {
    match result {
        MessageDialogResult::Ok | MessageDialogResult::Yes => true,
        MessageDialogResult::Custom(label) => label == UPDATE_NOW,
        _ => false,
    }
}

async fn tell_failed() {
    AsyncMessageDialog::new()
        .set_level(MessageLevel::Warning)
        .set_title("Lumio update")
        .set_description("The update couldn't be installed. Lumio will offer it again next time it starts.")
        .show()
        .await;
}

async fn check_once(app: &AppHandle, declined: &mut Option<String>) -> Result<(), String> {
    let updater = app.updater().map_err(|e| e.to_string())?;
    let Some(update) = updater.check().await.map_err(|e| e.to_string())? else {
        return Ok(());
    };
    if !should_offer(&update.version, declined.as_deref()) {
        return Ok(());
    }
    let answer = AsyncMessageDialog::new()
        .set_title("Update Lumio")
        .set_description(prompt_text(&update.version))
        .set_buttons(MessageButtons::OkCancelCustom(UPDATE_NOW.into(), LATER.into()))
        .show()
        .await;
    if !accepted(&answer) {
        *declined = Some(update.version.clone());
        return Ok(());
    }
    // Downloads fail now and then for a moment, so a single failure shouldn't send the user to wait for the next start.
    let mut tries = 0;
    loop {
        tries += 1;
        match update.download_and_install(|_, _| {}, || {}).await {
            Ok(()) => break,
            Err(err) if tries >= INSTALL_TRIES => {
                tell_failed().await;
                return Err(err.to_string());
            }
            Err(err) => {
                eprintln!("update try {tries} failed: {err}");
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
        }
    }
    app.restart();
}

/// Release builds only. A failed check is logged and retried later, so being offline never bothers the user.
pub fn start(app: AppHandle) {
    if cfg!(debug_assertions) {
        return;
    }
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(FIRST_CHECK_AFTER).await;
        let mut declined = None;
        loop {
            let wait = match check_once(&app, &mut declined).await {
                Ok(()) => CHECK_EVERY,
                Err(err) => {
                    eprintln!("update check failed: {err}");
                    CHECK_AGAIN_AFTER_FAILURE
                }
            };
            tokio::time::sleep(wait).await;
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn later_holds_for_that_version_only() {
        assert!(should_offer("0.1.12", None));
        assert!(!should_offer("0.1.12", Some("0.1.12")));
        assert!(should_offer("0.1.13", Some("0.1.12")));
    }

    #[test]
    fn only_a_clear_yes_installs() {
        assert!(accepted(&MessageDialogResult::Custom(UPDATE_NOW.into())));
        assert!(accepted(&MessageDialogResult::Ok));
        assert!(!accepted(&MessageDialogResult::Custom(LATER.into())));
        assert!(!accepted(&MessageDialogResult::Cancel));
    }

    #[test]
    fn the_prompt_warns_about_the_restart() {
        let text = prompt_text("0.1.12");
        assert!(text.contains("0.1.12") && text.contains("restarts"));
    }

    #[test]
    fn config_uses_our_feed_then_github_and_ships_update_files() {
        let conf: serde_json::Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let updater = &conf["plugins"]["updater"];
        assert_eq!(
            updater["endpoints"],
            serde_json::json!([
                "https://checkme-web-kw68.onrender.com/api/desktop/update/latest.json",
                "https://github.com/specialshweta26/Checkme/releases/latest/download/latest.json"
            ])
        );
        let key = updater["pubkey"].as_str().unwrap();
        let decoded = base64::Engine::decode(&base64::engine::general_purpose::STANDARD, key).expect("a real public key");
        assert!(String::from_utf8_lossy(&decoded).contains("minisign public key"));
        assert_eq!(conf["bundle"]["createUpdaterArtifacts"], true);
        assert!(conf["bundle"]["targets"].as_array().unwrap().iter().any(|t| t == "app"));
    }

    #[test]
    fn the_site_in_the_window_gets_no_updater_permission() {
        let caps = include_str!("../capabilities/default.json");
        assert!(!caps.contains("updater") && !caps.contains("process"));
    }
}

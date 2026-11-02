//! User-selected folder grants. The sidecar enforces these on every file operation.
//! Canonicalize + prefix check, so `..`, symlinks, and Windows reparse points cannot escape.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

const MAX_READ: u64 = 8 * 1024 * 1024;
const SEARCH_CAP: usize = 400;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FolderGrant {
    pub id: String,
    pub path: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, Default)]
pub struct Grants {
    #[serde(default)]
    pub folders: Vec<FolderGrant>,
    #[serde(default)]
    pub terminal: bool,
}

#[derive(Debug)]
pub enum AccessError {
    NeedGrant { path: String },
    Io(String),
}

impl AccessError {
    pub fn to_json(&self) -> Value {
        match self {
            AccessError::NeedGrant { path } => json!({
                "ok": false,
                "need": "folder_grant",
                "path": path,
                "error": format!("Ask the user in one sentence whether Lumio may use this folder, then wait for their answer. Do not open a dialog. Path: {path}"),
                "ran_on": "local_computer",
            }),
            AccessError::Io(err) => json!({
                "ok": false,
                "error": err,
                "ran_on": "local_computer",
            }),
        }
    }
}

fn store() -> &'static Mutex<Grants> {
    static STORE: OnceLock<Mutex<Grants>> = OnceLock::new();
    STORE.get_or_init(|| Mutex::new(load()))
}

pub fn grants_path() -> PathBuf {
    if let Ok(raw) = std::env::var("LUMIO_GRANTS_PATH") {
        if !raw.trim().is_empty() {
            return PathBuf::from(raw);
        }
    }
    data_dir().join("grants.json")
}

pub fn data_dir() -> PathBuf {
    let base = dirs::data_dir().or_else(dirs::home_dir).unwrap_or_else(|| PathBuf::from("."));
    let dir = base.join("Lumio");
    let _ = fs::create_dir_all(&dir);
    dir
}

pub fn browser_profile_dir() -> PathBuf {
    let dir = data_dir().join("browser-profile");
    let _ = fs::create_dir_all(&dir);
    dir
}

fn strip_unc(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    PathBuf::from(text.strip_prefix(r"\\?\").unwrap_or(&text))
}

fn canonical(path: &Path) -> Result<PathBuf, String> {
    Ok(strip_unc(&path.canonicalize().map_err(|e| e.to_string())?))
}

fn load() -> Grants {
    let path = grants_path();
    let Ok(bytes) = fs::read(&path) else {
        return Grants::default();
    };
    serde_json::from_slice(&bytes).unwrap_or_default()
}

fn persist(grants: &Grants) -> Result<(), String> {
    let path = grants_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let body = serde_json::to_vec_pretty(grants).map_err(|e| e.to_string())?;
    fs::write(path, body).map_err(|e| e.to_string())
}

fn with_grants<T>(f: impl FnOnce(&mut Grants) -> T) -> T {
    let mut g = store().lock().unwrap_or_else(|e| e.into_inner());
    f(&mut g)
}

pub fn snapshot() -> Grants {
    with_grants(|g| g.clone())
}

pub fn terminal_allowed() -> bool {
    with_grants(|g| g.terminal)
}

pub fn set_terminal(enabled: bool) -> Result<Grants, String> {
    with_grants(|g| {
        g.terminal = enabled;
        persist(g)?;
        Ok(g.clone())
    })
}

fn new_id() -> String {
    let t = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis();
    format!("g{t:x}")
}

pub fn ensure_common_folders() {
    let Some(home) = dirs::home_dir() else { return };
    for name in ["Desktop", "Documents", "Downloads"] {
        let _ = add_folder_loose(&home.join(name));
    }
}

fn add_folder_loose(path: &Path) -> Result<FolderGrant, String> {
    match canonical(path) {
        Ok(resolved) if resolved.is_dir() => add_folder(&resolved),
        Ok(_) => Err("not a folder".into()),
        Err(err) if crate::os::is_denied_message(&err) => store_grant_path(path),
        Err(err) => Err(err),
    }
}

fn store_grant_path(path: &Path) -> Result<FolderGrant, String> {
    let text = path.to_string_lossy().into_owned();
    with_grants(|g| {
        if let Some(existing) = g.folders.iter().find(|f| f.path == text) {
            return Ok(existing.clone());
        }
        let grant = FolderGrant { id: new_id(), path: text };
        g.folders.push(grant.clone());
        persist(g)?;
        Ok(grant)
    })
}

fn expand_user_path(raw: &str) -> PathBuf {
    let t = raw.trim();
    let Some(home) = dirs::home_dir() else {
        return PathBuf::from(t);
    };
    if t == "~" {
        return home;
    }
    if let Some(rest) = t.strip_prefix("~/").or_else(|| t.strip_prefix("~\\")) {
        return home.join(rest);
    }
    let path = PathBuf::from(t);
    if path.is_absolute() {
        return path;
    }
    let under_home = home.join(&path);
    if under_home.exists() {
        return under_home;
    }
    for grant in snapshot().folders {
        let candidate = PathBuf::from(&grant.path).join(&path);
        if candidate.exists() {
            return candidate;
        }
    }
    under_home
}

pub fn add_folder(path: &Path) -> Result<FolderGrant, String> {
    let resolved = canonical(path)?;
    if !resolved.is_dir() {
        return Err("pick a folder".into());
    }
    with_grants(|g| {
        if let Some(existing) = g.folders.iter().find(|f| Path::new(&f.path) == resolved.as_path()) {
            return Ok(existing.clone());
        }
        let grant = FolderGrant {
            id: new_id(),
            path: resolved.to_string_lossy().into_owned(),
        };
        g.folders.push(grant.clone());
        persist(g)?;
        Ok(grant)
    })
}

pub fn revoke(id: &str) -> Result<Grants, String> {
    with_grants(|g| {
        let before = g.folders.len();
        g.folders.retain(|f| f.id != id);
        if g.folders.len() == before {
            return Err("unknown folder grant".into());
        }
        persist(g)?;
        Ok(g.clone())
    })
}

pub fn pick_folder(start: Option<&Path>) -> Result<FolderGrant, String> {
    let mut dialog = rfd::FileDialog::new().set_title("Allow Lumio to use this folder");
    if let Some(start) = start {
        if start.exists() {
            dialog = dialog.set_directory(start);
        }
    }
    let chosen = dialog.pick_folder().ok_or_else(|| "The folder picker was cancelled.".to_string())?;
    add_folder_loose(&chosen)
}

fn under_grant(resolved: &Path, grants: &Grants) -> bool {
    grants.folders.iter().any(|g| {
        let root = Path::new(&g.path);
        resolved == root || resolved.starts_with(root)
    })
}

/// Resolve a user-supplied path that must already exist, inside a granted folder.
pub fn resolve_existing(raw: &str) -> Result<PathBuf, AccessError> {
    let grants = snapshot();
    if raw.trim().is_empty() {
        return Err(AccessError::NeedGrant {
            path: grants.folders.first().map(|f| f.path.clone()).unwrap_or_else(|| "(no folders granted)".into()),
        });
    }
    let given = expand_user_path(raw);
    if grants.folders.is_empty() && !home_protected(&given) {
        return Err(AccessError::NeedGrant {
            path: given.to_string_lossy().into_owned(),
        });
    }
    match canonical(&given) {
        Ok(resolved) => {
            if under_grant(&resolved, &grants) {
                Ok(resolved)
            } else {
                Err(AccessError::NeedGrant {
                    path: resolved.to_string_lossy().into_owned(),
                })
            }
        }
        Err(err) if crate::os::is_denied_message(&err) && (under_grant_text(&given, &grants) || home_protected(&given)) => Ok(given),
        Err(err) => Err(AccessError::Io(err)),
    }
}

fn under_grant_text(path: &Path, grants: &Grants) -> bool {
    let text = path.to_string_lossy();
    grants.folders.iter().any(|g| text == g.path.as_str() || text.starts_with(&(g.path.clone() + "/")) || text.starts_with(&(g.path.clone() + "\\")))
}

pub fn is_common_home_folder(raw: &str) -> bool {
    home_protected(&expand_user_path(raw))
}

fn home_protected(path: &Path) -> bool {
    let Some(home) = dirs::home_dir() else { return false };
    ["Desktop", "Documents", "Downloads"].iter().any(|name| {
        let root = home.join(name);
        path == root || path.starts_with(&root)
    })
}

/// Resolve a path for create/write. Parent must exist and be granted; file may be new.
pub fn resolve_writable(raw: &str) -> Result<PathBuf, AccessError> {
    let grants = snapshot();
    if grants.folders.is_empty() {
        return Err(AccessError::NeedGrant {
            path: raw.trim().to_string(),
        });
    }
    let given = expand_user_path(raw);
    if given.as_os_str().is_empty() {
        return Err(AccessError::Io("path is required".into()));
    }
    if given.exists() {
        return resolve_existing(raw);
    }
    let parent = given.parent().filter(|p| !p.as_os_str().is_empty()).ok_or_else(|| AccessError::Io("path is required".into()))?;
    let parent_c = canonical(parent).map_err(AccessError::Io)?;
    if !under_grant(&parent_c, &grants) {
        return Err(AccessError::NeedGrant {
            path: parent_c.to_string_lossy().into_owned(),
        });
    }
    Ok(parent_c.join(given.file_name().unwrap_or_default()))
}

pub fn list_roots() -> Value {
    let grants = snapshot();
    json!({
        "ok": true,
        "folders": grants.folders,
        "terminal": grants.terminal,
        "ran_on": "local_computer",
        "note": "Desktop, Documents, and Downloads are already allowed. List and read them directly. Ask in the chat before using any other folder.",
    })
}

pub fn max_read_bytes() -> u64 {
    MAX_READ
}

pub fn search(query: &str, under: Option<&str>) -> Result<Value, AccessError> {
    let grants = snapshot();
    if grants.folders.is_empty() {
        return Err(AccessError::NeedGrant {
            path: under.unwrap_or("").to_string(),
        });
    }
    let roots: Vec<PathBuf> = if let Some(raw) = under.filter(|s| !s.trim().is_empty()) {
        vec![resolve_existing(raw)?]
    } else {
        grants.folders.iter().map(|f| PathBuf::from(&f.path)).collect()
    };
    let q = query.trim().to_ascii_lowercase();
    let mut hits = Vec::new();
    for root in roots {
        walk_search(&root, &q, &mut hits);
        if hits.len() >= SEARCH_CAP {
            break;
        }
    }
    Ok(json!({
        "ok": true,
        "query": query,
        "hits": hits,
        "truncated": hits.len() >= SEARCH_CAP,
        "ran_on": "local_computer",
    }))
}

fn walk_search(dir: &Path, q: &str, hits: &mut Vec<Value>) {
    if hits.len() >= SEARCH_CAP {
        return;
    }
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(err) if err.kind() == std::io::ErrorKind::PermissionDenied || err.raw_os_error() == Some(1) => {
            if let Ok(rows) = crate::os::finder_entries(dir) {
                for (name, is_dir) in rows {
                    if hits.len() >= SEARCH_CAP {
                        return;
                    }
                    if name.starts_with('.') || name == "node_modules" || name == "browser-profile" {
                        continue;
                    }
                    let path = dir.join(&name);
                    if q.is_empty() || name.to_ascii_lowercase().contains(q) || path.to_string_lossy().to_ascii_lowercase().contains(q) {
                        hits.push(json!({
                            "path": path.to_string_lossy(),
                            "name": name,
                            "dir": is_dir,
                        }));
                    }
                }
            }
            return;
        }
        Err(_) => return,
    };
    let mut rows: Vec<_> = entries.filter_map(|e| e.ok()).collect();
    rows.sort_by_key(|e| e.file_name());
    for entry in rows {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with('.') || name == "node_modules" || name == "browser-profile" {
            continue;
        }
        let is_dir = path.is_dir();
        if q.is_empty() || name.to_ascii_lowercase().contains(q) || path.to_string_lossy().to_ascii_lowercase().contains(q) {
            let meta = entry.metadata().ok();
            hits.push(json!({
                "path": path.to_string_lossy(),
                "name": name,
                "dir": is_dir,
                "bytes": meta.as_ref().map(|m| m.len()),
                "modified": meta.and_then(|m| m.modified().ok()).and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_secs()),
            }));
            if hits.len() >= SEARCH_CAP {
                return;
            }
        }
        if is_dir {
            walk_search(&path, q, hits);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::Mutex as StdMutex;

    static SEQ: AtomicU64 = AtomicU64::new(0);
    static TEST_LOCK: StdMutex<()> = StdMutex::new(());

    fn isolate() -> PathBuf {
        let n = SEQ.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("lumio-grants-{n}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        std::env::set_var("LUMIO_GRANTS_PATH", dir.join("grants.json"));
        *store().lock().unwrap() = Grants::default();
        dir
    }

    fn lock_tests() -> std::sync::MutexGuard<'static, ()> {
        TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }

    #[test]
    fn grant_allows_nested_file() {
        let _lock = lock_tests();
        let root = isolate();
        let folder = root.join("work");
        fs::create_dir_all(folder.join("q")).unwrap();
        fs::write(folder.join("q/a.txt"), b"hi").unwrap();
        add_folder(&folder).unwrap();
        let got = resolve_existing(folder.join("q/a.txt").to_str().unwrap()).unwrap();
        assert!(got.ends_with("a.txt"));
    }

    #[test]
    fn traversal_cannot_leave_grant() {
        let _lock = lock_tests();
        let root = isolate();
        let folder = root.join("work");
        fs::create_dir_all(&folder).unwrap();
        fs::write(root.join("secret.txt"), b"no").unwrap();
        add_folder(&folder).unwrap();
        let escape = folder.join("..").join("secret.txt");
        match resolve_existing(escape.to_str().unwrap()) {
            Err(AccessError::NeedGrant { .. }) => {}
            other => panic!("expected deny, got {other:?}"),
        }
    }

    #[test]
    fn symlink_escape_is_denied() {
        let _lock = lock_tests();
        let root = isolate();
        let folder = root.join("work");
        fs::create_dir_all(&folder).unwrap();
        let secret = root.join("secret.txt");
        fs::write(&secret, b"no").unwrap();
        let link = folder.join("leak");
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&secret, &link).unwrap();
            add_folder(&folder).unwrap();
            match resolve_existing(link.to_str().unwrap()) {
                Err(AccessError::NeedGrant { .. }) => {}
                Ok(p) => panic!("symlink escaped to {}", p.display()),
                Err(other) => panic!("unexpected {other:?}"),
            }
        }
        #[cfg(windows)]
        {
            add_folder(&folder).unwrap();
            let _ = std::os::windows::fs::symlink_file(&secret, &link);
            if link.exists() {
                match resolve_existing(link.to_str().unwrap()) {
                    Err(AccessError::NeedGrant { .. }) => {}
                    Ok(p) => panic!("reparse escaped to {}", p.display()),
                    Err(_) => {}
                }
            }
        }
    }

    #[test]
    fn ungranted_path_asks_for_grant() {
        let _lock = lock_tests();
        isolate();
        match resolve_existing("/tmp/not-granted-lumio") {
            Err(AccessError::NeedGrant { .. }) => {}
            other => panic!("expected need grant, got {other:?}"),
        }
    }

    #[test]
    fn revoke_stops_access() {
        let _lock = lock_tests();
        let root = isolate();
        let folder = root.join("work");
        fs::create_dir_all(&folder).unwrap();
        fs::write(folder.join("a.txt"), b"x").unwrap();
        let grant = add_folder(&folder).unwrap();
        assert!(resolve_existing(folder.join("a.txt").to_str().unwrap()).is_ok());
        revoke(&grant.id).unwrap();
        match resolve_existing(folder.join("a.txt").to_str().unwrap()) {
            Err(AccessError::NeedGrant { .. }) => {}
            other => panic!("expected need grant after revoke, got {other:?}"),
        }
    }
}

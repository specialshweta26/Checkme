//! Agents' own browsers on this computer (docs/AGENT_BROWSERS.md). An agent set to "its own browser" gets a separate
//! Chrome with its own profile under `<Lumio data>/browser-spaces/<space>`, so sites signed in for one agent or in
//! the shared Lumio browser stay closed to the others. At most `MAX_OPEN` stay open; opening another closes the one
//! used longest ago that isn't working, and its profile stays on disk, so it reopens signed in.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use tokio::sync::Mutex;

use crate::browser::{self, Chrome, Target};

pub const MAX_OPEN: usize = 3;

pub type Slot = Arc<Mutex<Option<Chrome>>>;

struct Open {
    target: Target,
    slot: Slot,
    used: Instant,
}

pub struct AgentBrowsers {
    base: PathBuf,
    open: HashMap<String, Open>,
}

impl Default for AgentBrowsers {
    fn default() -> Self {
        Self::at(browser::spaces_dir())
    }
}

/// What to do before a space can open: close these ports, then delete these profiles.
#[derive(Debug, Default, PartialEq)]
pub struct Plan {
    pub close: Vec<u16>,
    pub evicted: Vec<String>,
}

impl AgentBrowsers {
    pub fn at(base: PathBuf) -> Self {
        AgentBrowsers { base, open: HashMap::new() }
    }

    pub fn open_spaces(&self) -> Vec<String> {
        let mut names: Vec<String> = self.open.keys().cloned().collect();
        names.sort();
        names
    }

    /// Make room for `space`: its earlier profiles (other epochs of the same agent) close, and when `MAX_OPEN` are
    /// open the least recently used idle one closes. A browser running a command (its slot is locked) is never closed.
    fn make_room(&mut self, space: &str) -> Result<Plan, String> {
        let mut plan = Plan::default();
        if let Some((stem, _)) = space.rsplit_once('-') {
            let prefix = format!("{stem}-");
            let old: Vec<String> = self.open.keys().filter(|k| k.as_str() != space && k.starts_with(&prefix)).cloned().collect();
            for name in old {
                if let Some(o) = self.open.remove(&name) {
                    plan.close.push(o.target.port);
                    plan.evicted.push(name);
                }
            }
        }
        if self.open.contains_key(space) || self.open.len() < MAX_OPEN {
            return Ok(plan);
        }
        let mut idle: Vec<(&String, &Open)> = self.open.iter().filter(|(_, o)| o.slot.try_lock().is_ok()).collect();
        idle.sort_by_key(|(_, o)| o.used);
        let Some((name, _)) = idle.first() else {
            return Err(format!("{MAX_OPEN} agent browsers are busy on this computer. Try again in a moment."));
        };
        let name = (*name).clone();
        if let Some(o) = self.open.remove(&name) {
            plan.close.push(o.target.port);
            plan.evicted.push(name);
        }
        Ok(plan)
    }

    /// The browser slot and Chrome target for `space`, opening a place for it if needed. Chrome itself starts on the
    /// first command that opens a page.
    pub async fn get(&mut self, space: &str) -> Result<(Slot, Target), String> {
        if !browser::valid_space(space) {
            return Err("bad browser name".into());
        }
        if let Some(o) = self.open.get_mut(space) {
            o.used = Instant::now();
            return Ok((o.slot.clone(), o.target.clone()));
        }
        let plan = self.make_room(space)?;
        for port in &plan.close {
            browser::close_browser(*port).await;
        }
        // An earlier profile of this agent belongs to before "Sign out of everything": nothing uses it again.
        browser::forget_old_profiles(&self.base, space);
        let taken: Vec<u16> = self.open.values().map(|o| o.target.port).collect();
        let target = browser::space_target_in(&self.base, space, &taken).await?;
        let slot: Slot = Arc::new(Mutex::new(None));
        self.open.insert(space.to_string(), Open { target: target.clone(), slot: slot.clone(), used: Instant::now() });
        Ok((slot, target))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("lumio-spaces-test-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn space_names_are_checked() {
        assert!(browser::valid_space("a0f3c9-2"));
        assert!(browser::valid_space("agent_B"));
        for bad in ["", "../x", "a/b", "a b", "a.b", &"x".repeat(65)] {
            assert!(!browser::valid_space(bad), "{bad}");
        }
    }

    #[tokio::test]
    async fn each_agent_gets_its_own_profile_and_port_and_keeps_them() {
        let dir = base("ports");
        let mut b = AgentBrowsers::at(dir.clone());
        let (_, a) = b.get("aaa-0").await.unwrap();
        let (_, c) = b.get("ccc-0").await.unwrap();
        assert_ne!(a.port, c.port);
        assert_ne!(a.port, browser::SHARED_PORT);
        assert_eq!(a.profile, dir.join("aaa-0"));
        assert!(a.profile.is_dir() && c.profile.is_dir());
        // A restarted app finds the same port for the same agent.
        let mut again = AgentBrowsers::at(dir.clone());
        let (_, a2) = again.get("aaa-0").await.unwrap();
        assert_eq!(a2, a);
        // A new agent doesn't take a port another agent's profile kept.
        let (_, d) = again.get("ddd-0").await.unwrap();
        assert!(d.port != a.port && d.port != c.port);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn at_most_three_open_and_the_oldest_idle_one_closes() {
        let dir = base("cap");
        let mut b = AgentBrowsers::at(dir.clone());
        let (one, _) = b.get("one-0").await.unwrap();
        b.get("two-0").await.unwrap();
        b.get("three-0").await.unwrap();
        b.get("one-0").await.unwrap(); // used again: two is now the oldest
        b.get("four-0").await.unwrap();
        assert_eq!(b.open_spaces(), vec!["four-0", "one-0", "three-0"]);
        assert!(dir.join("two-0").is_dir(), "a closed browser keeps its profile");
        // A busy browser is never closed to make room.
        let _busy = one.lock().await;
        b.get("five-0").await.unwrap();
        assert!(b.open_spaces().contains(&"one-0".to_string()));
        assert_eq!(b.open_spaces().len(), 3);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn all_busy_is_refused() {
        let dir = base("busy");
        let mut b = AgentBrowsers::at(dir.clone());
        let mut held = vec![];
        for name in ["p-0", "q-0", "r-0"] {
            held.push(b.get(name).await.unwrap().0);
        }
        let _locks: Vec<_> = futures_util::future::join_all(held.iter().map(|s| s.lock())).await;
        let Err(err) = b.get("s-0").await else { panic!("a fourth browser opened while three were busy") };
        assert!(err.contains("busy"), "{err}");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[tokio::test]
    async fn sign_out_of_everything_drops_the_old_profile() {
        let dir = base("epoch");
        let mut b = AgentBrowsers::at(dir.clone());
        b.get("aabb-0").await.unwrap();
        b.get("other-0").await.unwrap();
        std::fs::write(dir.join("aabb-0").join("Cookies"), b"old").unwrap();
        b.get("aabb-1").await.unwrap();
        assert!(!dir.join("aabb-0").exists(), "the old profile is deleted");
        assert!(dir.join("aabb-1").is_dir() && dir.join("other-0").is_dir());
        assert_eq!(b.open_spaces(), vec!["aabb-1", "other-0"]);
        assert!(b.get("../etc").await.is_err());
        let _ = std::fs::remove_dir_all(dir);
    }

    // ------------------------------------------------------------- real Chrome

    async fn cookie_site(listener: tokio::net::TcpListener) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        loop {
            let Ok((mut sock, _)) = listener.accept().await else { return };
            tokio::spawn(async move {
                let mut buf = vec![0u8; 8192];
                let n = sock.read(&mut buf).await.unwrap_or(0);
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                let path = req.split_whitespace().nth(1).unwrap_or("/").to_string();
                let cookie = req
                    .lines()
                    .find_map(|l| l.strip_prefix("Cookie: ").or_else(|| l.strip_prefix("cookie: ")))
                    .unwrap_or("nobody")
                    .trim()
                    .to_string();
                let set = path.strip_prefix("/login?who=").map(|who| format!("set-cookie: session={who}; Path=/; Max-Age=3600\r\n")).unwrap_or_default();
                let body = format!("<html><head><title>Cookie site</title></head><body><h1>Signed in as {cookie}</h1><p>This page shows which session cookie the browser sent so the test can tell whose browser it is, with enough words to count as a loaded page.</p></body></html>");
                let reply = format!("HTTP/1.1 200 OK\r\ncontent-type: text/html\r\n{set}content-length: {}\r\nconnection: close\r\n\r\n{body}", body.len());
                let _ = sock.write_all(reply.as_bytes()).await;
            });
        }
    }

    async fn headless(target: &Target, url: &str) -> Chrome {
        let bin = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/google-chrome"]
            .into_iter()
            .map(PathBuf::from)
            .find(|p| p.exists())
            .expect("Chrome is installed");
        tokio::process::Command::new(bin)
            .arg("--headless=new")
            .arg(format!("--remote-debugging-port={}", target.port))
            .arg(format!("--user-data-dir={}", target.profile.display()))
            .arg("--no-first-run")
            .arg(url)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .unwrap();
        for _ in 0..80 {
            if let Ok(chrome) = browser::attach_to(target).await {
                return chrome;
            }
            tokio::time::sleep(std::time::Duration::from_millis(150)).await;
        }
        panic!("Chrome did not start on {}", target.port);
    }

    async fn who(chrome: &Chrome, site: &str) -> String {
        let page = chrome.navigate(&format!("{site}/"), 3000).await.unwrap();
        let text = page.to_string();
        let at = text.find("Signed in as ").expect("page text");
        text[at + 13..].split(|c: char| c == '"' || c == '\\' || c == '<').next().unwrap_or("").trim().to_string()
    }

    /// Two agent browsers and the shared one on real headless Chrome. Run with:
    /// cargo test --lib agent_browsers -- --ignored
    #[test]
    #[ignore = "needs Google Chrome"]
    fn real_chrome_keeps_each_agents_sign_ins_apart() {
        let rt = tokio::runtime::Builder::new_multi_thread().enable_all().build().unwrap();
        rt.block_on(async {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let site = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
            tokio::spawn(cookie_site(listener));
            let dir = base("chrome");
            let shared = Target { port: 9398, profile: dir.join("shared") };
            let mut b = AgentBrowsers::at(dir.join("spaces"));
            let (_, ta) = b.get("aaa-0").await.unwrap();
            let (_, tb) = b.get("bbb-0").await.unwrap();

            let s = headless(&shared, &format!("{site}/login?who=you")).await;
            let a = headless(&ta, &format!("{site}/")).await;
            let c = headless(&tb, &format!("{site}/")).await;
            assert_eq!(who(&a, &site).await, "nobody", "agent A starts signed out");
            a.navigate(&format!("{site}/login?who=agentA"), 2000).await.unwrap();
            c.navigate(&format!("{site}/login?who=agentB"), 2000).await.unwrap();
            assert_eq!(who(&s, &site).await, "session=you");
            assert_eq!(who(&a, &site).await, "session=agentA");
            assert_eq!(who(&c, &site).await, "session=agentB");

            // Closed to make room, A reopens from its profile still signed in.
            browser::close_browser(ta.port).await;
            assert!(browser::attach_to(&ta).await.is_err(), "A's Chrome is closed");
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
            let a2 = headless(&ta, &format!("{site}/")).await;
            assert_eq!(who(&a2, &site).await, "session=agentA");

            for t in [&shared, &ta, &tb] {
                browser::close_browser(t.port).await;
            }
            let _ = std::fs::remove_dir_all(dir);
        });
    }
}

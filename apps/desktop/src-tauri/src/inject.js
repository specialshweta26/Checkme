(function () {
  if (window.__lumioDesktop) return;
  window.__lumioDesktop = true;

  // Do not mirror the Mac/Windows desktop inside Lumio — that capture loop freezes the machine.
  // Teaching records OS clicks in the background; the user works in real apps.
  var css = document.createElement("style");
  css.textContent = [
    ".screen-preview,.screen-stream,.screen-placeholder,.screen-input-bar{display:none!important}",
    ".screen-scrim{display:none!important}",
    ".screen-rail.overlay.fullscreen{inset:auto!important;top:12px!important;left:50%!important;transform:translateX(-50%)!important;width:min(640px,calc(100vw - 24px))!important;height:auto!important;max-height:none!important;border-radius:16px!important;padding:12px 16px!important}",
  ].join("");
  document.documentElement.appendChild(css);

  function withClient(headers) {
    var h = new Headers(headers || {});
    h.set("X-Lumio-Client", "desktop");
    return h;
  }

  var origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    init.headers = withClient(init.headers);
    return origFetch(input, init);
  };

  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function () {
    this.__lumio = true;
    return origOpen.apply(this, arguments);
  };
  var origSet = XMLHttpRequest.prototype.setRequestHeader;
  var origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (body) {
    try {
      origSet.call(this, "X-Lumio-Client", "desktop");
    } catch (e) {}
    return origSend.call(this, body);
  };

  function invoke(name, payload) {
    var core = window.__TAURI__ && window.__TAURI__.core;
    if (!core || !core.invoke) return Promise.reject(new Error("tauri"));
    return core.invoke(name, payload);
  }

  var started = false;
  async function pair() {
    try {
      var me = await origFetch("/api/me", { credentials: "include", headers: withClient() });
      if (!me.ok) {
        started = false;
        return;
      }
      if (started) return;
      var info = { hostname: "Lumio desktop", os: navigator.platform || "" };
      try {
        info = await invoke("machine_info");
      } catch (e) {}
      var res = await origFetch("/api/desktop/pair", {
        method: "POST",
        credentials: "include",
        headers: withClient({ "Content-Type": "application/json" }),
        body: JSON.stringify({ hostname: info.hostname || "", os: info.os || "" }),
      });
      if (!res.ok) return;
      var data = await res.json();
      if (data && data.agent_token) {
        await invoke("start_sidecar", {
          token: data.agent_token,
          apiBase: window.location.origin,
        });
        started = true;
      }
    } catch (e) {}
  }

  async function loop() {
    await pair();
    setTimeout(loop, started ? 15000 : 2500);
  }
  loop();

  function settings() {
    if (document.getElementById("lumio-computer-settings")) return;
    var css2 = document.createElement("style");
    css2.textContent =
      "#lumio-computer-settings{position:fixed;z-index:2147483000;right:16px;bottom:16px;font:13px/1.4 system-ui,sans-serif}" +
      "#lumio-computer-settings button{font:inherit;cursor:pointer}" +
      "#lumio-cs-toggle{border:0;border-radius:999px;padding:8px 12px;background:#111;color:#fff;box-shadow:0 8px 24px rgba(0,0,0,.2)}" +
      "#lumio-cs-panel{display:none;margin-top:8px;width:320px;background:#fff;color:#111;border:1px solid #e5e7eb;border-radius:14px;padding:12px 14px;box-shadow:0 16px 40px rgba(0,0,0,.16)}" +
      "#lumio-cs-panel.open{display:block}" +
      "#lumio-cs-panel h2{margin:0 0 8px;font-size:14px}" +
      "#lumio-cs-panel ul{margin:0;padding:0;list-style:none}" +
      "#lumio-cs-panel li{display:flex;justify-content:space-between;gap:8px;padding:6px 0;border-top:1px solid #f1f5f9;font-size:12px;word-break:break-all}" +
      "#lumio-cs-panel .row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}" +
      "#lumio-cs-panel .row button{border:1px solid #e5e7eb;background:#f8fafc;border-radius:8px;padding:6px 8px}";
    document.documentElement.appendChild(css2);
    var wrap = document.createElement("div");
    wrap.id = "lumio-computer-settings";
    wrap.innerHTML =
      '<button type="button" id="lumio-cs-toggle">This computer</button>' +
      '<div id="lumio-cs-panel"><h2>This computer</h2><p id="lumio-cs-note" style="margin:0 0 8px;color:#64748b">Lumio can only use folders you grant. Terminal is off unless you enable it.</p><ul id="lumio-cs-folders"></ul><label style="display:flex;gap:8px;align-items:center;margin-top:8px"><input type="checkbox" id="lumio-cs-term"/> Allow terminal / Python</label><div class="row"><button type="button" id="lumio-cs-add">Grant folder…</button><button type="button" id="lumio-cs-clear">Clear Lumio browser data</button></div></div>';
    document.documentElement.appendChild(wrap);
    var panel = wrap.querySelector("#lumio-cs-panel");
    wrap.querySelector("#lumio-cs-toggle").onclick = function () {
      panel.classList.toggle("open");
      if (panel.classList.contains("open")) refresh();
    };
    wrap.querySelector("#lumio-cs-add").onclick = async function () {
      try {
        await invoke("grant_folder_ui");
        await refresh();
      } catch (e) {}
    };
    wrap.querySelector("#lumio-cs-term").onchange = async function (ev) {
      try {
        await invoke("set_terminal_grant", { enabled: ev.target.checked });
      } catch (e) {
        ev.target.checked = false;
      }
    };
    wrap.querySelector("#lumio-cs-clear").onclick = async function () {
      if (!window.confirm("Clear cookies and site data in Lumio’s browser? Your Chrome/Edge/Safari profiles are not touched.")) return;
      try {
        await invoke("reset_browser_profile");
      } catch (e) {}
    };
    async function refresh() {
      try {
        var data = await invoke("list_folder_grants");
        wrap.querySelector("#lumio-cs-term").checked = !!data.terminal;
        var ul = wrap.querySelector("#lumio-cs-folders");
        ul.innerHTML = "";
        (data.folders || []).forEach(function (f) {
          var li = document.createElement("li");
          li.textContent = f.path;
          var rm = document.createElement("button");
          rm.type = "button";
          rm.textContent = "Revoke";
          rm.onclick = async function () {
            await invoke("revoke_folder_grant", { id: f.id });
            await refresh();
          };
          li.appendChild(rm);
          ul.appendChild(li);
        });
        if (!(data.folders || []).length) {
          ul.innerHTML = "<li>No folders granted yet.</li>";
        }
      } catch (e) {}
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", settings);
  else settings();
})();

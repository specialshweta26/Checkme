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
})();

(function () {
  if (window.__lumioDesktop) return;
  window.__lumioDesktop = true;

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

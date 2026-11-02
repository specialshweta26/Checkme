(() => {
  try {
    if (!window.__lumioTeachBuf) window.__lumioTeachBuf = [];
    if (window.__lumioTeach) return true;
    window.__lumioTeach = true;
    const push = (row) => {
      try {
        window.__lumioTeachBuf.push(row);
      } catch (e) {}
    };
    document.addEventListener(
      "click",
      (e) => {
        push({
          kind: "click",
          button: e.button === 2 ? 3 : e.button === 1 ? 2 : 1,
          x: Math.round(e.clientX),
          y: Math.round(e.clientY),
          window: document.title || location.href,
        });
      },
      true
    );
    document.addEventListener(
      "keydown",
      (e) => {
        if (e.metaKey || e.ctrlKey || e.altKey) {
          const parts = [];
          if (e.ctrlKey || e.metaKey) parts.push("ctrl");
          if (e.altKey) parts.push("alt");
          if (e.shiftKey) parts.push("shift");
          parts.push(e.key);
          push({ kind: "key", key: parts.join("+") });
          return;
        }
        if (e.key === "Enter" || e.key === "Tab" || e.key === "Escape" || e.key === "Backspace") {
          push({ kind: "key", key: e.key });
          return;
        }
        if (e.key.length === 1) push({ kind: "char", char: e.key });
      },
      true
    );
    document.addEventListener(
      "wheel",
      (e) => {
        const direction =
          Math.abs(e.deltaY) >= Math.abs(e.deltaX)
            ? e.deltaY > 0
              ? "down"
              : "up"
            : e.deltaX > 0
              ? "right"
              : "left";
        push({ kind: "scroll", direction });
      },
      { passive: true, capture: true }
    );
    return true;
  } catch (e) {
    return false;
  }
})();

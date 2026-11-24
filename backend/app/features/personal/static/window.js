"use strict";

// A persistent connection also works in background tabs; no throttled JS heartbeat.
(() => {
  let socket = null;
  let retry = null;
  let leaving = false;
  let enabled = false;

  function connect() {
    if (!enabled || leaving || socket) return;
    const url = new URL("/api/window/ws", location.href);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const current = new WebSocket(url);
    socket = current;
    current.onclose = () => {
      if (socket === current) socket = null;
      if (!leaving) retry = setTimeout(connect, 1000);
    };
    current.onerror = () => current.close();
  }

  window.addEventListener("pagehide", () => {
    leaving = true;
    clearTimeout(retry);
    if (socket) socket.close();
  });
  window.addEventListener("pageshow", () => {
    leaving = false;
    connect();
  });
  fetch("/api/window", { credentials: "same-origin" })
    .then((response) => response.ok ? response.json() : null)
    .then((config) => {
      enabled = config?.exit_on_close === true;
      connect();
    })
    .catch(() => {});
})();

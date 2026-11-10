// Live camera frames + overlay metadata + event feed from the detection
// process (mounted at /live on the same port). One WebSocket for all
// cameras: binary = 12-byte session id + 4-byte JSON length + JSON + JPEG.
import { onUnmounted, reactive, ref } from "vue";
import { api } from "../api";

const ID_LEN = 12;

export function useLive({ autostart = false, withFeed = false } = {}) {
  const sessions = ref([]); // [{id, filename, camera_id, running, error, people}]
  const frames = reactive({}); // session id -> { url, meta }
  const feed = ref([]);
  const state = ref("connecting"); // connecting | live | unavailable | idle
  let ws = null;
  let poll = null;
  let closed = false;
  let retry = null;

  async function loadSessions() {
    try {
      const list = (await api("/live/api/sessions")).sessions || [];
      if (closed) return false;
      sessions.value = list;
      // Drop frames of sessions that no longer exist (restart = new ids).
      const ids = new Set(list.map((s) => s.id));
      for (const id of Object.keys(frames)) {
        if (!ids.has(id)) {
          URL.revokeObjectURL(frames[id].url);
          delete frames[id];
        }
      }
      // Recover from an earlier failed poll as soon as the server answers.
      if (!list.length) state.value = "idle";
      else state.value = ws && ws.readyState === WebSocket.OPEN ? "live" : "connecting";
      return true;
    } catch (err) {
      if (!closed) state.value = "unavailable";
      return false;
    }
  }

  async function start() {
    // Starts every enabled camera of the logged-in store (replaces running ones).
    const res = await api("/live/api/session/cloud", { method: "POST", body: {} });
    feed.value = []; // a restart starts a new event sequence
    await loadSessions();
    return res;
  }

  function connect() {
    if (closed) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/live/api/ws/frames?mode=meta`);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => { state.value = "live"; };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "feed" && withFeed) {
            // A reconnect resends the server's buffer: skip items we already have.
            const seen = new Set(feed.value.map((f) => `${f.ts}-${f.seq}`));
            const fresh = msg.items.filter((f) => !seen.has(`${f.ts}-${f.seq}`)).reverse();
            if (fresh.length) feed.value = [...fresh, ...feed.value].slice(0, 30);
          }
        } catch { /* ignore */ }
        return;
      }
      const buf = ev.data;
      if (buf.byteLength <= ID_LEN + 4) return;
      const id = new TextDecoder().decode(buf.slice(0, ID_LEN));
      const len = new DataView(buf, ID_LEN, 4).getUint32(0);
      const meta = JSON.parse(new TextDecoder().decode(buf.slice(ID_LEN + 4, ID_LEN + 4 + len)));
      const blob = new Blob([buf.slice(ID_LEN + 4 + len)], { type: "image/jpeg" });
      const prev = frames[id];
      frames[id] = { url: URL.createObjectURL(blob), meta };
      if (prev) URL.revokeObjectURL(prev.url);
    };
    ws.onclose = () => {
      if (closed) return;
      retry = setTimeout(connect, 2000);
    };
  }

  async function init() {
    const ok = await loadSessions();
    if (!ok || closed) return;
    if (autostart && !sessions.value.length) {
      try {
        await start();
      } catch {
        /* no cameras yet / remote edge — the page shows why */
      }
    }
    if (closed) return; // left the page while we were waiting
    connect();
    poll = setInterval(loadSessions, 5000);
  }

  onUnmounted(() => {
    closed = true;
    clearInterval(poll);
    clearTimeout(retry);
    if (ws) ws.close();
    for (const f of Object.values(frames)) URL.revokeObjectURL(f.url);
  });

  return { sessions, frames, feed, state, init, start, loadSessions };
}

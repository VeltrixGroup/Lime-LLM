<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from "vue";
import CameraTile from "./CameraTile.vue";

const MAX_CAMERAS = 16;
const SESSION_ID_LEN = 12;

// This app is served two ways: standalone at "/" (storeguard dashboard) and
// proxied under "/live/" through the cloud cabinet. A hardcoded "/api/..."
// would hit the wrong origin's root when proxied, so every API call and the
// WebSocket URL go through this — computed from the current page's own
// directory, so it resolves to "" at root or "/live" under the proxy.
const API_BASE = (() => {
  const path = window.location.pathname;
  return path.endsWith("/") ? path.slice(0, -1) : path.substring(0, path.lastIndexOf("/"));
})();

function apiPath(path) {
  return `${API_BASE}${path}`;
}

// ---------- state ----------

const running = ref(false);
// Latched true once any session reports running, so the poll can tell
// "still loading the model" (never ran) apart from "stopped" (ran, then not).
let everRunning = false;

const sessions = ref([]);
const frameUrls = reactive({}); // session id -> object URL for <img :src>
const blobUrls = new Map(); // session id -> last object URL (revoked on replace)
const expandedId = ref(null);

const cameraUrlRows = ref([""]);
const cloudServer = ref("");
const cloudToken = ref("");

const videos = ref([]);
const dataDir = ref("");
const selectedLocal = ref("");
const selectedFile = ref(null);

const everyN = ref(1);
const statusMsg = ref("");
const statusError = ref(false);

let ws = null;
let wsRetryTimer = null;
let statsTimer = null;

// ---------- derived ----------

const cameraUrls = computed(() => {
  const urls = [];
  for (const raw of cameraUrlRows.value) {
    const v = (raw || "").trim();
    if (v && !urls.includes(v)) urls.push(v);
  }
  return urls;
});

const canStart = computed(
  () => Boolean(cameraUrls.value.length || selectedLocal.value || selectedFile.value)
);

const gridCols = computed(() => {
  const count = sessions.value.length;
  if (count <= 1) return 1;
  if (count <= 4) return 2;
  if (count <= 9) return 3;
  return 4;
});

const isMulti = computed(() => sessions.value.length > 1 && !expandedId.value);
const isExpanded = computed(
  () => Boolean(expandedId.value) && sessions.value.some((s) => s.id === expandedId.value)
);

const totals = computed(() => {
  let people = 0;
  let paid = 0;
  let notPaid = 0;
  let fps = 0;
  const peopleStatus = [];
  for (const s of sessions.value) {
    people += s.people || 0;
    paid += s.paid || 0;
    notPaid += s.not_paid || 0;
    fps += Number(s.fps || 0);
    for (const p of s.people_status || []) {
      peopleStatus.push({
        key: `${s.id}-${p.track_id}`,
        status: p.status,
        label:
          sessions.value.length > 1
            ? `${s.filename} · id ${p.track_id}: ${p.status}`
            : `id ${p.track_id}: ${p.status}`,
      });
    }
  }
  return { people, paid, notPaid, fps, peopleStatus };
});

// ---------- status / camera rows ----------

function setStatus(msg, isError = false) {
  statusMsg.value = msg || "";
  statusError.value = Boolean(isError);
}

function addCameraRow() {
  if (cameraUrlRows.value.length >= MAX_CAMERAS) return;
  cameraUrlRows.value.push("");
}

function removeCameraRow(index) {
  if (cameraUrlRows.value.length <= 1) return;
  cameraUrlRows.value.splice(index, 1);
}

function onCameraRowInput() {
  if (cameraUrls.value.length) {
    selectedLocal.value = "";
    selectedFile.value = null;
  }
}

function startHint() {
  if (running.value) return;
  if (cameraUrls.value.length) {
    setStatus(
      cameraUrls.value.length === 1
        ? "Start connects 1 camera"
        : `Start connects ${cameraUrls.value.length} cameras`
    );
  } else if (selectedLocal.value) {
    setStatus(`Start plays data/${selectedLocal.value}`);
  } else if (selectedFile.value) {
    setStatus(`Start uploads ${selectedFile.value.name}`);
  }
}

// ---------- data/ picker + upload ----------

async function loadVideos() {
  try {
    const res = await fetch(apiPath("/api/videos"));
    if (!res.ok) throw new Error(`list failed (${res.status})`);
    const data = await res.json();
    videos.value = data.videos || [];
    dataDir.value = data.data_dir || "data/";
    if (selectedLocal.value && !videos.value.some((v) => v.path === selectedLocal.value)) {
      selectedLocal.value = "";
    }
    if (!cameraUrls.value.length) {
      setStatus(
        videos.value.length
          ? `${videos.value.length} video(s) in data/`
          : `Looking in ${dataDir.value} — or add camera URLs`
      );
    }
  } catch (err) {
    setStatus(err.message || String(err), true);
  }
}

function onLocalChange() {
  if (selectedLocal.value) {
    selectedFile.value = null;
  }
  startHint();
}

function onFileChange(e) {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  selectedFile.value = file;
  selectedLocal.value = "";
  startHint();
}

// ---------- tiles ----------

function toggleExpand(id) {
  expandedId.value = expandedId.value === id ? null : id;
}

async function removeTile(id) {
  try {
    await fetch(apiPath(`/api/session/${id}`), { method: "DELETE" });
  } catch {
    /* ignore */
  }
  sessions.value = sessions.value.filter((s) => s.id !== id);
  const url = blobUrls.get(id);
  if (url) {
    URL.revokeObjectURL(url);
    blobUrls.delete(id);
    delete frameUrls[id];
  }
  if (expandedId.value === id) expandedId.value = null;
  if (!sessions.value.length) await stopAll(false);
}

function clearTiles() {
  sessions.value = [];
  expandedId.value = null;
  for (const url of blobUrls.values()) URL.revokeObjectURL(url);
  blobUrls.clear();
  for (const key of Object.keys(frameUrls)) delete frameUrls[key];
}

// ---------- websocket frame feed ----------

function openWs() {
  closeWs();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}${apiPath("/api/ws/frames")}`);
  ws.binaryType = "arraybuffer";
  ws.onmessage = (ev) => {
    if (!(ev.data instanceof ArrayBuffer)) return;
    if (ev.data.byteLength <= SESSION_ID_LEN) return;
    const id = new TextDecoder().decode(ev.data.slice(0, SESSION_ID_LEN));
    if (!sessions.value.some((s) => s.id === id)) return;
    const blob = new Blob([ev.data.slice(SESSION_ID_LEN)], { type: "image/jpeg" });
    const url = URL.createObjectURL(blob);
    const prev = blobUrls.get(id);
    frameUrls[id] = url;
    blobUrls.set(id, url);
    if (prev) URL.revokeObjectURL(prev);
  };
  ws.onclose = () => {
    ws = null;
    if (running.value) wsRetryTimer = setTimeout(openWs, 1000);
  };
}

function closeWs() {
  if (wsRetryTimer) {
    clearTimeout(wsRetryTimer);
    wsRetryTimer = null;
  }
  if (ws) {
    ws.onclose = null;
    ws.close();
    ws = null;
  }
}

// ---------- stats polling ----------

function startStatsPoll() {
  stopStatsPoll();
  everRunning = false;
  statsTimer = setInterval(async () => {
    try {
      const res = await fetch(apiPath("/api/sessions"));
      if (!res.ok) return;
      const data = await res.json();
      const newSessions = data.sessions || [];
      sessions.value = newSessions;

      const liveIds = new Set(newSessions.map((s) => s.id));
      if (expandedId.value && !liveIds.has(expandedId.value)) expandedId.value = null;
      for (const id of blobUrls.keys()) {
        if (!liveIds.has(id)) {
          URL.revokeObjectURL(blobUrls.get(id));
          blobUrls.delete(id);
          delete frameUrls[id];
        }
      }

      const anyRunning = newSessions.some((s) => s.running);
      const errors = newSessions.filter((s) => s.error).map((s) => `${s.filename}: ${s.error}`);
      if (anyRunning) everRunning = true;
      if (errors.length) setStatus(errors.join(" | "), true);

      // Only "stopped" if the server has no sessions at all, or they ran and
      // then all stopped. While models are still cold-loading (never ran
      // yet), leave the live view up instead of tearing it down.
      if (running.value && (!newSessions.length || (everRunning && !anyRunning))) {
        running.value = false;
        closeWs();
        stopStatsPoll();
        clearTiles();
        setStatus(errors.length ? errors.join(" | ") : "Stopped", errors.length > 0);
      }
    } catch {
      /* ignore transient poll errors */
    }
  }, 700);
}

function stopStatsPoll() {
  if (statsTimer) {
    clearInterval(statsTimer);
    statsTimer = null;
  }
}

// ---------- start / stop ----------

async function startCamerasReq(urls) {
  setStatus(urls.length === 1 ? "Connecting to camera…" : `Connecting ${urls.length} cameras…`);
  const res = await fetch(apiPath("/api/session/cameras"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls, process_every: Number(everyN.value) }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Camera connect failed (${res.status})`);
  }
  return (await res.json()).sessions || [];
}

async function startLocalReq(path) {
  setStatus(`Opening data/${path}…`);
  const res = await fetch(apiPath("/api/session/local"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, process_every: Number(everyN.value), loop: true }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Open failed (${res.status})`);
  }
  const data = await res.json();
  const startRes = await fetch(apiPath(`/api/session/${data.id}/start?process_every=${everyN.value}`), {
    method: "POST",
  });
  if (!startRes.ok) {
    const err = await startRes.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to start");
  }
  return [data];
}

async function startUploadReq(file) {
  const body = new FormData();
  body.append("file", file);
  body.append("process_every", everyN.value);
  body.append("loop", "true");
  setStatus("Uploading…");
  const res = await fetch(apiPath("/api/session"), { method: "POST", body });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Upload failed (${res.status})`);
  }
  const data = await res.json();
  const startRes = await fetch(apiPath(`/api/session/${data.id}/start?process_every=${everyN.value}`), {
    method: "POST",
  });
  if (!startRes.ok) {
    const err = await startRes.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to start");
  }
  return [data];
}

async function startCloudReq(server, token) {
  setStatus("Connecting to cloud…");
  const res = await fetch(apiPath("/api/session/cloud"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ server, token, process_every: Number(everyN.value) }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Cloud connect failed (${res.status})`);
  }
  return res.json();
}

function onSessionsStarted(newSessions, label) {
  sessions.value = newSessions;
  expandedId.value = null;
  openWs();
  startStatsPoll();
  setStatus(
    label ||
      (newSessions.length === 1
        ? `Detecting — ${newSessions[0].filename}`
        : `Detecting on ${newSessions.length} cameras`)
  );
}

async function onStart() {
  try {
    running.value = true;
    let started;
    if (cameraUrls.value.length) {
      started = await startCamerasReq(cameraUrls.value);
    } else if (selectedLocal.value) {
      started = await startLocalReq(selectedLocal.value);
    } else if (selectedFile.value) {
      started = await startUploadReq(selectedFile.value);
    } else {
      throw new Error("Add a camera URL, pick data/, or upload a video");
    }
    onSessionsStarted(started);
  } catch (err) {
    running.value = false;
    clearTiles();
    setStatus(err.message || String(err), true);
  }
}

async function onCloudConnect() {
  const server = cloudServer.value.trim();
  const token = cloudToken.value.trim();
  if (!server || !token) {
    setStatus("Enter both the cloud server URL and an agent token", true);
    return;
  }
  try {
    localStorage.setItem("storeguard_cloud_server", server);
    localStorage.setItem("storeguard_cloud_token", token);
  } catch {
    /* localStorage unavailable — not fatal */
  }
  try {
    running.value = true;
    const data = await startCloudReq(server, token);
    const started = data.sessions || [];
    if (!started.length) throw new Error("No cameras connected");
    const dropped = (data.total_enabled || started.length) - started.length;
    onSessionsStarted(
      started,
      `Connected ${started.length} camera${started.length === 1 ? "" : "s"}` +
        (data.tenant_name ? ` from ${data.tenant_name}` : "") +
        (dropped > 0 ? ` (${dropped} more skipped — 16 max)` : "")
    );
  } catch (err) {
    running.value = false;
    clearTiles();
    setStatus(err.message || String(err), true);
  }
}

async function stopAll(callServer = true) {
  if (callServer) {
    try {
      await fetch(apiPath("/api/sessions/stop"), { method: "POST" });
    } catch {
      /* ignore */
    }
  }
  closeWs();
  stopStatsPoll();
  clearTiles();
  running.value = false;
  setStatus("Stopped");
}

// ---------- init ----------

async function reattachRunningSessions() {
  // A page reload must not lose the live view: sessions keep running
  // server-side, so rebuild the grid and re-join the frame feed.
  try {
    const res = await fetch(apiPath("/api/sessions"));
    if (!res.ok) return;
    const data = await res.json();
    const newSessions = data.sessions || [];
    if (!newSessions.length) return;
    running.value = true;
    onSessionsStarted(newSessions);
  } catch {
    /* server unreachable — leave the idle UI */
  }
}

function restoreCloudFields() {
  try {
    cloudServer.value = localStorage.getItem("storeguard_cloud_server") || "";
    cloudToken.value = localStorage.getItem("storeguard_cloud_token") || "";
  } catch {
    /* localStorage unavailable — leave fields blank */
  }
}

onMounted(() => {
  loadVideos();
  restoreCloudFields();
  reattachRunningSessions();
});

onUnmounted(() => {
  closeWs();
  stopStatsPoll();
});
</script>

<template>
  <div class="grain"></div>
  <main class="shell">
    <header class="top">
      <h1 class="brand">storeguard</h1>
      <p class="tagline">Person detection on your cameras</p>
    </header>

    <section class="stage" aria-label="Detection preview">
      <div class="viewport" :class="{ multi: isMulti }">
        <div
          class="grid"
          :class="{ expanded: isExpanded }"
          :style="{ '--cols': gridCols }"
          :hidden="sessions.length === 0"
        >
          <CameraTile
            v-for="s in sessions"
            :key="s.id"
            :session="s"
            :frame-url="frameUrls[s.id] || ''"
            :expanded="expandedId === s.id"
            @toggle-expand="toggleExpand(s.id)"
            @close="removeTile(s.id)"
          />
        </div>
        <div class="placeholder" :hidden="sessions.length > 0">
          <p>
            Add up to {{ MAX_CAMERAS }} camera URLs, pick a clip from
            <code>data/</code>, or upload a video.
          </p>
        </div>
        <div class="hud" :hidden="sessions.length === 0 || isExpanded">
          <div class="hud-row">
            <span>{{ sessions.length }} camera{{ sessions.length === 1 ? "" : "s" }}</span>
            <span>{{ totals.people }} {{ totals.people === 1 ? "person" : "people" }}</span>
            <span>{{ totals.paid }} paid</span>
            <span>{{ totals.notPaid }} not paid</span>
            <span>{{ totals.fps.toFixed(1) }} fps</span>
          </div>
          <ul class="hud-people">
            <li v-for="p in totals.peopleStatus" :key="p.key" :class="p.status === 'paid' ? 'paid' : 'not-paid'">
              {{ p.label }}
            </li>
          </ul>
        </div>
      </div>
    </section>

    <section class="controls" aria-label="Playback controls">
      <div class="cameras">
        <div class="cameras-head">
          <span class="picker-label">Camera URLs</span>
          <span class="cameras-count">{{ cameraUrlRows.length }} / {{ MAX_CAMERAS }}</span>
          <button
            type="button"
            class="btn ghost"
            :disabled="running || cameraUrlRows.length >= MAX_CAMERAS"
            @click="addCameraRow"
          >
            + Add camera
          </button>
        </div>
        <div class="camera-rows">
          <div v-for="(row, i) in cameraUrlRows" :key="i" class="camera-row">
            <input
              type="text"
              v-model="cameraUrlRows[i]"
              placeholder="rtsp://user:pass@192.168.1.64:554/Streaming/Channels/101"
              autocomplete="off"
              spellcheck="false"
              :disabled="running"
              @input="onCameraRowInput"
            />
            <button
              type="button"
              class="camera-remove"
              title="Remove this camera"
              :disabled="running || cameraUrlRows.length <= 1"
              @click="removeCameraRow(i)"
            >
              ×
            </button>
          </div>
        </div>
      </div>

      <div class="cameras cloud-connect">
        <div class="cameras-head">
          <span class="picker-label">Or connect from cloud</span>
        </div>
        <div class="cloud-row">
          <input
            type="text"
            v-model="cloudServer"
            placeholder="http://127.0.0.1:8000"
            autocomplete="off"
            spellcheck="false"
            :disabled="running"
          />
          <input
            type="password"
            v-model="cloudToken"
            placeholder="agent token (Edge devices in the cabinet)"
            autocomplete="off"
            :disabled="running"
          />
          <button type="button" class="btn ghost" :disabled="running" @click="onCloudConnect">
            Connect
          </button>
        </div>
      </div>

      <label class="picker">
        <span class="picker-label">From data/</span>
        <select v-model="selectedLocal" :disabled="running" @change="onLocalChange">
          <option value="">
            {{ videos.length ? `Select from data/ (${videos.length})` : "No videos in data/ — drop an .mp4 there" }}
          </option>
          <option v-for="v in videos" :key="v.path" :value="v.path">{{ v.name }}</option>
        </select>
        <button type="button" class="btn ghost" :disabled="running" title="Rescan data/" @click="loadVideos">
          Refresh
        </button>
      </label>

      <label class="file">
        <input
          type="file"
          accept="video/*,.mp4,.avi,.mkv,.mov,.webm"
          :disabled="running"
          @change="onFileChange"
        />
        <span>{{ selectedFile ? selectedFile.name : "Or upload" }}</span>
      </label>

      <label class="every">
        Every
        <input type="range" min="1" max="8" v-model="everyN" />
        <span class="every-val">{{ everyN }}</span>
        frame
      </label>

      <div class="actions">
        <button type="button" class="btn primary" :disabled="running || !canStart" @click="onStart">
          Start
        </button>
        <button type="button" class="btn" :disabled="!running" @click="stopAll(true)">Stop</button>
      </div>

      <p class="status" :class="{ error: statusError }" role="status">{{ statusMsg }}</p>
    </section>
  </main>
</template>

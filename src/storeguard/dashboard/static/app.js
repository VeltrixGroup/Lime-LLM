(() => {
  const MAX_CAMERAS = 16;
  const SESSION_ID_LEN = 12;

  const cameraRows = document.getElementById("camera-rows");
  const cameraCount = document.getElementById("camera-count");
  const btnAddCamera = document.getElementById("btn-add-camera");
  const localSelect = document.getElementById("local-video");
  const btnRefresh = document.getElementById("btn-refresh");
  const fileInput = document.getElementById("file");
  const fileLabel = document.getElementById("file-label");
  const everyInput = document.getElementById("every");
  const everyVal = document.getElementById("every-val");
  const btnStart = document.getElementById("btn-start");
  const btnStop = document.getElementById("btn-stop");
  const statusEl = document.getElementById("status");
  const viewport = document.getElementById("viewport");
  const grid = document.getElementById("grid");
  const placeholder = document.getElementById("placeholder");
  const hud = document.getElementById("hud");
  const statCameras = document.getElementById("stat-cameras");
  const statPeople = document.getElementById("stat-people");
  const statPaid = document.getElementById("stat-paid");
  const statNotPaid = document.getElementById("stat-not-paid");
  const statFps = document.getElementById("stat-fps");
  const peopleList = document.getElementById("stat-people-list");

  let running = false;
  // Latched true once any session reports running, so the poll can tell
  // "still loading the model" (never ran) apart from "stopped" (ran, then not).
  let everRunning = false;
  let statsTimer = null;
  let ws = null;
  let wsRetryTimer = null;
  let selectedFile = null;
  let selectedLocal = "";
  const blobUrls = new Map(); // session id -> last object URL (revoked on replace)

  // ---------- camera url rows ----------

  function rowInputs() {
    return Array.from(cameraRows.querySelectorAll("input"));
  }

  function cameraUrls() {
    const urls = [];
    for (const input of rowInputs()) {
      const v = (input.value || "").trim();
      if (v && !urls.includes(v)) urls.push(v);
    }
    return urls;
  }

  function updateCameraHead() {
    const n = rowInputs().length;
    cameraCount.textContent = `${n} / ${MAX_CAMERAS}`;
    btnAddCamera.disabled = running || n >= MAX_CAMERAS;
    for (const btn of cameraRows.querySelectorAll(".camera-remove")) {
      btn.disabled = running || rowInputs().length <= 1;
    }
  }

  function addCameraRow(value = "") {
    if (rowInputs().length >= MAX_CAMERAS) return null;
    const row = document.createElement("div");
    row.className = "camera-row";

    const input = document.createElement("input");
    input.type = "text";
    input.placeholder =
      "rtsp://user:pass@192.168.1.64:554/Streaming/Channels/101";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.value = value;
    input.addEventListener("input", () => {
      if (input.value.trim()) {
        selectedLocal = "";
        localSelect.value = "";
        selectedFile = null;
        fileInput.value = "";
        fileLabel.textContent = "Or upload";
      }
      updateStartHint();
    });

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "camera-remove";
    remove.title = "Remove this camera";
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      if (rowInputs().length <= 1) return;
      row.remove();
      updateCameraHead();
      updateStartHint();
    });

    row.appendChild(input);
    row.appendChild(remove);
    cameraRows.appendChild(row);
    updateCameraHead();
    return input;
  }

  btnAddCamera.addEventListener("click", () => {
    const input = addCameraRow();
    if (input) input.focus();
    updateStartHint();
  });

  // ---------- status / enablement ----------

  function setStatus(msg, isError = false) {
    statusEl.textContent = msg || "";
    statusEl.classList.toggle("error", Boolean(isError));
  }

  function canStart() {
    return Boolean(cameraUrls().length || selectedLocal || selectedFile);
  }

  function updateStartHint() {
    btnStart.disabled = running || !canStart();
    if (running) return;
    const urls = cameraUrls();
    if (urls.length) {
      setStatus(
        urls.length === 1
          ? "Start connects 1 camera"
          : `Start connects ${urls.length} cameras`
      );
    } else if (selectedLocal) {
      setStatus(`Start plays data/${selectedLocal}`);
    } else if (selectedFile) {
      setStatus(`Start uploads ${selectedFile.name}`);
    }
  }

  function setRunning(next) {
    running = next;
    btnStart.disabled = running || !canStart();
    btnStop.disabled = !running;
    fileInput.disabled = running;
    localSelect.disabled = running;
    btnRefresh.disabled = running;
    for (const input of rowInputs()) input.disabled = running;
    updateCameraHead();
  }

  everyInput.addEventListener("input", () => {
    everyVal.textContent = everyInput.value;
  });

  // ---------- data/ picker + upload ----------

  async function loadVideos() {
    localSelect.innerHTML = "";
    try {
      const res = await fetch("/api/videos");
      if (!res.ok) throw new Error(`list failed (${res.status})`);
      const data = await res.json();
      const videos = data.videos || [];
      if (!videos.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "No videos in data/ — drop an .mp4 there";
        localSelect.appendChild(opt);
        selectedLocal = "";
        if (!cameraUrls().length) {
          setStatus(`Looking in ${data.data_dir || "data/"} — or add camera URLs`);
        }
      } else {
        const blank = document.createElement("option");
        blank.value = "";
        blank.textContent = `Select from data/ (${videos.length})`;
        localSelect.appendChild(blank);
        for (const v of videos) {
          const opt = document.createElement("option");
          opt.value = v.path;
          opt.textContent = v.name;
          localSelect.appendChild(opt);
        }
        if (selectedLocal && videos.some((v) => v.path === selectedLocal)) {
          localSelect.value = selectedLocal;
        } else {
          selectedLocal = "";
        }
        if (!cameraUrls().length) {
          setStatus(`${videos.length} video(s) in data/`);
        }
      }
    } catch (err) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "Could not list data/";
      localSelect.appendChild(opt);
      setStatus(err.message || String(err), true);
    }
    updateStartHint();
  }

  localSelect.addEventListener("change", () => {
    selectedLocal = localSelect.value || "";
    if (selectedLocal) {
      selectedFile = null;
      fileInput.value = "";
      fileLabel.textContent = "Or upload";
    }
    updateStartHint();
  });

  btnRefresh.addEventListener("click", () => {
    loadVideos();
  });

  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    selectedFile = file;
    selectedLocal = "";
    localSelect.value = "";
    fileLabel.textContent = file.name;
    updateStartHint();
  });

  // ---------- tiles ----------

  function gridColumns(count) {
    if (count <= 1) return 1;
    if (count <= 4) return 2;
    if (count <= 9) return 3;
    return 4;
  }

  function buildTile(s) {
    const tile = document.createElement("figure");
    tile.className = "tile";
    tile.dataset.id = s.id;

    const img = document.createElement("img");
    img.alt = `Detection stream — ${s.filename}`;
    tile.appendChild(img);

    const cap = document.createElement("figcaption");
    cap.className = "tile-hud";
    const name = document.createElement("span");
    name.className = "tile-name";
    name.textContent = s.filename;
    const meta = document.createElement("span");
    meta.className = "tile-meta";
    meta.textContent = "connecting…";
    cap.appendChild(name);
    cap.appendChild(meta);
    tile.appendChild(cap);

    const close = document.createElement("button");
    close.type = "button";
    close.className = "tile-close";
    close.title = "Disconnect this camera";
    close.textContent = "×";
    close.addEventListener("click", () => removeTile(s.id));
    tile.appendChild(close);
    return tile;
  }

  function syncStage() {
    const count = grid.querySelectorAll(".tile").length;
    grid.style.setProperty("--cols", gridColumns(count));
    viewport.classList.toggle("multi", count > 1);
    grid.hidden = count === 0;
    placeholder.hidden = count > 0;
    hud.hidden = count === 0;
  }

  function buildTiles(sessions) {
    grid.innerHTML = "";
    for (const s of sessions) grid.appendChild(buildTile(s));
    syncStage();
  }

  // Incrementally match the grid to the server's session list: drop tiles that
  // vanished (revoking their blob) and add tiles for sessions that appeared
  // (e.g. cameras that were still loading their model on a previous poll).
  function reconcileTiles(sessions) {
    const liveIds = new Set(sessions.map((s) => s.id));
    for (const tile of grid.querySelectorAll(".tile")) {
      if (!liveIds.has(tile.dataset.id)) {
        const url = blobUrls.get(tile.dataset.id);
        if (url) {
          URL.revokeObjectURL(url);
          blobUrls.delete(tile.dataset.id);
        }
        tile.remove();
      }
    }
    const have = new Set(
      Array.from(grid.querySelectorAll(".tile")).map((t) => t.dataset.id)
    );
    for (const s of sessions) {
      if (!have.has(s.id)) grid.appendChild(buildTile(s));
    }
    syncStage();
  }

  async function removeTile(id) {
    try {
      await fetch(`/api/session/${id}`, { method: "DELETE" });
    } catch {
      /* ignore */
    }
    const tile = grid.querySelector(`.tile[data-id="${id}"]`);
    if (tile) tile.remove();
    const url = blobUrls.get(id);
    if (url) {
      URL.revokeObjectURL(url);
      blobUrls.delete(id);
    }
    const left = grid.querySelectorAll(".tile").length;
    grid.style.setProperty("--cols", gridColumns(left));
    viewport.classList.toggle("multi", left > 1);
    if (!left) stopAll(false);
  }

  function clearTiles() {
    grid.innerHTML = "";
    grid.hidden = true;
    viewport.classList.remove("multi");
    placeholder.hidden = false;
    hud.hidden = true;
    for (const url of blobUrls.values()) URL.revokeObjectURL(url);
    blobUrls.clear();
  }

  // ---------- websocket frame feed ----------

  function openWs() {
    closeWs();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/api/ws/frames`);
    ws.binaryType = "arraybuffer";
    ws.onmessage = (ev) => {
      if (!(ev.data instanceof ArrayBuffer)) return;
      if (ev.data.byteLength <= SESSION_ID_LEN) return;
      const id = new TextDecoder().decode(ev.data.slice(0, SESSION_ID_LEN));
      const img = grid.querySelector(`.tile[data-id="${id}"] img`);
      if (!img) return;
      const blob = new Blob([ev.data.slice(SESSION_ID_LEN)], {
        type: "image/jpeg",
      });
      const url = URL.createObjectURL(blob);
      img.src = url;
      const prev = blobUrls.get(id);
      if (prev) URL.revokeObjectURL(prev);
      blobUrls.set(id, url);
    };
    ws.onclose = () => {
      ws = null;
      if (running) {
        wsRetryTimer = setTimeout(openWs, 1000);
      }
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
        const res = await fetch("/api/sessions");
        if (!res.ok) return;
        const data = await res.json();
        const sessions = data.sessions || [];
        let people = 0;
        let paid = 0;
        let notPaid = 0;
        let fps = 0;
        let anyRunning = false;
        const errors = [];
        peopleList.innerHTML = "";

        // Match tiles to the live session set (adds late-starting cameras,
        // drops ones that vanished — e.g. disconnected from another tab).
        reconcileTiles(sessions);

        for (const s of sessions) {
          people += s.people || 0;
          paid += s.paid || 0;
          notPaid += s.not_paid || 0;
          fps += Number(s.fps || 0);
          if (s.running) anyRunning = true;
          if (s.error) errors.push(`${s.filename}: ${s.error}`);

          const tile = grid.querySelector(`.tile[data-id="${s.id}"]`);
          if (tile) {
            const meta = tile.querySelector(".tile-meta");
            if (s.error) {
              meta.textContent = s.error;
              tile.classList.add("error");
            } else {
              tile.classList.remove("error");
              const n = s.people || 0;
              meta.textContent = `${n} ${n === 1 ? "person" : "people"} · ${Number(
                s.fps || 0
              ).toFixed(1)} fps`;
            }
            tile.classList.toggle("stopped", !s.running && !s.error);
          }

          for (const p of s.people_status || []) {
            const li = document.createElement("li");
            li.className = p.status === "paid" ? "paid" : "not-paid";
            li.textContent =
              sessions.length > 1
                ? `${s.filename} · id ${p.track_id}: ${p.status}`
                : `id ${p.track_id}: ${p.status}`;
            peopleList.appendChild(li);
          }
        }
        const nCams = sessions.length;
        statCameras.textContent = nCams === 1 ? "1 camera" : `${nCams} cameras`;
        statPeople.textContent = people === 1 ? "1 person" : `${people} people`;
        statPaid.textContent = `${paid} paid`;
        statNotPaid.textContent = `${notPaid} not paid`;
        statFps.textContent = `${fps.toFixed(1)} fps`;

        if (anyRunning) everRunning = true;
        if (errors.length) {
          setStatus(errors.join(" | "), true);
        }
        // Only "stopped" if the server has no sessions at all, or they ran and
        // then all stopped. While models are still cold-loading (never ran
        // yet), leave the live view up instead of tearing it down.
        if (running && (!sessions.length || (everRunning && !anyRunning))) {
          setRunning(false);
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

  async function startCameras(urls) {
    setStatus(
      urls.length === 1
        ? "Connecting to camera…"
        : `Connecting ${urls.length} cameras…`
    );
    const res = await fetch("/api/session/cameras", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls, process_every: Number(everyInput.value) }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Camera connect failed (${res.status})`);
    }
    const data = await res.json();
    return data.sessions || [];
  }

  async function startLocal(path) {
    setStatus(`Opening data/${path}…`);
    const res = await fetch("/api/session/local", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path,
        process_every: Number(everyInput.value),
        loop: true,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Open failed (${res.status})`);
    }
    const data = await res.json();
    const startRes = await fetch(
      `/api/session/${data.id}/start?process_every=${everyInput.value}`,
      { method: "POST" }
    );
    if (!startRes.ok) {
      const err = await startRes.json().catch(() => ({}));
      throw new Error(err.detail || "Failed to start");
    }
    return [data];
  }

  async function startUpload(file) {
    const body = new FormData();
    body.append("file", file);
    body.append("process_every", everyInput.value);
    body.append("loop", "true");
    setStatus("Uploading…");
    const res = await fetch("/api/session", { method: "POST", body });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Upload failed (${res.status})`);
    }
    const data = await res.json();
    const startRes = await fetch(
      `/api/session/${data.id}/start?process_every=${everyInput.value}`,
      { method: "POST" }
    );
    if (!startRes.ok) {
      const err = await startRes.json().catch(() => ({}));
      throw new Error(err.detail || "Failed to start");
    }
    return [data];
  }

  btnStart.addEventListener("click", async () => {
    const urls = cameraUrls();
    try {
      setRunning(true);
      let sessions;
      if (urls.length) {
        sessions = await startCameras(urls);
      } else if (selectedLocal) {
        sessions = await startLocal(selectedLocal);
      } else if (selectedFile) {
        sessions = await startUpload(selectedFile);
      } else {
        throw new Error("Add a camera URL, pick data/, or upload a video");
      }
      buildTiles(sessions);
      openWs();
      startStatsPoll();
      setStatus(
        sessions.length === 1
          ? `Detecting — ${sessions[0].filename}`
          : `Detecting on ${sessions.length} cameras`
      );
    } catch (err) {
      setRunning(false);
      clearTiles();
      setStatus(err.message || String(err), true);
    }
  });

  async function stopAll(callServer = true) {
    if (callServer) {
      try {
        await fetch("/api/sessions/stop", { method: "POST" });
      } catch {
        /* ignore */
      }
    }
    closeWs();
    stopStatsPoll();
    clearTiles();
    setRunning(false);
    setStatus("Stopped");
  }

  btnStop.addEventListener("click", () => stopAll(true));

  // ---------- init ----------

  async function reattachRunningSessions() {
    // A page reload must not lose the live view: sessions keep running
    // server-side, so rebuild the grid and re-join the frame feed.
    try {
      const res = await fetch("/api/sessions");
      if (!res.ok) return;
      const data = await res.json();
      // Show every registered session, not just running ones — on a reload
      // during model cold-start they report running=false but must still appear
      // (the poll's latch keeps the view up until they actually run).
      const sessions = data.sessions || [];
      if (!sessions.length) return;
      buildTiles(sessions);
      setRunning(true);
      openWs();
      startStatsPoll();
      setStatus(
        sessions.length === 1
          ? `Detecting — ${sessions[0].filename}`
          : `Detecting on ${sessions.length} cameras`
      );
    } catch {
      /* server unreachable — leave the idle UI */
    }
  }

  addCameraRow();
  loadVideos();
  reattachRunningSessions();
})();

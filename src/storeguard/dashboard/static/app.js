(() => {
  const localSelect = document.getElementById("local-video");
  const btnRefresh = document.getElementById("btn-refresh");
  const fileInput = document.getElementById("file");
  const fileLabel = document.getElementById("file-label");
  const everyInput = document.getElementById("every");
  const everyVal = document.getElementById("every-val");
  const btnStart = document.getElementById("btn-start");
  const btnStop = document.getElementById("btn-stop");
  const statusEl = document.getElementById("status");
  const streamImg = document.getElementById("stream");
  const placeholder = document.getElementById("placeholder");
  const hud = document.getElementById("hud");
  const statPeople = document.getElementById("stat-people");
  const statPaid = document.getElementById("stat-paid");
  const statNotPaid = document.getElementById("stat-not-paid");
  const statFps = document.getElementById("stat-fps");
  const statFrame = document.getElementById("stat-frame");
  const peopleList = document.getElementById("stat-people-list");

  let sessionId = null;
  let statsTimer = null;
  let selectedFile = null;
  let selectedLocal = "";

  function setStatus(msg, isError = false) {
    statusEl.textContent = msg || "";
    statusEl.classList.toggle("error", Boolean(isError));
  }

  function canStart() {
    return Boolean(selectedLocal || selectedFile);
  }

  function setRunning(running) {
    btnStart.disabled = !canStart() || running;
    btnStop.disabled = !sessionId || !running;
    fileInput.disabled = running;
    localSelect.disabled = running;
    btnRefresh.disabled = running;
  }

  function updateStartEnabled() {
    btnStart.disabled = !canStart();
  }

  everyInput.addEventListener("input", () => {
    everyVal.textContent = everyInput.value;
  });

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
        setStatus(`Looking in ${data.data_dir || "data/"}`);
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
        setStatus(`${videos.length} video(s) in data/`);
      }
    } catch (err) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "Could not list data/";
      localSelect.appendChild(opt);
      setStatus(err.message || String(err), true);
    }
    updateStartEnabled();
  }

  localSelect.addEventListener("change", () => {
    selectedLocal = localSelect.value || "";
    if (selectedLocal) {
      selectedFile = null;
      fileInput.value = "";
      fileLabel.textContent = "Or upload";
      setStatus(`Ready: data/${selectedLocal}`);
    }
    updateStartEnabled();
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
    btnStart.disabled = false;
    setStatus(`Ready (upload): ${file.name}`);
  });

  async function createSession() {
    if (selectedLocal) {
      setStatus(`Opening data/${selectedLocal}…`);
      const res = await fetch("/api/session/local", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: selectedLocal,
          process_every: Number(everyInput.value),
          loop: true,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Open failed (${res.status})`);
      }
      return res.json();
    }
    if (!selectedFile) {
      throw new Error("Pick a video from data/ or upload one");
    }
    const body = new FormData();
    body.append("file", selectedFile);
    body.append("process_every", everyInput.value);
    body.append("loop", "true");
    setStatus("Uploading…");
    const res = await fetch("/api/session", { method: "POST", body });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Upload failed (${res.status})`);
    }
    return res.json();
  }

  function attachStream(id) {
    placeholder.hidden = true;
    hud.hidden = false;
    streamImg.hidden = false;
    streamImg.src = `/api/session/${id}/stream?t=${Date.now()}`;
  }

  function clearStream() {
    streamImg.removeAttribute("src");
    streamImg.hidden = true;
    placeholder.hidden = false;
    hud.hidden = true;
  }

  function startStatsPoll(id) {
    stopStatsPoll();
    statsTimer = setInterval(async () => {
      try {
        const res = await fetch(`/api/session/${id}/stats`);
        if (!res.ok) return;
        const s = await res.json();
        const n = s.people || 0;
        statPeople.textContent = n === 1 ? "1 person" : `${n} people`;
        statPaid.textContent = `${s.paid || 0} paid`;
        statNotPaid.textContent = `${s.not_paid || 0} not paid`;
        statFps.textContent = `${Number(s.fps || 0).toFixed(1)} fps`;
        const total = s.total_frames || 0;
        statFrame.textContent =
          total > 0 ? `frame ${s.frame || 0} / ${total}` : `frame ${s.frame || 0}`;
        peopleList.innerHTML = "";
        for (const p of s.people_status || []) {
          const li = document.createElement("li");
          const st = p.status === "paid" ? "paid" : "not-paid";
          li.className = st;
          li.textContent = `id ${p.track_id}: ${p.status}`;
          peopleList.appendChild(li);
        }
        if (s.error) {
          setStatus(s.error, true);
        }
        if (!s.running && sessionId === id) {
          setRunning(false);
          setStatus(s.error ? s.error : "Stopped");
        }
      } catch {
        /* ignore transient poll errors */
      }
    }, 400);
  }

  function stopStatsPoll() {
    if (statsTimer) {
      clearInterval(statsTimer);
      statsTimer = null;
    }
  }

  btnStart.addEventListener("click", async () => {
    try {
      setRunning(true);
      const data = await createSession();
      sessionId = data.id;
      setStatus(`Detecting — ${data.filename}`);
      attachStream(sessionId);
      const startRes = await fetch(
        `/api/session/${sessionId}/start?process_every=${everyInput.value}`,
        { method: "POST" }
      );
      if (!startRes.ok) {
        const err = await startRes.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to start");
      }
      startStatsPoll(sessionId);
      setRunning(true);
    } catch (err) {
      setRunning(false);
      clearStream();
      setStatus(err.message || String(err), true);
    }
  });

  btnStop.addEventListener("click", async () => {
    if (!sessionId) return;
    try {
      await fetch(`/api/session/${sessionId}/stop`, { method: "POST" });
    } catch {
      /* ignore */
    }
    stopStatsPoll();
    clearStream();
    setRunning(false);
    setStatus("Stopped");
  });

  loadVideos();
})();

(() => {
  const $ = (id) => document.getElementById(id);
  const loading = $("loading");
  const authView = $("auth-view");
  const appView = $("app-view");
  const statusEl = $("status");

  let me = null; // { user:{id,email,full_name}, tenant:{id,name,slug,role} }

  function setStatus(msg, isError = false) {
    statusEl.textContent = msg || "";
    statusEl.classList.toggle("error", Boolean(isError));
  }

  async function api(path, { method = "GET", body } = {}) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    let data = null;
    try {
      data = await res.json();
    } catch {
      /* no JSON body */
    }
    if (!res.ok) {
      const detail =
        data && data.detail
          ? typeof data.detail === "string"
            ? data.detail
            : "invalid input"
          : `request failed (${res.status})`;
      throw new Error(detail);
    }
    return data;
  }

  function formData(form) {
    const out = {};
    for (const el of form.elements) {
      if (el.name) out[el.name] = el.value;
    }
    return out;
  }

  const isOwner = () => Boolean(me && me.tenant && me.tenant.role === "owner");

  // ---------- auth tabs ----------

  const tabLogin = $("tab-login");
  const tabSignup = $("tab-signup");
  const loginForm = $("login-form");
  const signupForm = $("signup-form");

  function showTab(which) {
    const login = which === "login";
    tabLogin.classList.toggle("active", login);
    tabSignup.classList.toggle("active", !login);
    loginForm.hidden = !login;
    signupForm.hidden = login;
    setStatus("");
  }
  tabLogin.addEventListener("click", () => showTab("login"));
  tabSignup.addEventListener("click", () => showTab("signup"));

  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      me = await api("/api/auth/login", { method: "POST", body: formData(loginForm) });
      await enterApp();
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  signupForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      me = await api("/api/auth/signup", {
        method: "POST",
        body: formData(signupForm),
      });
      await enterApp();
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  // ---------- logged-in controls ----------

  const orgName = $("org-name");
  const whoEmail = $("who-email");
  const whoRole = $("who-role");
  const membersBody = $("members-body");
  const memberCount = $("member-count");
  const addForm = $("add-member-form");
  const btnLogout = $("btn-logout");
  const btnChangePw = $("btn-change-pw");
  const changePwForm = $("changepw-form");

  btnLogout.addEventListener("click", async () => {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch {
      /* ignore */
    }
    me = null;
    showAuth();
  });

  btnChangePw.addEventListener("click", () => {
    changePwForm.hidden = !changePwForm.hidden;
    setStatus("");
  });

  changePwForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/auth/change-password", {
        method: "POST",
        body: formData(changePwForm),
      });
      changePwForm.reset();
      changePwForm.hidden = true;
      setStatus("Password updated");
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/org/members", { method: "POST", body: formData(addForm) });
      addForm.reset();
      setStatus("Member added");
      await loadMembers();
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  function renderMemberRow(m) {
    const tr = document.createElement("tr");
    const owner = isOwner();
    const isSelf = m.id === me.user.id;

    const tdEmail = document.createElement("td");
    tdEmail.textContent = m.email + (isSelf ? " (you)" : "");
    const tdName = document.createElement("td");
    tdName.textContent = m.full_name || "—";

    const tdRole = document.createElement("td");
    if (owner) {
      const sel = document.createElement("select");
      for (const r of ["owner", "staff"]) {
        const opt = document.createElement("option");
        opt.value = r;
        opt.textContent = r;
        if (r === m.role) opt.selected = true;
        sel.appendChild(opt);
      }
      sel.addEventListener("change", async () => {
        try {
          await api(`/api/org/members/${m.id}`, {
            method: "PATCH",
            body: { role: sel.value },
          });
          setStatus("Role updated");
          // If we changed our own role we may no longer be an owner — re-fetch.
          if (isSelf) {
            me = await api("/api/me");
            await enterApp();
          } else {
            await loadMembers();
          }
        } catch (err) {
          setStatus(err.message, true);
          await loadMembers(); // revert the select to the server's truth
        }
      });
      tdRole.appendChild(sel);
    } else {
      const span = document.createElement("span");
      span.className = "role";
      span.textContent = m.role;
      tdRole.appendChild(span);
    }

    const tdAct = document.createElement("td");
    if (owner && !isSelf) {
      const btn = document.createElement("button");
      btn.className = "btn danger sm";
      btn.type = "button";
      btn.textContent = "Remove";
      btn.addEventListener("click", async () => {
        if (!window.confirm(`Remove ${m.email}?`)) return;
        try {
          await api(`/api/org/members/${m.id}`, { method: "DELETE" });
          setStatus("Member removed");
          await loadMembers();
        } catch (err) {
          setStatus(err.message, true);
        }
      });
      tdAct.appendChild(btn);
    }

    tr.append(tdEmail, tdName, tdRole, tdAct);
    return tr;
  }

  async function loadMembers() {
    try {
      const data = await api("/api/org/members");
      const members = data.members || [];
      membersBody.innerHTML = "";
      for (const m of members) membersBody.appendChild(renderMemberRow(m));
      memberCount.textContent = `${members.length} member${
        members.length === 1 ? "" : "s"
      }`;
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  // ---------- cameras ----------

  const camerasList = $("cameras-list");
  const cameraCount = $("camera-count");
  const addCameraForm = $("add-camera-form");

  function mkBtn(text, cls) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn " + cls;
    b.textContent = text;
    return b;
  }

  function labeledInput(labelText, input) {
    const l = document.createElement("label");
    l.textContent = labelText;
    l.appendChild(input);
    return l;
  }

  function textInput(name, value, attrs = {}) {
    const i = document.createElement("input");
    i.type = attrs.type || "text";
    i.name = name;
    i.value = value == null ? "" : value;
    if (attrs.min != null) i.min = attrs.min;
    if (attrs.max != null) i.max = attrs.max;
    if (attrs.maxLength != null) i.maxLength = attrs.maxLength;
    if (attrs.required) i.required = true;
    return i;
  }

  function buildEditForm(cam) {
    const form = document.createElement("form");
    form.className = "cam-edit form";
    form.hidden = true;
    const name = textInput("name", cam.name, { required: true, maxLength: 120 });
    const source = textInput("source", cam.source, {
      required: true,
      maxLength: 1024,
    });
    const every = textInput("process_every", cam.process_every, {
      type: "number",
      min: 1,
      max: 100,
    });
    const enabledLbl = document.createElement("label");
    enabledLbl.className = "chk";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = cam.enabled;
    enabledLbl.append(enabled, document.createTextNode(" enabled"));
    const save = mkBtn("Save", "primary sm");
    save.type = "submit";
    const cancel = mkBtn("Cancel", "ghost sm");
    cancel.addEventListener("click", () => {
      form.hidden = true;
    });

    const row = document.createElement("div");
    row.className = "row";
    row.append(
      labeledInput("name", name),
      labeledInput("source", source),
      labeledInput("every", every),
      enabledLbl,
      save,
      cancel
    );
    form.appendChild(row);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api(`/api/cameras/${cam.id}`, {
          method: "PATCH",
          body: {
            name: name.value,
            source: source.value,
            process_every: Number(every.value) || 1,
            enabled: enabled.checked,
          },
        });
        setStatus("Camera updated");
        await loadCameras();
      } catch (err) {
        setStatus(err.message, true);
      }
    });
    return form;
  }

  function buildZonesForm(cam) {
    const form = document.createElement("form");
    form.className = "cam-zones-edit form";
    form.hidden = true;
    const hint = document.createElement("div");
    hint.className = "muted";
    hint.textContent =
      'JSON: [{"name":"checkout","points":[[x,y],…]}] — coordinates 0..1';
    const ta = document.createElement("textarea");
    ta.className = "zones-json";
    ta.rows = 6;
    ta.value = JSON.stringify(
      cam.zones.map((z) => ({ name: z.name, points: z.points })),
      null,
      2
    );
    const save = mkBtn("Save zones", "primary sm");
    save.type = "submit";
    const cancel = mkBtn("Cancel", "ghost sm");
    cancel.addEventListener("click", () => {
      form.hidden = true;
    });
    form.append(hint, ta, save, cancel);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      let zones;
      try {
        zones = JSON.parse(ta.value || "[]");
      } catch {
        setStatus("Zones must be valid JSON", true);
        return;
      }
      try {
        await api(`/api/cameras/${cam.id}/zones`, {
          method: "PUT",
          body: { zones },
        });
        setStatus("Zones saved");
        await loadCameras();
      } catch (err) {
        setStatus(err.message, true);
      }
    });
    return form;
  }

  function renderCamera(cam) {
    const owner = isOwner();
    const wrap = document.createElement("div");
    wrap.className = "cam";

    const head = document.createElement("div");
    head.className = "cam-head";

    const title = document.createElement("div");
    title.className = "cam-title";
    const nm = document.createElement("span");
    nm.className = "cam-name";
    nm.textContent = cam.name;
    const src = document.createElement("span");
    src.className = "cam-src muted";
    src.textContent = cam.label;
    title.append(nm, src);

    const meta = document.createElement("span");
    meta.className = "cam-meta muted";
    meta.textContent = `every ${cam.process_every} · ${cam.zones.length} zone${
      cam.zones.length === 1 ? "" : "s"
    }`;

    const badge = document.createElement("span");
    badge.className = "badge " + (cam.enabled ? "ok" : "off");
    badge.textContent = cam.enabled ? "enabled" : "disabled";

    head.append(title, meta, badge);

    if (!owner) {
      wrap.appendChild(head);
      return wrap;
    }

    const actions = document.createElement("div");
    actions.className = "cam-actions";
    const editBtn = mkBtn("Edit", "ghost sm");
    const zonesBtn = mkBtn("Zones", "ghost sm");
    const delBtn = mkBtn("Delete", "danger sm");
    actions.append(editBtn, zonesBtn, delBtn);
    head.appendChild(actions);

    const editForm = buildEditForm(cam);
    const zonesForm = buildZonesForm(cam);
    editBtn.addEventListener("click", () => {
      zonesForm.hidden = true;
      editForm.hidden = !editForm.hidden;
    });
    zonesBtn.addEventListener("click", () => {
      editForm.hidden = true;
      zonesForm.hidden = !zonesForm.hidden;
    });
    delBtn.addEventListener("click", async () => {
      if (!window.confirm(`Delete camera "${cam.name}"?`)) return;
      try {
        await api(`/api/cameras/${cam.id}`, { method: "DELETE" });
        setStatus("Camera deleted");
        await loadCameras();
      } catch (err) {
        setStatus(err.message, true);
      }
    });

    wrap.append(head, editForm, zonesForm);
    return wrap;
  }

  async function loadCameras() {
    try {
      const data = await api("/api/cameras");
      const cams = data.cameras || [];
      camerasList.innerHTML = "";
      for (const c of cams) camerasList.appendChild(renderCamera(c));
      cameraCount.textContent = `${cams.length} camera${
        cams.length === 1 ? "" : "s"
      }`;
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  addCameraForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = formData(addCameraForm);
    try {
      await api("/api/cameras", {
        method: "POST",
        body: {
          name: fd.name,
          source: fd.source,
          process_every: Number(fd.process_every) || 1,
          enabled: addCameraForm.elements.enabled.checked,
        },
      });
      addCameraForm.reset();
      addCameraForm.elements.enabled.checked = true;
      setStatus("Camera added");
      await loadCameras();
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  // ---------- edge devices (agent keys) + events ----------

  const devicesCard = $("devices-card");
  const keysList = $("keys-list");
  const keyCount = $("key-count");
  const addKeyForm = $("add-key-form");
  const newKeyBox = $("new-key-box");
  const newKeyToken = $("new-key-token");
  const eventsBody = $("events-body");
  const eventCount = $("event-count");
  const btnRefreshEvents = $("btn-refresh-events");

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  function renderKeyRow(k) {
    const row = document.createElement("div");
    row.className = "cam";
    const head = document.createElement("div");
    head.className = "cam-head";
    const title = document.createElement("div");
    title.className = "cam-title";
    const nm = document.createElement("span");
    nm.className = "cam-name";
    nm.textContent = k.name || "(unnamed)";
    const pfx = document.createElement("span");
    pfx.className = "cam-src muted";
    pfx.textContent = k.prefix + "…";
    title.append(nm, pfx);
    const meta = document.createElement("span");
    meta.className = "cam-meta muted";
    meta.textContent = k.revoked
      ? "revoked"
      : `last seen ${fmtTime(k.last_seen_at)}`;
    head.append(title, meta);
    if (!k.revoked) {
      const actions = document.createElement("div");
      actions.className = "cam-actions";
      const btn = mkBtn("Revoke", "danger sm");
      btn.addEventListener("click", async () => {
        if (
          !window.confirm(
            `Revoke token "${k.name || k.prefix}"? The device will lose access.`
          )
        )
          return;
        try {
          await api(`/api/agent-keys/${k.id}`, { method: "DELETE" });
          setStatus("Token revoked");
          await loadAgentKeys();
        } catch (err) {
          setStatus(err.message, true);
        }
      });
      actions.appendChild(btn);
      head.appendChild(actions);
    }
    row.appendChild(head);
    return row;
  }

  async function loadAgentKeys() {
    try {
      const data = await api("/api/agent-keys");
      const keys = data.keys || [];
      keysList.innerHTML = "";
      for (const k of keys) keysList.appendChild(renderKeyRow(k));
      keyCount.textContent = `${keys.length} token${keys.length === 1 ? "" : "s"}`;
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  addKeyForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = formData(addKeyForm);
    try {
      const created = await api("/api/agent-keys", {
        method: "POST",
        body: { name: fd.name || "" },
      });
      addKeyForm.reset();
      newKeyToken.textContent = created.token;
      newKeyBox.hidden = false;
      setStatus("Token created");
      await loadAgentKeys();
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  function renderEventRow(ev) {
    const tr = document.createElement("tr");
    const time = document.createElement("td");
    time.textContent = fmtTime(ev.ts);
    const cam = document.createElement("td");
    cam.textContent = ev.camera_name || "—";
    const person = document.createElement("td");
    if (ev.person_id) {
      // Click a person to see every camera that saw them (their trail).
      const a = document.createElement("a");
      a.href = "#";
      a.className = "clip-link";
      a.textContent = ev.person_id.slice(0, 8);
      a.title = `Show all cameras that saw ${ev.person_id}`;
      a.addEventListener("click", (e) => {
        e.preventDefault();
        loadEvents(ev.person_id);
      });
      person.appendChild(a);
    } else {
      person.textContent = "—";
    }
    const kind = document.createElement("td");
    kind.textContent = ev.kind;
    const msg = document.createElement("td");
    msg.textContent = ev.message || "";
    const clip = document.createElement("td");
    if (ev.has_clip) {
      const a = document.createElement("a");
      a.href = `/api/events/${ev.id}/clip`;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "view";
      a.className = "clip-link";
      clip.appendChild(a);
    } else {
      clip.textContent = "—";
    }
    tr.append(time, cam, person, kind, msg, clip);
    return tr;
  }

  async function loadEvents(personId = null) {
    try {
      const path = personId
        ? `/api/events?person_id=${encodeURIComponent(personId)}`
        : "/api/events";
      const data = await api(path);
      const evs = data.events || [];
      eventsBody.innerHTML = "";
      for (const ev of evs) eventsBody.appendChild(renderEventRow(ev));
      const base = `${evs.length} event${evs.length === 1 ? "" : "s"}`;
      eventCount.textContent = personId
        ? `${base} · person ${personId.slice(0, 8)} (Refresh to clear)`
        : base;
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  btnRefreshEvents.addEventListener("click", () => loadEvents());

  // ---------- telegram settings ----------

  const telegramCard = $("telegram-card");
  const telegramForm = $("telegram-form");
  const telegramState = $("telegram-state");
  const btnTestTelegram = $("btn-test-telegram");

  async function loadTelegram() {
    try {
      const cfg = await api("/api/settings/telegram");
      telegramForm.elements.enabled.checked = Boolean(cfg.enabled);
      telegramForm.elements.chat_id.value = cfg.chat_id || "";
      telegramForm.elements.bot_token.value = "";
      telegramState.textContent = cfg.enabled
        ? cfg.token_set
          ? "on"
          : "on · no token set"
        : "off";
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  telegramForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = formData(telegramForm);
    try {
      await api("/api/settings/telegram", {
        method: "PUT",
        body: {
          enabled: telegramForm.elements.enabled.checked,
          bot_token: fd.bot_token || "",
          chat_id: fd.chat_id || "",
        },
      });
      setStatus("Telegram settings saved");
      await loadTelegram();
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  btnTestTelegram.addEventListener("click", async () => {
    try {
      const res = await api("/api/settings/telegram/test", { method: "POST" });
      setStatus(
        res.sent ? "Test message sent" : "Telegram rejected the test",
        !res.sent
      );
    } catch (err) {
      setStatus(err.message, true);
    }
  });

  // ---------- view switching ----------

  function showAuth() {
    loading.hidden = true;
    appView.hidden = true;
    authView.hidden = false;
    loginForm.reset();
    signupForm.reset();
    showTab("login");
  }

  async function enterApp() {
    setStatus("");
    loading.hidden = true;
    authView.hidden = true;
    appView.hidden = false;
    orgName.textContent = me.tenant.name;
    whoEmail.textContent = me.user.email;
    whoRole.textContent = me.tenant.role;
    addForm.hidden = !isOwner();
    addCameraForm.hidden = !isOwner();
    devicesCard.hidden = !isOwner();
    telegramCard.hidden = !isOwner();
    changePwForm.hidden = true;
    newKeyBox.hidden = true;
    await loadMembers();
    await loadCameras();
    if (isOwner()) {
      await loadAgentKeys();
      await loadTelegram();
    }
    await loadEvents();
  }

  async function init() {
    try {
      me = await api("/api/me");
      await enterApp();
    } catch {
      showAuth();
    }
  }

  init();
})();

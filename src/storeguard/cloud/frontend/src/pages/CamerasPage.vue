<script setup>
import { reactive, ref, onMounted } from "vue";
import { api } from "../api";
import { setStatus, isOwner } from "../store";
import CameraRow from "../components/CameraRow.vue";
import AddCameraModal from "../modals/AddCameraModal.vue";

const cameras = ref([]);
const loaded = ref(false);
const showAddModal = ref(false);

const defaults = reactive({ username: "", password: "", port: 554, stream_path: "" });
const defaultsState = ref("");

async function loadCameras() {
  try {
    const data = await api("/api/cameras");
    cameras.value = data.cameras || [];
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    loaded.value = true;
  }
}

async function loadDefaults() {
  try {
    const cfg = await api("/api/settings/camera-defaults");
    defaults.username = cfg.username || "";
    defaults.password = "";
    defaults.port = cfg.port;
    defaults.stream_path = cfg.stream_path || "";
    defaultsState.value = cfg.password_set ? "password set" : "no password set";
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function saveDefaults() {
  try {
    await api("/api/settings/camera-defaults", {
      method: "PUT",
      body: {
        username: defaults.username || "",
        password: defaults.password || "",
        port: Number(defaults.port) || 554,
        stream_path: defaults.stream_path || "",
      },
    });
    setStatus("Camera settings saved");
    await loadDefaults();
  } catch (err) {
    setStatus(err.message, true);
  }
}

onMounted(() => {
  loadCameras();
  if (isOwner()) loadDefaults();
});
</script>

<template>
  <div class="page-head">
    <h1>Cameras</h1>
  </div>

  <section v-if="isOwner()" class="card" id="camera-defaults-card">
    <div class="section-head">
      <h2>
        <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="6" x2="20" y2="6"/><circle cx="9" cy="6" r="2"/><line x1="4" y1="12" x2="20" y2="12"/><circle cx="16" cy="12" r="2"/><line x1="4" y1="18" x2="20" y2="18"/><circle cx="11" cy="18" r="2"/></svg>
        Camera settings
      </h2>
      <span class="muted">{{ defaultsState }}</span>
    </div>
    <p class="hint">
      Saved once and reused for every camera you add below — so adding a
      camera only needs its IP address, not a full URL.
    </p>
    <form class="form" @submit.prevent="saveDefaults">
      <label>
        Username
        <input type="text" v-model="defaults.username" placeholder="admin" autocomplete="off" />
      </label>
      <label>
        Password
        <input
          type="password"
          v-model="defaults.password"
          placeholder="leave blank to keep the saved password"
          autocomplete="off"
        />
      </label>
      <label>
        Port
        <input type="number" v-model="defaults.port" min="1" max="65535" />
      </label>
      <label>
        Stream path
        <input type="text" v-model="defaults.stream_path" placeholder="/Streaming/Channels/101" />
      </label>
      <button class="btn primary" type="submit">Save</button>
    </form>
  </section>

  <section class="card">
    <div class="section-head">
      <h2>
        <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5c0-.8.6-1.5 1.5-1.5h2l1-1.5h5l1 1.5h2A1.5 1.5 0 0 1 17 8.5v8A1.5 1.5 0 0 1 15.5 18h-11A1.5 1.5 0 0 1 3 16.5z"/><circle cx="10" cy="12" r="3.2"/><path d="M18.5 10.5 21 9v8l-2.5-1.5"/></svg>
        Cameras
      </h2>
      <div class="section-actions">
        <span class="muted">{{ cameras.length }} camera{{ cameras.length === 1 ? "" : "s" }}</span>
        <button
          v-if="isOwner()"
          class="btn primary sm"
          type="button"
          @click="showAddModal = true"
        >
          + Add camera
        </button>
      </div>
    </div>
    <div v-if="loaded && cameras.length === 0" class="empty">
      No cameras yet — add one below with its IP address.
    </div>
    <CameraRow
      v-for="cam in cameras"
      :key="cam.id"
      :camera="cam"
      @changed="loadCameras"
    />
  </section>

  <AddCameraModal
    v-if="showAddModal"
    @close="showAddModal = false"
    @added="loadCameras"
  />
</template>

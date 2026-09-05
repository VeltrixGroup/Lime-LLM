<script setup>
import { ref, onMounted } from "vue";
import { api } from "../api";
import { setStatus, isOwner, fmtTime } from "../store";
import AddKeyModal from "../modals/AddKeyModal.vue";

const keys = ref([]);
const showAddModal = ref(false);
const newKeyToken = ref("");

async function loadKeys() {
  try {
    const data = await api("/api/agent-keys");
    keys.value = data.keys || [];
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function revokeKey(k) {
  if (!window.confirm(`Revoke token "${k.name || k.prefix}"? The device will lose access.`)) return;
  try {
    await api(`/api/agent-keys/${k.id}`, { method: "DELETE" });
    setStatus("Token revoked");
    await loadKeys();
  } catch (err) {
    setStatus(err.message, true);
  }
}

function onKeyCreated(created) {
  newKeyToken.value = created.token;
  loadKeys();
}

onMounted(loadKeys);
</script>

<template>
  <div class="page-head">
    <h1>Edge devices</h1>
  </div>

  <section class="card" id="devices-card">
    <div class="section-head">
      <h2>
        <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9 3v2.3M13 3v2.3M9 18.7V21M13 18.7V21M3 9h2.3M3 13h2.3M18.7 9H21M18.7 13H21"/></svg>
        Edge devices
      </h2>
      <div class="section-actions">
        <span class="muted">{{ keys.length }} token{{ keys.length === 1 ? "" : "s" }}</span>
        <button
          v-if="isOwner()"
          class="btn primary sm"
          type="button"
          @click="showAddModal = true"
        >
          + Create token
        </button>
      </div>
    </div>

    <div v-if="keys.length === 0" class="empty">
      No device tokens yet — create one below for the store PC.
    </div>
    <div v-for="k in keys" :key="k.id" class="cam">
      <div class="cam-head">
        <div class="cam-title">
          <span class="cam-name">{{ k.name || "(unnamed)" }}</span>
          <span class="cam-src muted">{{ k.prefix }}…</span>
        </div>
        <span class="cam-meta muted">
          {{ k.revoked ? "revoked" : `last seen ${fmtTime(k.last_seen_at)}` }}
        </span>
        <div v-if="!k.revoked && isOwner()" class="cam-actions">
          <button class="btn danger sm" type="button" @click="revokeKey(k)">Revoke</button>
        </div>
      </div>
    </div>

    <div v-if="newKeyToken" class="new-key">
      <div class="muted">Copy this token now — it won't be shown again:</div>
      <code class="token">{{ newKeyToken }}</code>
    </div>
  </section>

  <AddKeyModal
    v-if="showAddModal"
    @close="showAddModal = false"
    @added="onKeyCreated"
  />
</template>

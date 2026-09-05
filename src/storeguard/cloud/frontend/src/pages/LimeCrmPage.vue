<script setup>
import { reactive, ref, onMounted } from "vue";
import { api } from "../api";
import { setStatus } from "../store";

const form = reactive({ enabled: false, webhook_url: "", auth_header: "", auth_token: "" });
const stateLabel = ref("");

async function load() {
  try {
    const cfg = await api("/api/settings/lime-crm");
    form.enabled = Boolean(cfg.enabled);
    form.webhook_url = cfg.webhook_url || "";
    form.auth_header = cfg.auth_header || "";
    form.auth_token = "";
    stateLabel.value = cfg.enabled ? (cfg.token_set ? "on" : "on · no auth token set") : "off";
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function save() {
  try {
    await api("/api/settings/lime-crm", {
      method: "PUT",
      body: {
        enabled: form.enabled,
        webhook_url: form.webhook_url || "",
        auth_header: form.auth_header || "",
        auth_token: form.auth_token || "",
      },
    });
    setStatus("Lime CRM settings saved");
    await load();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function sendTest() {
  try {
    const res = await api("/api/settings/lime-crm/test", { method: "POST" });
    setStatus(res.sent ? "Test notification sent" : "Lime CRM rejected the test", !res.sent);
  } catch (err) {
    setStatus(err.message, true);
  }
}

onMounted(load);
</script>

<template>
  <div class="page-head">
    <h1>Lime CRM</h1>
  </div>

  <section class="card" id="lime-crm-card">
    <div class="section-head">
      <h2>
        <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M10 14a4 4 0 0 0 5.7.4l2-2a4 4 0 0 0-5.6-5.7L10.6 8.2"/><path d="M14 10a4 4 0 0 0-5.7-.4l-2 2a4 4 0 0 0 5.6 5.7l1.5-1.5"/></svg>
        Lime CRM
      </h2>
      <span class="muted">{{ stateLabel }}</span>
    </div>
    <p class="hint">
      Sends every detection (theft alert, idle, phone use — everything) as a
      JSON POST to this URL, as soon as it happens. Works with any receiver
      that accepts a webhook, not just Lime CRM.
    </p>
    <form class="form" @submit.prevent="save">
      <label class="chk">
        <input type="checkbox" v-model="form.enabled" /> Enable Lime CRM notifications
      </label>
      <label>
        Webhook URL
        <input
          type="text"
          v-model="form.webhook_url"
          placeholder="https://your-lime-crm.example.com/hooks/storeguard"
        />
      </label>
      <label>
        Auth header name (optional)
        <input type="text" v-model="form.auth_header" placeholder="e.g. Authorization or X-Api-Key" />
      </label>
      <label>
        Auth header value
        <input
          type="password"
          v-model="form.auth_token"
          placeholder="leave blank to keep the saved value"
          autocomplete="off"
        />
      </label>
      <div class="row">
        <button class="btn primary" type="submit">Save</button>
        <button class="btn ghost" type="button" @click="sendTest">Send test</button>
      </div>
    </form>
  </section>
</template>

<script setup>
import { reactive, ref, onMounted } from "vue";
import { api } from "../api";
import { setStatus } from "../store";

const form = reactive({ enabled: false, bot_token: "", chat_id: "" });
const stateLabel = ref("");

async function load() {
  try {
    const cfg = await api("/api/settings/telegram");
    form.enabled = Boolean(cfg.enabled);
    form.chat_id = cfg.chat_id || "";
    form.bot_token = "";
    stateLabel.value = cfg.enabled ? (cfg.token_set ? "on" : "on · no token set") : "off";
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function save() {
  try {
    await api("/api/settings/telegram", {
      method: "PUT",
      body: {
        enabled: form.enabled,
        bot_token: form.bot_token || "",
        chat_id: form.chat_id || "",
      },
    });
    setStatus("Telegram settings saved");
    await load();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function sendTest() {
  try {
    const res = await api("/api/settings/telegram/test", { method: "POST" });
    setStatus(res.sent ? "Test message sent" : "Telegram rejected the test", !res.sent);
  } catch (err) {
    setStatus(err.message, true);
  }
}

onMounted(load);
</script>

<template>
  <div class="page-head">
    <h1>Telegram alerts</h1>
  </div>

  <section class="card" id="telegram-card">
    <div class="section-head">
      <h2>
        <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M21 3 3.5 10.3c-.9.4-.9 1.7.1 2l4.9 1.6 1.7 5.2c.3.9 1.5 1.1 2.1.3l2.4-3 4.6 3.4c.8.6 1.9.1 2-.9L21 3Z"/><path d="M8.5 13.9 18 6.5"/></svg>
        Telegram alerts
      </h2>
      <span class="muted">{{ stateLabel }}</span>
    </div>
    <form class="form" @submit.prevent="save">
      <label class="chk">
        <input type="checkbox" v-model="form.enabled" /> Enable Telegram alerts
      </label>
      <label>
        Bot token
        <input
          type="password"
          v-model="form.bot_token"
          placeholder="leave blank to keep the saved token"
          autocomplete="off"
        />
      </label>
      <label>
        Chat ID
        <input type="text" v-model="form.chat_id" placeholder="e.g. -1001234567890" />
      </label>
      <div class="row">
        <button class="btn primary" type="submit">Save</button>
        <button class="btn ghost" type="button" @click="sendTest">Send test</button>
      </div>
    </form>
  </section>
</template>

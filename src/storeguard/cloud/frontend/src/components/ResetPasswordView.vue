<script setup>
import { reactive, ref } from "vue";
import { api } from "../api";
import { setStatus } from "../store";

const token = new URLSearchParams(window.location.search).get("token") || "";
const form = reactive({ password: "", confirm: "" });
const done = ref(false);

async function submit() {
  if (form.password !== form.confirm) {
    setStatus("Passwords don't match", true);
    return;
  }
  try {
    await api("/api/auth/reset-password", {
      method: "POST",
      body: { token, new_password: form.password },
    });
    done.value = true;
  } catch (err) {
    setStatus(err.message, true);
  }
}

function backToLogin() {
  window.location.href = "/";
}
</script>

<template>
  <main class="auth-shell">
    <header class="top">
      <h1 class="brand">storeguard</h1>
      <p class="tagline">cloud control plane</p>
    </header>

    <div class="card form">
      <template v-if="!token">
        <p class="hint">This reset link is missing its token — open the link from your email again.</p>
        <button type="button" class="btn primary" @click="backToLogin">Back to log in</button>
      </template>
      <template v-else-if="done">
        <p class="hint">Password updated — you can log in with your new password now.</p>
        <button type="button" class="btn primary" @click="backToLogin">Back to log in</button>
      </template>
      <template v-else>
        <form @submit.prevent="submit">
          <label>
            New password
            <input
              type="password"
              v-model="form.password"
              autocomplete="new-password"
              minlength="8"
              maxlength="72"
              required
            />
          </label>
          <label>
            Confirm new password
            <input
              type="password"
              v-model="form.confirm"
              autocomplete="new-password"
              minlength="8"
              maxlength="72"
              required
            />
          </label>
          <button class="btn primary" type="submit">Set new password</button>
        </form>
        <button type="button" class="link-btn" @click="backToLogin">Back to log in</button>
      </template>
    </div>
  </main>
</template>

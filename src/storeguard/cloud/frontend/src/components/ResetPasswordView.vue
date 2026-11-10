<script setup>
import { reactive, ref } from "vue";
import { api } from "../api";
import { setStatus } from "../store";

const token = new URLSearchParams(window.location.search).get("token") || "";
const form = reactive({ password: "", confirm: "" });
const done = ref(false);

async function submit() {
  if (form.password !== form.confirm) {
    setStatus("Пароли не совпадают", true);
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
  <div class="auth">
    <section class="auth-form">
      <form v-if="!done" @submit.prevent="submit">
        <h1>Новый пароль</h1>
        <label class="field">Пароль<input v-model="form.password" type="password" minlength="8" autocomplete="new-password" required /></label>
        <label class="field">Повторите пароль<input v-model="form.confirm" type="password" minlength="8" autocomplete="new-password" required /></label>
        <button class="btn primary lg" type="submit">Сохранить пароль</button>
      </form>
      <form v-else @submit.prevent="backToLogin">
        <h1>Пароль изменён</h1>
        <span class="muted">Теперь войдите с новым паролем.</span>
        <button class="btn primary lg" type="submit">Ко входу</button>
      </form>
    </section>
  </div>
</template>

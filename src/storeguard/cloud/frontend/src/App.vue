<script setup>
import { ref, onMounted } from "vue";
import { api } from "./api";
import { me } from "./store";
import AppShell from "./components/AppShell.vue";
import LoginView from "./components/LoginView.vue";
import RegisterView from "./components/RegisterView.vue";
import ResetPasswordView from "./components/ResetPasswordView.vue";
import StatusToast from "./components/StatusToast.vue";

// Reached from an emailed link, logged in or not.
const isResetPage = window.location.pathname === "/reset-password";
const loading = ref(true);
const authMode = ref(window.location.pathname === "/register" ? "register" : "login");

onMounted(async () => {
  try {
    me.value = await api("/api/me");
  } catch {
    me.value = null;
  } finally {
    loading.value = false;
  }
});

function showAuth(mode) {
  authMode.value = mode;
  window.history.replaceState(null, "", mode === "register" ? "/register" : "/");
}
</script>

<template>
  <ResetPasswordView v-if="isResetPage" />
  <section v-else-if="loading" class="loading-screen">Загрузка…</section>
  <template v-else-if="!me">
    <RegisterView v-if="authMode === 'register'" @login="showAuth('login')" />
    <LoginView v-else @register="showAuth('register')" />
  </template>
  <AppShell v-else />
  <StatusToast />
</template>

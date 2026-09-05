<script setup>
import { ref, onMounted } from "vue";
import { api } from "./api";
import { me } from "./store";
import AuthView from "./components/AuthView.vue";
import AppShell from "./components/AppShell.vue";
import ResetPasswordView from "./components/ResetPasswordView.vue";
import StatusToast from "./components/StatusToast.vue";

// Reached from an emailed link, logged in or not — checked ahead of the
// auth gate below rather than added to the router's (login-only) route
// table, the same way AuthView itself isn't a router page.
const isResetPasswordPage = window.location.pathname === "/reset-password";

const loading = ref(true);

onMounted(async () => {
  try {
    me.value = await api("/api/me");
  } catch {
    me.value = null;
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div class="grain"></div>
  <ResetPasswordView v-if="isResetPasswordPage" />
  <section v-else-if="loading" class="loading-screen">Loading…</section>
  <AuthView v-else-if="!me" />
  <AppShell v-else />
  <StatusToast />
</template>

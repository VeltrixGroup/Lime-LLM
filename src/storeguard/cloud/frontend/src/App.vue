<script setup>
import { ref, onMounted } from "vue";
import { api } from "./api";
import { me } from "./store";
import AuthView from "./components/AuthView.vue";
import AppShell from "./components/AppShell.vue";
import StatusToast from "./components/StatusToast.vue";

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
  <section v-if="loading" class="loading-screen">Loading…</section>
  <AuthView v-else-if="!me" />
  <AppShell v-else />
  <StatusToast />
</template>

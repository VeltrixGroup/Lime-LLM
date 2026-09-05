<script setup>
import { ref } from "vue";
import { api } from "../api";
import { me } from "../store";
import SidebarNav from "./SidebarNav.vue";
import ChangePasswordModal from "../modals/ChangePasswordModal.vue";

const showChangePw = ref(false);

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch {
    /* ignore */
  }
  me.value = null;
}
</script>

<template>
  <div class="app-shell">
    <a class="skip-link" href="#page-content">Skip to content</a>
    <SidebarNav />
    <div class="app-main">
      <header class="topbar">
        <div>
          <div class="org-name">{{ me.tenant.name }}</div>
          <div class="who">
            <span>{{ me.user.email }}</span> ·
            <span class="role">{{ me.tenant.role }}</span>
          </div>
        </div>
        <div class="topbar-actions">
          <button class="btn ghost" type="button" @click="showChangePw = true">
            Change password
          </button>
          <button class="btn ghost" type="button" @click="logout">Log out</button>
        </div>
      </header>

      <div class="page-wrap" id="page-content">
        <router-view />
      </div>
    </div>
  </div>

  <ChangePasswordModal v-if="showChangePw" @close="showChangePw = false" />
</template>

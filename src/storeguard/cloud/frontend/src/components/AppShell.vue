<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { can, initials, me, setStatus, stores } from "../store";
import { NAV } from "../router";
import Icon from "./Icon.vue";
import Logo from "./Logo.vue";

const route = useRoute();
const router = useRouter();
const nav = computed(() => NAV.filter((n) => !n.perm || can(n.perm)));
const allowed = computed(() => !route.meta.perm || can(route.meta.perm));
const newIncidents = ref(0);

// Sidebar: collapsed to an icon rail on desktop (remembered), a slide-in
// drawer on phones (closed after picking a page).
const collapsed = ref(readPref());
const drawerOpen = ref(false);
function readPref() {
  try {
    return localStorage.getItem("lime.sidebar") === "collapsed";
  } catch {
    return false;
  }
}
function toggleSidebar() {
  collapsed.value = !collapsed.value;
  try {
    localStorage.setItem("lime.sidebar", collapsed.value ? "collapsed" : "open");
  } catch {
    /* private mode */
  }
}
watch(() => route.fullPath, () => { drawerOpen.value = false; });

// Store picker: a real dropdown only when there is something to switch to.
const storeList = computed(() =>
  stores.value.map((s) => (s.id === me.value.tenant.id ? { ...s, name: me.value.tenant.name } : s))
);

async function loadStores() {
  try {
    stores.value = (await api("/api/stores")).stores;
  } catch {
    stores.value = [];
  }
}

async function loadBadge() {
  if (!can("archive")) return;
  try {
    newIncidents.value = (await api("/api/incidents?range=today&limit=1")).new_count;
  } catch {
    /* ignore */
  }
}

async function switchStore(e) {
  const id = e.target.value;
  try {
    await api("/api/stores/switch", { method: "POST", body: { tenant_id: id } });
    window.location.reload();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function logout() {
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  me.value = null;
  router.replace("/");
}

let badgeTimer = null;
onMounted(() => {
  loadStores();
  loadBadge();
  badgeTimer = setInterval(loadBadge, 30000);
});
onUnmounted(() => clearInterval(badgeTimer));
</script>

<template>
  <div class="shell" :class="{ collapsed, 'drawer-open': drawerOpen }">
    <header class="topbar">
      <button class="btn icon" type="button" aria-label="Открыть меню" @click="drawerOpen = true"><Icon name="menu" /></button>
      <router-link to="/overview" class="brand"><Logo />Лайм AI</router-link>
    </header>
    <div class="scrim" @click="drawerOpen = false"></div>

    <aside class="side" aria-label="Меню">
      <div class="side-head">
        <router-link to="/overview" class="brand" :title="collapsed ? 'Лайм AI' : undefined"><Logo /><span class="lbl">Лайм AI</span></router-link>
        <button class="btn icon side-toggle" type="button" :aria-label="collapsed ? 'Развернуть меню' : 'Свернуть меню'"
                :title="collapsed ? 'Развернуть меню' : 'Свернуть меню'" @click="toggleSidebar">
          <Icon :name="collapsed ? 'expand' : 'collapse'" /></button>
        <button class="btn icon side-close" type="button" aria-label="Закрыть меню" @click="drawerOpen = false"><Icon name="close" /></button>
      </div>

      <div class="store lbl">
        <span class="store-cap">Магазин</span>
        <select v-if="storeList.length > 1" class="input sm" :value="me.tenant.id" aria-label="Магазин" @change="switchStore">
          <option v-for="s in storeList" :key="s.id" :value="s.id">{{ s.name }}</option>
        </select>
        <div v-else class="store-name" :title="me.tenant.name">{{ me.tenant.name }}</div>
      </div>

      <nav class="nav" aria-label="Разделы">
        <router-link v-for="n in nav" :key="n.path" :to="n.path" :title="collapsed ? n.label : undefined">
          <Icon :name="n.icon" /><span class="lbl">{{ n.label }}</span>
          <span v-if="n.path === '/incidents' && newIncidents" class="badge-count">{{ newIncidents }}</span>
        </router-link>
      </nav>

      <div class="usercard">
        <span class="avatar" :title="collapsed ? (me.user.full_name || me.user.email) : undefined">{{ initials(me.user.full_name || me.user.email) }}</span>
        <div class="lbl user-lbl">
          <strong style="font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">
            {{ me.user.full_name || me.user.email }}
          </strong>
          <span style="font-size: 12px; color: var(--accent)">{{ me.tenant.role_name }}</span>
        </div>
        <button class="btn icon logout" type="button" aria-label="Выйти" title="Выйти" @click="logout"
                style="border: none"><Icon name="logout" /></button>
      </div>
    </aside>
    <main class="main">
      <router-view v-if="allowed" />
      <div v-else class="card empty">
        У вашей роли нет доступа к этому разделу. Права назначает владелец магазина.
      </div>
    </main>
  </div>
</template>

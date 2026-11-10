<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { api } from "../api";
import { fmtTime, initials, me, setStatus } from "../store";
import Icon from "../components/Icon.vue";
import Modal from "../components/Modal.vue";

const users = ref([]);
const roles = ref([]);
const groups = ref([]);
const perms = ref({}); // role key -> Set-like object of permission keys (edited copy)
const saved = ref({});
const menuFor = ref(null);
const modal = ref(null); // "invite" | "role" | { mode: "changeRole", user }
const invite = reactive({ full_name: "", email: "", phone: "", password: "", role: "security" });
const roleForm = reactive({ name: "", color: "#8AB4FF" });

const ROLE_BG = { "#B6F24A": "#263022", "#8AB4FF": "#1A2233", "#FF8F5A": "#3A1F14", "#FFC53D": "#33290F" };
const roleBg = (c) => ROLE_BG[c] || "#22261F";

function clonePerms(list) {
  const out = {};
  for (const r of list) out[r.key] = Object.fromEntries(r.permissions.map((p) => [p, true]));
  return out;
}

async function load() {
  try {
    users.value = (await api("/api/users")).users;
    const r = await api("/api/roles");
    roles.value = r.roles;
    groups.value = r.groups;
    perms.value = clonePerms(r.roles);
    saved.value = clonePerms(r.roles);
  } catch (err) {
    setStatus(err.message, true);
  }
}

const dirty = computed(() => JSON.stringify(perms.value) !== JSON.stringify(saved.value));
const onCount = (r) => (r.locked ? groups.value.reduce((n, g) => n + g.rows.length, 0) : Object.keys(perms.value[r.key] || {}).length);

function toggle(r, key) {
  if (r.locked) return;
  const cur = { ...(perms.value[r.key] || {}) };
  if (cur[key]) delete cur[key];
  else cur[key] = true;
  perms.value = { ...perms.value, [r.key]: cur };
}

async function savePerms() {
  try {
    const body = { roles: Object.fromEntries(Object.entries(perms.value).map(([k, v]) => [k, Object.keys(v)])) };
    const r = await api("/api/roles/permissions", { method: "PUT", body });
    roles.value = r.roles;
    perms.value = clonePerms(r.roles);
    saved.value = clonePerms(r.roles);
    setStatus("Права сохранены");
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function sendInvite() {
  try {
    await api("/api/org/members", { method: "POST", body: { ...invite } });
    setStatus(`Пользователь добавлен. Передайте ему логин (${invite.email}) и временный пароль.`);
    modal.value = null;
    Object.assign(invite, { full_name: "", email: "", phone: "", password: "", role: "security" });
    load();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function createRole() {
  try {
    await api("/api/roles", { method: "POST", body: { name: roleForm.name, color: roleForm.color, permissions: ["live"] } });
    modal.value = null;
    roleForm.name = "";
    setStatus("Роль создана — отметьте её права в таблице ниже");
    load();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function deleteRole(r) {
  if (!window.confirm(`Удалить роль «${r.name}»?`)) return;
  try {
    await api(`/api/roles/${r.key}`, { method: "DELETE" });
    load();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function changeRole(u, role) {
  try {
    await api(`/api/org/members/${u.id}`, { method: "PATCH", body: { role } });
    modal.value = null;
    load();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function removeUser(u) {
  menuFor.value = null;
  if (!window.confirm(`Убрать «${u.full_name || u.email}» из магазина?`)) return;
  try {
    await api(`/api/org/members/${u.id}`, { method: "DELETE" });
    load();
  } catch (err) {
    setStatus(err.message, true);
  }
}

function tempPassword() {
  const abc = "abcdefghjkmnpqrstuvwxyz23456789";
  invite.password = Array.from(crypto.getRandomValues(new Uint32Array(10)), (n) => abc[n % abc.length]).join("");
}

const contact = (u) => (u.is_me ? "вы · " + (me.value.tenant.role === "owner" ? "владелец аккаунта" : u.email) : u.phone || u.email);
const lastLogin = (u) => (u.is_me ? "сейчас" : fmtTime(u.last_login_at));
onMounted(load);
</script>

<template>
  <div class="page-head" @click="menuFor = null">
    <div style="display: flex; flex-direction: column; gap: 2px">
      <h1>Доступ и роли</h1>
      <span class="sub">Кто что видит и что может делать в панели</span>
    </div>
    <div class="actions">
      <button class="btn" type="button" @click="modal = 'role'">+ Новая роль</button>
      <button class="btn primary" type="button" @click="tempPassword(); modal = 'invite'">+ Пригласить пользователя</button>
    </div>
  </div>

  <div class="card flush">
    <div class="table">
      <div style="min-width: 820px">
        <div class="card-head" style="padding: 16px 20px; border-bottom: 1px solid var(--line)">
          <strong class="card-title">Пользователи</strong><span class="muted" style="font-size: 13px">{{ users.length }} пользователей</span></div>
        <div class="thead" style="grid-template-columns: 2fr 1.3fr 1.4fr 1fr 0.6fr"><span>ПОЛЬЗОВАТЕЛЬ</span><span>РОЛЬ</span><span>МАГАЗИНЫ</span><span>ПОСЛЕДНИЙ ВХОД</span><span></span></div>
        <div v-for="u in users" :key="u.id" class="tr" style="grid-template-columns: 2fr 1.3fr 1.4fr 1fr 0.6fr">
          <div class="who"><span class="avatar sm">{{ initials(u.full_name || u.email) }}</span>
            <div class="t"><strong>{{ u.full_name || u.email }}</strong><small>{{ contact(u) }}</small></div></div>
          <span class="pill" style="justify-self: start; padding: 4px 10px" :style="{ background: roleBg(u.role_color), color: u.role_color }">{{ u.role_name }}</span>
          <span class="text-2" style="font-size: 14px">{{ u.stores }}</span>
          <span class="muted mono" style="font-size: 13px">{{ lastLogin(u) }}</span>
          <div style="justify-self: end; position: relative">
            <button v-if="!u.is_me" class="btn icon" type="button" aria-label="Действия с пользователем" @click.stop="menuFor = menuFor === u.id ? null : u.id"><Icon name="dots" :size="16" /></button>
            <div v-if="menuFor === u.id" class="menu" @click.stop>
              <button type="button" @click="menuFor = null; modal = { mode: 'changeRole', user: u }">Сменить роль</button>
              <button type="button" class="danger" @click="removeUser(u)">Убрать из магазина</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="card flush">
    <div class="table">
      <div style="min-width: 820px">
        <div class="card-head" style="padding: 16px 20px; border-bottom: 1px solid var(--line)">
          <strong class="card-title">Права ролей</strong>
          <span class="muted" style="font-size: 13px">Нажмите на ячейку, чтобы выдать или забрать право · Владелец имеет все права</span></div>
        <div :style="{ display: 'grid', gridTemplateColumns: `2.4fr repeat(${roles.length}, minmax(0, 1fr))`, gap: '8px', padding: '14px 20px', borderBottom: '1px solid var(--line)', alignItems: 'end' }">
          <span></span>
          <div v-for="r in roles" :key="r.key" style="display: flex; flex-direction: column; align-items: center; gap: 2px; text-align: center">
            <strong style="font-size: 14px" :style="{ color: r.color }">{{ r.name }}</strong>
            <span class="muted" style="font-size: 12px">{{ r.count }} польз. · {{ onCount(r) }} прав</span>
            <button v-if="!r.is_system" type="button" style="background: none; border: none; color: var(--faint); font-size: 12px; cursor: pointer" @click="deleteRole(r)">удалить</button>
          </div>
        </div>
        <template v-for="g in groups" :key="g.name">
          <div class="section-label" style="padding: 12px 20px 4px">{{ g.name }}</div>
          <div v-for="row in g.rows" :key="row[0]" :style="{ display: 'grid', gridTemplateColumns: `2.4fr repeat(${roles.length}, minmax(0, 1fr))`, gap: '8px', padding: '6px 20px', alignItems: 'center', borderBottom: '1px solid var(--line-soft)' }">
            <span style="font-size: 14px">{{ row[1] }}</span>
            <button v-for="r in roles" :key="r.key" type="button" @click="toggle(r, row[0])"
                    :aria-label="`${r.name}: ${row[1]} — ${r.locked || perms[r.key]?.[row[0]] ? 'разрешено' : 'запрещено'}`"
                    :style="(() => { const on = r.locked || perms[r.key]?.[row[0]]; return { justifySelf: 'center', width: '44px', height: '32px', borderRadius: '8px', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: r.locked ? 'default' : 'pointer',
                      background: on ? (r.locked ? '#263022' : 'var(--accent)') : 'transparent', border: `1px solid ${on ? (r.locked ? '#263022' : 'var(--accent)') : 'var(--line-strong)'}`, color: on ? (r.locked ? 'var(--accent)' : 'var(--bg)') : 'var(--faint)' }; })()">
              <Icon v-if="r.locked || perms[r.key]?.[row[0]]" name="check" :size="16" style="stroke-width: 3" />
            </button>
          </div>
        </template>
        <div style="padding: 16px 20px; display: flex; justify-content: flex-end; gap: 10px">
          <button class="btn" type="button" :disabled="!dirty" @click="perms = JSON.parse(JSON.stringify(saved))">Сбросить</button>
          <button class="btn primary" type="button" :disabled="!dirty" @click="savePerms">Сохранить права</button>
        </div>
      </div>
    </div>
  </div>

  <Modal v-if="modal === 'invite'" @close="modal = null">
    <template #title>Пригласить пользователя</template>
    <form class="form" @submit.prevent="sendInvite">
      <label class="field light">Имя<input v-model.trim="invite.full_name" type="text" placeholder="Охранник смены 1" /></label>
      <div class="grid2">
        <label class="field light">Email (логин)<input v-model.trim="invite.email" type="email" required /></label>
        <label class="field light">Телефон<input v-model.trim="invite.phone" type="tel" placeholder="+7 900 000-00-00" /></label>
      </div>
      <label class="field light">Роль<select v-model="invite.role" class="input sm">
        <option v-for="r in roles.filter((x) => !x.locked || me.tenant.role === 'owner')" :key="r.key" :value="r.key">{{ r.name }}</option></select></label>
      <label class="field light">Временный пароль<input v-model="invite.password" type="text" minlength="8" required class="mono" /></label>
      <span class="hint">Передайте логин и пароль сотруднику — он сможет войти по email или телефону и сменить пароль.</span>
      <button class="btn primary" type="submit">Добавить</button>
    </form>
  </Modal>

  <Modal v-if="modal === 'role'" @close="modal = null">
    <template #title>Новая роль</template>
    <form class="form" @submit.prevent="createRole">
      <label class="field light">Название<input v-model.trim="roleForm.name" type="text" placeholder="Кассир" required /></label>
      <label class="field light">Цвет<input v-model="roleForm.color" type="color" style="height: 44px; padding: 4px" /></label>
      <button class="btn primary" type="submit">Создать</button>
    </form>
  </Modal>

  <Modal v-if="modal && modal.mode === 'changeRole'" @close="modal = null">
    <template #title>Роль: {{ modal.user.full_name || modal.user.email }}</template>
    <div class="form">
      <button v-for="r in roles.filter((x) => !x.locked || me.tenant.role === 'owner')" :key="r.key" class="btn" type="button"
              :style="{ justifyContent: 'space-between', borderColor: modal.user.role === r.key ? r.color : undefined }" @click="changeRole(modal.user, r.key)">
        <span :style="{ color: r.color }">{{ r.name }}</span><span class="muted" style="font-size: 12px">{{ onCount(r) }} прав</span></button>
    </div>
  </Modal>
</template>

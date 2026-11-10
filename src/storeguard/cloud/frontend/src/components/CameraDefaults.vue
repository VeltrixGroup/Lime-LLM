<script setup>
import { onMounted, reactive, ref } from "vue";
import { api } from "../api";
import { setStatus } from "../store";

const d = reactive({ username: "", password: "", port: 554, stream_path: "/Streaming/Channels/101" });
const passwordSet = ref(false);

async function load() {
  try {
    const cfg = await api("/api/settings/camera-defaults");
    Object.assign(d, { username: cfg.username, password: "", port: cfg.port, stream_path: cfg.stream_path });
    passwordSet.value = cfg.password_set;
  } catch (err) {
    setStatus(err.message, true);
  }
}
async function save() {
  try {
    await api("/api/settings/camera-defaults", { method: "PUT", body: { ...d, port: Number(d.port) || 554 } });
    setStatus("Доступ к камерам сохранён");
    load();
  } catch (err) {
    setStatus(err.message, true);
  }
}
onMounted(load);
</script>

<template>
  <section class="card">
    <div class="card-head"><strong class="card-title">Доступ к камерам</strong>
      <span class="muted" style="font-size: 13px">{{ passwordSet ? "пароль сохранён" : "пароль не задан" }}</span></div>
    <span class="hint">Один раз для всех камер — тогда новую камеру можно добавить по одному IP-адресу.</span>
    <form class="grid2" @submit.prevent="save">
      <label class="field light">Логин<input v-model.trim="d.username" type="text" autocomplete="off" /></label>
      <label class="field light">Пароль<input v-model="d.password" type="password" :placeholder="passwordSet ? 'не менять' : ''" autocomplete="new-password" /></label>
      <label class="field light">Порт RTSP<input v-model.number="d.port" type="number" min="1" max="65535" /></label>
      <label class="field light">Путь потока<input v-model.trim="d.stream_path" type="text" /></label>
      <div><button class="btn primary" type="submit">Сохранить</button></div>
    </form>
  </section>
</template>

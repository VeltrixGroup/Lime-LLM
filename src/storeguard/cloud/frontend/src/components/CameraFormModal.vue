<script setup>
import { computed, reactive, ref } from "vue";
import { api } from "../api";
import { setStatus } from "../store";
import Modal from "./Modal.vue";

const props = defineProps({ camera: Object, sessionId: String });
const emit = defineEmits(["close", "saved"]);

const isNew = !props.camera;
const form = reactive({
  name: props.camera?.name || "",
  mode: "ip",
  ip: "",
  source: props.camera?.source || "",
  process_every: props.camera?.process_every || 1,
  enabled: props.camera ? props.camera.enabled : true,
});
if (!isNew) form.mode = "url";

// Zone roles go by the name's first word (what the detector understands).
const KINDS = [
  { prefix: "checkout", label: "Касса", color: "#B6F24A", hint: "где покупатель оплачивает" },
  { prefix: "shelf", label: "Полка", color: "#8AB4FF", hint: "товар — для воронки и краж" },
  { prefix: "exit", label: "Выход", color: "#FF8F5A", hint: "вынос без оплаты" },
  { prefix: "staff", label: "Служебная зона", color: "#FFC53D", hint: "склад, за кассой — новые лица сотрудников" },
];
const kindOf = (name) => KINDS.find((k) => name.toLowerCase().startsWith(k.prefix)) || { label: "Зона", color: "#B9C2B2" };
const zones = ref((props.camera?.zones || []).map((z) => ({ name: z.name, points: z.points.map((p) => [...p]) })));
const active = ref(zones.value.length ? 0 : -1);
const tab = ref(isNew ? "main" : "main");
const testing = ref(false);
const busy = ref(false);
const snapshot = props.sessionId ? `/live/api/session/${props.sessionId}/snapshot.jpg?t=${Date.now()}` : null;

function addZone(kind) {
  const n = zones.value.filter((z) => z.name.startsWith(kind.prefix)).length + 1;
  zones.value.push({ name: `${kind.prefix} ${kind.label} ${n}`, points: [] });
  active.value = zones.value.length - 1;
}
function clickCanvas(e) {
  if (active.value < 0) return;
  const r = e.currentTarget.getBoundingClientRect();
  const x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  const y = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
  zones.value[active.value].points.push([Number(x.toFixed(4)), Number(y.toFixed(4))]);
}
const polys = computed(() => zones.value.map((z, i) => ({
  i, color: kindOf(z.name).color, pts: z.points.map((p) => `${p[0] * 100},${p[1] * 100}`).join(" "), z,
})));

async function save() {
  const bad = zones.value.find((z) => z.points.length < 3);
  if (bad) return setStatus(`В зоне «${bad.name}» нужно минимум 3 точки`, true);
  busy.value = true;
  try {
    const base = { name: form.name, process_every: Number(form.process_every) || 1, enabled: form.enabled };
    let cam;
    if (isNew) {
      const body = { ...base, zones: zones.value };
      if (form.mode === "ip") body.ip = form.ip;
      else body.source = form.source;
      cam = await api("/api/cameras", { method: "POST", body });
    } else {
      cam = await api(`/api/cameras/${props.camera.id}`, { method: "PATCH", body: { ...base, source: form.source } });
      await api(`/api/cameras/${props.camera.id}/zones`, { method: "PUT", body: { zones: zones.value } });
    }
    setStatus(isNew ? "Камера добавлена" : "Камера сохранена");
    emit("saved", cam);
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    busy.value = false;
  }
}

async function test() {
  testing.value = true;
  try {
    const res = await api(`/api/cameras/${props.camera.id}/test`, { method: "POST" });
    setStatus(res.ok ? "Поток открывается" : "Не удалось подключиться — проверьте IP, логин и пароль", !res.ok);
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    testing.value = false;
  }
}

async function remove() {
  if (!window.confirm(`Удалить камеру «${props.camera.name}»? История инцидентов сохранится.`)) return;
  try {
    await api(`/api/cameras/${props.camera.id}`, { method: "DELETE" });
    setStatus("Камера удалена");
    emit("saved");
  } catch (err) {
    setStatus(err.message, true);
  }
}
</script>

<template>
  <Modal wide @close="$emit('close')">
    <template #title>{{ isNew ? "Новая камера" : camera.name }}</template>
    <div class="seg">
      <button type="button" :class="{ on: tab === 'main' }" @click="tab = 'main'">Подключение</button>
      <button type="button" :class="{ on: tab === 'zones' }" @click="tab = 'zones'">Зоны · {{ zones.length }}</button>
    </div>

    <form v-if="tab === 'main'" class="form" @submit.prevent="save">
      <label class="field light">Название<input v-model.trim="form.name" type="text" maxlength="120" placeholder="Например, Касса 1–2" /></label>
      <div v-if="isNew" class="seg">
        <button type="button" :class="{ on: form.mode === 'ip' }" @click="form.mode = 'ip'">По IP-адресу</button>
        <button type="button" :class="{ on: form.mode === 'url' }" @click="form.mode = 'url'">RTSP-ссылка</button>
      </div>
      <label v-if="form.mode === 'ip'" class="field light">IP-адрес камеры
        <input v-model.trim="form.ip" type="text" placeholder="192.168.1.64" required />
        <span class="hint">Логин, пароль и путь потока берутся из «Доступ к камерам».</span></label>
      <label v-else class="field light">RTSP-ссылка
        <input v-model.trim="form.source" type="text" placeholder="rtsp://user:pass@192.168.1.64:554/Streaming/Channels/101" required /></label>
      <div class="grid2">
        <label class="field light">Анализировать каждый N-й кадр
          <input v-model.number="form.process_every" type="number" min="1" max="100" /></label>
        <label style="display: flex; align-items: center; gap: 10px; margin-top: 22px"><input v-model="form.enabled" type="checkbox" class="big" />Камера включена</label>
      </div>
      <div class="actions" style="justify-content: space-between">
        <div class="actions">
          <button v-if="!isNew" class="btn sm" type="button" :disabled="testing" @click="test">{{ testing ? "Проверяем…" : "Проверить поток" }}</button>
          <button v-if="!isNew" class="btn sm danger-ghost" type="button" @click="remove">Удалить</button>
        </div>
        <button class="btn primary" type="submit" :disabled="busy">Сохранить</button>
      </div>
    </form>

    <div v-else class="form">
      <span class="hint">Выберите зону и кликайте по кадру, чтобы поставить углы. Кассы, полки и выход нужны для воронки и краж; служебные зоны — чтобы система находила новых сотрудников.</span>
      <div class="actions">
        <button v-for="k in KINDS" :key="k.prefix" class="btn sm" type="button" :title="k.hint" @click="addZone(k)">
          <span class="dot" :style="{ background: k.color }"></span>+ {{ k.label }}</button>
      </div>
      <div class="video" style="aspect-ratio: 16 / 9; cursor: crosshair; border-radius: 12px" @click="clickCanvas">
        <img v-if="snapshot" class="frame" :src="snapshot" alt="" style="object-fit: fill" />
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" style="position: absolute; inset: 0; width: 100%; height: 100%">
          <polygon v-for="p in polys" :key="p.i" :points="p.pts" :fill="p.color + '33'" :stroke="p.color"
                   :stroke-width="p.i === active ? 0.6 : 0.3" vector-effect="non-scaling-stroke" />
          <template v-for="p in polys" :key="'c' + p.i">
            <circle v-for="(pt, j) in p.z.points" :key="j" :cx="pt[0] * 100" :cy="pt[1] * 100" r="0.7" :fill="p.color" />
          </template>
        </svg>
        <span v-if="!snapshot" class="faint" style="position: absolute; left: 12px; bottom: 10px; font-size: 12px">Кадр появится, когда камера будет в сети</span>
      </div>
      <div v-for="(z, i) in zones" :key="i" style="display: flex; gap: 8px; align-items: center">
        <button type="button" class="btn sm" :style="{ borderColor: i === active ? kindOf(z.name).color : undefined }" @click.stop="active = i">
          <span class="dot" :style="{ background: kindOf(z.name).color }"></span>{{ kindOf(z.name).label }}</button>
        <input v-model="z.name" class="input sm" style="flex: 1" />
        <span class="mono muted" style="font-size: 12px; width: 70px">{{ z.points.length }} точ.</span>
        <button class="btn sm" type="button" @click="z.points.pop()" :disabled="!z.points.length">↶</button>
        <button class="btn sm danger-ghost" type="button" @click="zones.splice(i, 1); active = zones.length - 1">×</button>
      </div>
      <div class="actions" style="justify-content: flex-end">
        <button class="btn primary" type="button" :disabled="busy" @click="save">Сохранить</button>
      </div>
    </div>
  </Modal>
</template>

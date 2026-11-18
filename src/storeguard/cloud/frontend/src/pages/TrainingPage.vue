<script setup>
// "Обучение ИИ": confirm / fix / reject the person boxes on frames the
// cameras found hard (re-training step 3). Frames live on the store PC and
// are reached through /live/api/hard-frames.
import { computed, onMounted, onUnmounted, ref } from "vue";
import { api } from "../api";
import { setStatus } from "../store";

const BASE = "/live/api/hard-frames";
const REASONS = { uncertain: "модель не уверена", id_switch: "потерял человека", crowd: "толпа" };
const TABS = [
  { key: "todo", label: "К проверке" },
  { key: "approved", label: "Проверено" },
  { key: "rejected", label: "Отклонено" },
];

const tab = ref("todo");
const counts = ref({ todo: 0, approved: 0, rejected: 0 });
const items = ref([]);
const loadError = ref("");
const loading = ref(true);
const frame = ref(null); // { id, width, height, boxes, live, teacher, ... }
const boxes = ref([]); // [[x1, y1, x2, y2]] normalised, being edited
const selected = ref(-1);
const showLive = ref(false);
const busy = ref(false);
const stage = ref(null);
let drag = null;

const imgUrl = computed(() => (frame.value ? `${BASE}/image?id=${encodeURIComponent(frame.value.id)}` : ""));
const ar = computed(() => (frame.value ? frame.value.width / frame.value.height : 16 / 9));
const position = computed(() => {
  const i = items.value.findIndex((f) => f.id === frame.value?.id);
  return i < 0 ? "" : `${i + 1} из ${items.value.length}`;
});

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

async function loadList(openFirst = true) {
  loading.value = true;
  try {
    const res = await api(`${BASE}?status=${tab.value}&limit=200`);
    counts.value = res.counts;
    items.value = res.items;
    loadError.value = "";
    if (openFirst) await open(items.value[0]?.id);
  } catch (err) {
    loadError.value = /502|isn't running|не запущ/i.test(err.message)
      ? "Детекция не запущена на ПК магазина — кадры хранятся там."
      : err.message;
    items.value = [];
    frame.value = null;
  } finally {
    loading.value = false;
  }
}

async function open(id) {
  selected.value = -1;
  if (!id) {
    frame.value = null;
    boxes.value = [];
    return;
  }
  try {
    frame.value = await api(`${BASE}/frame?id=${encodeURIComponent(id)}`);
    boxes.value = frame.value.boxes.map((b) => [...b]);
  } catch (err) {
    setStatus(err.message, true);
  }
}

function switchTab(key) {
  tab.value = key;
  loadList();
}

async function decide(status) {
  if (!frame.value || busy.value) return;
  busy.value = true;
  const id = frame.value.id;
  try {
    await api(`${BASE}/review`, { method: "POST", body: { id, status, boxes: status === "approved" ? boxes.value : [] } });
    setStatus(status === "approved" ? "Сохранено" : "Кадр отклонён");
    const i = items.value.findIndex((f) => f.id === id);
    await loadList(false);
    // On "К проверке" the frame just left the list: the next one took its place.
    if (tab.value === "todo") await open(items.value[Math.min(Math.max(i, 0), items.value.length - 1)]?.id);
    else await open(id);
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    busy.value = false;
  }
}

function skip() {
  const i = items.value.findIndex((f) => f.id === frame.value?.id);
  const next = items.value[(i + 1) % Math.max(1, items.value.length)];
  if (next) open(next.id);
}

function reset() {
  boxes.value = frame.value.boxes.map((b) => [...b]);
  selected.value = -1;
}

function removeSelected() {
  if (selected.value < 0) return;
  boxes.value.splice(selected.value, 1);
  selected.value = -1;
}

// ---- drawing / moving / resizing boxes (normalised coordinates) ----
function point(e) {
  const r = stage.value.getBoundingClientRect();
  return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
}
function startNew(e) {
  if (e.button !== 0) return;
  const [x, y] = point(e);
  boxes.value.push([x, y, x, y]);
  selected.value = boxes.value.length - 1;
  drag = { mode: "new", i: selected.value };
  capture(e);
}
function startMove(e, i) {
  if (e.button !== 0) return;
  selected.value = i;
  drag = { mode: "move", i, from: point(e), orig: [...boxes.value[i]] };
  capture(e);
}
function startResize(e, i, corner) {
  if (e.button !== 0) return;
  selected.value = i;
  drag = { mode: "resize", i, corner };
  capture(e);
}
function capture(e) {
  e.preventDefault();
  e.stopPropagation();
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp, { once: true });
}
function onMove(e) {
  if (!drag) return;
  const [x, y] = point(e);
  const b = boxes.value[drag.i];
  if (drag.mode === "new") {
    b[2] = x;
    b[3] = y;
  } else if (drag.mode === "move") {
    const [ox1, oy1, ox2, oy2] = drag.orig;
    const dx = Math.min(1 - Math.max(ox1, ox2), Math.max(-Math.min(ox1, ox2), x - drag.from[0]));
    const dy = Math.min(1 - Math.max(oy1, oy2), Math.max(-Math.min(oy1, oy2), y - drag.from[1]));
    boxes.value[drag.i] = [ox1 + dx, oy1 + dy, ox2 + dx, oy2 + dy];
  } else {
    if (drag.corner.includes("l")) b[0] = x; else b[2] = x;
    if (drag.corner.includes("t")) b[1] = y; else b[3] = y;
  }
}
function onUp() {
  window.removeEventListener("pointermove", onMove);
  if (drag) {
    const b = boxes.value[drag.i];
    if (b) {
      const n = [Math.min(b[0], b[2]), Math.min(b[1], b[3]), Math.max(b[0], b[2]), Math.max(b[1], b[3])];
      // A click without a drag is not a person.
      if ((n[2] - n[0]) * frame.value.width < 6 || (n[3] - n[1]) * frame.value.height < 6) {
        boxes.value.splice(drag.i, 1);
        selected.value = -1;
      } else {
        boxes.value[drag.i] = n;
      }
    }
  }
  drag = null;
}
function style(b) {
  const x1 = Math.min(b[0], b[2]), y1 = Math.min(b[1], b[3]);
  return { left: x1 * 100 + "%", top: y1 * 100 + "%", width: Math.abs(b[2] - b[0]) * 100 + "%", height: Math.abs(b[3] - b[1]) * 100 + "%" };
}

function onKey(e) {
  if (!frame.value || e.target.closest?.("input, textarea")) return;
  if (e.key === "Enter") { e.preventDefault(); decide("approved"); }
  else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeSelected(); }
  else if (e.key === "x" || e.key === "ч") decide("rejected");
  else if (e.key === "s" || e.key === "ы" || e.key === "ArrowRight") skip();
  else if (e.key === "l" || e.key === "д") showLive.value = !showLive.value;
  else if (e.key === "Escape") selected.value = -1;
}

onMounted(() => {
  window.addEventListener("keydown", onKey);
  loadList();
});
onUnmounted(() => {
  window.removeEventListener("keydown", onKey);
  window.removeEventListener("pointermove", onMove);
});
</script>

<template>
  <div class="page-head">
    <div style="display: flex; flex-direction: column; gap: 2px">
      <h1>Обучение ИИ</h1>
      <span class="sub">Кадры, на которых модель сомневалась. Поправьте рамки людей — по проверенным кадрам ИИ доучится на ваших камерах.</span>
    </div>
    <div class="seg">
      <button v-for="t in TABS" :key="t.key" type="button" :class="{ on: tab === t.key }" @click="switchTab(t.key)">
        {{ t.label }} · {{ counts[t.key] || 0 }}</button>
    </div>
  </div>

  <div v-if="loadError" class="card empty">{{ loadError }}</div>
  <div v-else-if="!loading && !items.length" class="card empty">
    <template v-if="tab === 'todo'">Всё проверено. Новые кадры появятся, когда камеры их сохранят (до 200 в день на камеру).</template>
    <template v-else>Здесь пока пусто.</template>
  </div>

  <div v-else-if="frame" class="row" style="gap: 20px; align-items: flex-start">
    <div style="flex: 3 1 560px; min-width: 0; display: flex; flex-direction: column; gap: 12px">
      <div class="video staged review-view" style="height: min(62vh, 620px)">
        <div ref="stage" class="stage" :style="{ '--ar': ar }" @pointerdown="startNew">
          <img class="frame" :src="imgUrl" alt="Кадр с камеры" draggable="false" />
          <template v-if="showLive">
            <div v-for="(b, i) in frame.live" :key="'l' + i" class="rbox live" :style="style(b)"></div>
          </template>
          <div v-for="(b, i) in boxes" :key="i" class="rbox" :class="{ sel: i === selected }" :style="style(b)"
               @pointerdown="startMove($event, i)">
            <template v-if="i === selected">
              <span v-for="c in ['tl', 'tr', 'bl', 'br']" :key="c" class="handle" :class="c" @pointerdown="startResize($event, i, c)"></span>
            </template>
          </div>
        </div>
      </div>
      <div class="actions" style="justify-content: space-between; flex-wrap: wrap; gap: 10px">
        <div class="actions" style="gap: 8px; flex-wrap: wrap">
          <button class="btn sm" type="button" :disabled="selected < 0" @click="removeSelected">Удалить рамку <span class="faint mono">Del</span></button>
          <button class="btn sm" type="button" @click="reset">Сбросить</button>
          <button class="btn sm" type="button" :class="{ on: showLive }" @click="showLive = !showLive">
            {{ showLive ? "Скрыть" : "Показать" }} рамки live-модели <span class="faint mono">L</span></button>
        </div>
        <div class="actions" style="gap: 8px">
          <button class="btn sm" type="button" @click="skip">Пропустить <span class="faint mono">S</span></button>
          <button class="btn sm danger-ghost" type="button" :disabled="busy" @click="decide('rejected')">Плохой кадр <span class="mono">X</span></button>
          <button class="btn primary" type="button" :disabled="busy" @click="decide('approved')">Верно · {{ boxes.length }} чел. <span class="mono">Enter</span></button>
        </div>
      </div>
      <span class="hint">Протяните мышью по кадру — новая рамка. Клик по рамке — выбрать, тянуть — сдвинуть, за углы — изменить размер. Каждый человек целиком в своей рамке, даже если частично закрыт; манекены и отражения — без рамки.</span>
    </div>

    <div style="flex: 1 1 280px; min-width: 0; display: flex; flex-direction: column; gap: 14px">
      <section class="card" style="gap: 10px">
        <div class="card-head"><strong class="card-title">Кадр</strong><span class="muted mono" style="font-size: 12px">{{ position }}</span></div>
        <div class="kv"><span class="muted">Камера</span><span class="mono">{{ frame.camera }}</span></div>
        <div class="kv"><span class="muted">Время</span><span class="mono">{{ fmtTime(frame.time) }}</span></div>
        <div class="kv"><span class="muted">Почему</span>
          <span style="display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end">
            <span v-for="r in frame.reasons" :key="r" class="pill off">{{ REASONS[r] || r }}</span>
            <span v-if="frame.night" class="pill off">ночь</span>
          </span></div>
        <div class="kv"><span class="muted">Рамки от</span><span>{{ frame.status !== 'todo' ? 'вашей проверки' : frame.teacher_model ? `большой модели (${frame.teacher_model})` : 'live-модели' }}</span></div>
        <div v-if="frame.teacher_model" class="kv"><span class="muted">Расхождение моделей</span>
          <span class="mono" :style="{ color: frame.score >= 0.5 ? 'var(--alert)' : frame.score > 0 ? 'var(--warn)' : 'var(--accent)' }">{{ Math.round(frame.score * 100) }}%</span></div>
      </section>

      <section class="card flush">
        <div class="card-head" style="padding: 16px 18px 10px"><strong class="card-title">Очередь</strong></div>
        <div class="queue">
          <button v-for="f in items" :key="f.id" type="button" class="qitem" :class="{ on: f.id === frame.id }" @click="open(f.id)">
            <span class="mono" style="font-size: 12px">{{ fmtTime(f.time) }}</span>
            <span class="muted" style="font-size: 12px">{{ f.camera }} · {{ f.people }} чел.{{ f.night ? " · ночь" : "" }}</span>
            <span v-if="f.teacher" class="mono" style="font-size: 12px; margin-left: auto" :style="{ color: f.score >= 0.5 ? 'var(--alert)' : 'var(--muted)' }">{{ Math.round(f.score * 100) }}%</span>
          </button>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.review-view { user-select: none; touch-action: none; }
.stage { cursor: crosshair; }
.rbox { position: absolute; border: 2px solid var(--accent); border-radius: 2px; background: #b6f24a14; cursor: move; }
.rbox.sel { border-color: #fff; background: #ffffff1f; box-shadow: 0 0 0 1px #000a; }
.rbox.live { border: 1.5px dashed var(--visitor); background: transparent; pointer-events: none; }
.handle { position: absolute; width: 12px; height: 12px; background: #fff; border: 1px solid #000; border-radius: 2px; }
.handle.tl { left: -7px; top: -7px; cursor: nwse-resize; }
.handle.br { right: -7px; bottom: -7px; cursor: nwse-resize; }
.handle.tr { right: -7px; top: -7px; cursor: nesw-resize; }
.handle.bl { left: -7px; bottom: -7px; cursor: nesw-resize; }
.kv { display: flex; justify-content: space-between; gap: 12px; font-size: 14px; align-items: center; }
.queue { max-height: 360px; overflow-y: auto; padding: 0 8px 8px; display: flex; flex-direction: column; gap: 2px; }
.qitem { display: flex; gap: 10px; align-items: center; padding: 9px 10px; border-radius: 9px; border: 0; background: transparent; color: var(--text-2); cursor: pointer; text-align: left; flex-wrap: wrap; }
.qitem:hover { background: var(--bg-active); }
.qitem.on { background: var(--bg-accent-soft); color: var(--text); }
.btn.on { background: var(--bg-active); color: var(--text); }
</style>

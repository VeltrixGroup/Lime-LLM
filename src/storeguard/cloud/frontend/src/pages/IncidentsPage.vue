<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { can, CATEGORY_COLOR, setStatus, STATUS } from "../store";
import Icon from "../components/Icon.vue";

const route = useRoute();
const router = useRouter();
const range = ref(route.query.id ? "month" : "today");
const dateFrom = ref("");
const dateTo = ref("");
const showDates = ref(false);
const filter = ref("all");
const data = ref(null);
const selectedId = ref(route.query.id || null);
const detail = ref(null);
const playing = ref(false);
const videoError = ref(false);

const CHIPS = ["all", "theft", "cash", "refund", "phone", "idle"];
const RANGE_LABEL = { today: "Сегодня", week: "Неделя", month: "Месяц", custom: "Период" };

async function load() {
  const q = new URLSearchParams({ range: range.value, category: filter.value });
  if (range.value === "custom") {
    q.set("date_from", dateFrom.value);
    q.set("date_to", dateTo.value || dateFrom.value);
  }
  try {
    data.value = await api(`/api/incidents?${q}`);
    if (!selectedId.value || !data.value.items.some((i) => i.id === selectedId.value)) {
      selectedId.value = route.query.id && !data.value.items.length ? route.query.id : data.value.items[0]?.id || null;
    }
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function loadDetail() {
  playing.value = false;
  videoError.value = false;
  detail.value = null;
  const id = selectedId.value;
  if (!id) return;
  try {
    const d = await api(`/api/incidents/${id}`);
    if (id === selectedId.value) detail.value = d; // ignore a stale, slower answer
  } catch (err) {
    if (id === selectedId.value) setStatus(err.message, true);
  }
}

async function review(status) {
  try {
    const before = detail.value.status;
    // The incident whose clip is on screen, not whatever was clicked last.
    const out = await api(`/api/incidents/${detail.value.id}`, { method: "PATCH", body: { status } });
    if (detail.value && detail.value.id === out.id) detail.value = { ...detail.value, ...out };
    const it = data.value.items.find((i) => i.id === out.id);
    if (it) it.status = out.status;
    // new_count covers the whole range, not just the filtered list.
    if (before === "new" && out.status !== "new") data.value.new_count = Math.max(0, data.value.new_count - 1);
    else if (before !== "new" && out.status === "new") data.value.new_count += 1;
    setStatus(status === "confirmed" ? "Нарушение подтверждено" : "Отмечено как ложная тревога");
  } catch (err) {
    setStatus(err.message, true);
  }
}

function pickRange(r) {
  if (r === "custom") {
    showDates.value = true;
    if (!dateFrom.value) {
      const d = new Date(); // local date, not UTC
      dateFrom.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    }
  }
  range.value = r;
}

watch([range, filter], load);
watch([dateFrom, dateTo], () => range.value === "custom" && load());
watch(selectedId, (id) => {
  router.replace({ query: id ? { id } : {} });
  loadDetail();
});
onMounted(async () => {
  await load();
  loadDetail();
});

const subtitle = computed(() => data.value ? `${RANGE_LABEL[range.value]} · новых: ${data.value.new_count} · всего: ${data.value.total}` : "");
const tag = computed(() => {
  const d = detail.value;
  if (!d) return "";
  if (d.duration_sec) {
    const m = Math.floor(d.duration_sec / 60), s = Math.round(d.duration_sec % 60);
    return `${d.category === "phone" ? "телефон" : "бездействие"} ${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return d.message.toLowerCase();
});
</script>

<template>
  <div class="page-head">
    <div style="display: flex; flex-direction: column; gap: 2px">
      <h1>Инциденты</h1>
      <span class="sub">{{ subtitle }}</span>
    </div>
    <div class="seg">
      <button type="button" :class="{ on: range === 'today' }" @click="pickRange('today')">Сегодня</button>
      <button type="button" :class="{ on: range === 'week' }" @click="pickRange('week')">Неделя</button>
      <button type="button" :class="{ on: range === 'month' }" @click="pickRange('month')">Месяц</button>
      <button type="button" :class="{ on: range === 'custom' }" @click="pickRange('custom')">Выбрать даты</button>
    </div>
  </div>
  <div v-if="range === 'custom' && showDates" class="actions">
    <label class="field light" style="flex-direction: row; align-items: center">с<input v-model="dateFrom" type="date" class="input sm" /></label>
    <label class="field light" style="flex-direction: row; align-items: center">по<input v-model="dateTo" type="date" class="input sm" /></label>
  </div>

  <div v-if="data" class="chips">
    <button v-for="c in CHIPS" :key="c" type="button" class="chip" :class="{ on: filter === c }" @click="filter = c">
      {{ c === "all" ? "Все" : data.labels[c] }} · {{ data.counts[c] }}</button>
  </div>

  <div v-if="data" class="row" style="gap: 20px; align-items: flex-start">
    <div style="flex: 2 1 380px; min-width: 0; display: flex; flex-direction: column; gap: 8px">
      <button v-for="it in data.items" :key="it.id" type="button" @click="selectedId = it.id"
              :style="{ width: '100%', textAlign: 'left', padding: '14px 16px', borderRadius: '12px', color: 'var(--text)', cursor: 'pointer', display: 'flex', gap: '12px', alignItems: 'center',
                        background: selectedId === it.id ? 'var(--bg-active)' : 'var(--bg-card)', border: `1px solid ${selectedId === it.id ? 'var(--accent)' : 'var(--line)'}` }">
        <span class="dot" :style="{ width: '10px', height: '10px', background: CATEGORY_COLOR[it.category] }"></span>
        <div style="flex: 1; min-width: 0; display: flex; flex-direction: column; line-height: 1.35">
          <strong style="font-size: 15px">{{ it.message || it.kind }}</strong>
          <span class="muted" style="font-size: 13px">{{ range === "today" ? it.time : `${it.date} ${it.time}` }} · {{ it.camera_name || "—" }} · {{ it.who }}</span>
        </div>
        <span class="pill" :style="{ background: (STATUS[it.status] || STATUS.new).bg, color: (STATUS[it.status] || STATUS.new).fg }">{{ (STATUS[it.status] || STATUS.new).label }}</span>
      </button>
      <div v-if="!data.items.length" class="card empty">Инцидентов нет</div>
    </div>

    <div v-if="detail" class="card" style="flex: 3 1 480px; padding: 22px; gap: 16px; border-radius: var(--r-xl)">
      <div class="video" style="height: 320px; border-radius: 12px">
        <video v-if="playing && detail.has_clip && !videoError" :src="`/api/events/${detail.id}/clip`" controls autoplay
               style="position: absolute; inset: 0; width: 100%; height: 100%; background: #000" @error="videoError = true"></video>
        <div v-else-if="videoError" style="position: absolute; inset: 0; display: flex; flex-direction: column; gap: 10px; align-items: center; justify-content: center; text-align: center; padding: 16px" class="text-2">
          Браузер не может воспроизвести этот клип (старый видеокодек).
          <a v-if="can('download')" class="btn sm" :href="`/api/incidents/${detail.id}/download`">Скачать и открыть в плеере</a></div>
        <template v-else>
          <div class="box" style="left: 40%; top: 18%; width: 18%; height: 66%" :style="{ borderColor: CATEGORY_COLOR[detail.category] }">
            <span class="tag" :style="{ background: CATEGORY_COLOR[detail.category] }">{{ tag }}</span></div>
          <div style="position: absolute; inset: 0; display: flex; align-items: center; justify-content: center">
            <button v-if="detail.has_clip" type="button" aria-label="Воспроизвести клип" @click="playing = true"
                    style="width: 64px; height: 64px; border-radius: 50%; border: none; background: #eef2eae6; display: flex; align-items: center; justify-content: center; cursor: pointer; color: var(--bg)">
              <Icon name="play" :size="26" /></button>
            <span v-else class="faint">Клип не записан</span>
          </div>
        </template>
      </div>

      <div class="card-head" style="align-items: flex-start">
        <div style="display: flex; flex-direction: column; gap: 4px">
          <h2 style="font-size: 22px; font-weight: 800">{{ detail.message || detail.kind }}</h2>
          <span class="muted" style="font-size: 14px">Инцидент #{{ data.items.find((i) => i.id === detail.id)?.num || detail.id.slice(0, 6) }}</span>
        </div>
        <span class="pill" style="padding: 5px 12px; font-size: 13px" :style="{ background: (STATUS[detail.status] || STATUS.new).bg, color: (STATUS[detail.status] || STATUS.new).fg }">{{ (STATUS[detail.status] || STATUS.new).label }}</span>
      </div>

      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px">
        <div style="padding: 12px; border-radius: 10px; background: var(--bg-sunk); display: flex; flex-direction: column"><span class="muted" style="font-size: 12px">Время</span><strong class="mono">{{ detail.date }} {{ detail.time }}</strong></div>
        <div style="padding: 12px; border-radius: 10px; background: var(--bg-sunk); display: flex; flex-direction: column"><span class="muted" style="font-size: 12px">Камера</span><strong>{{ detail.camera_name || "—" }}</strong></div>
        <div style="padding: 12px; border-radius: 10px; background: var(--bg-sunk); display: flex; flex-direction: column"><span class="muted" style="font-size: 12px">Кто</span><strong>{{ detail.who }}</strong></div>
        <div style="padding: 12px; border-radius: 10px; background: var(--bg-sunk); display: flex; flex-direction: column"><span class="muted" style="font-size: 12px">Уверенность ИИ</span><strong class="mono">{{ detail.score != null ? Math.round(detail.score * 100) + "%" : "—" }}</strong></div>
      </div>

      <div v-if="detail.telegram_sent_to.length" style="display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-radius: 10px; background: #0f1a20; color: #b9d9ec; font-size: 14px">
        <Icon name="send" style="color: var(--tg)" />Клип отправлен в Telegram: {{ detail.telegram_sent_to.join(", ") }}</div>

      <div class="actions">
        <button v-if="can('resolve')" class="btn danger" type="button" @click="review('confirmed')">Подтвердить нарушение</button>
        <button v-if="can('resolve')" class="btn" type="button" style="font-weight: 700" @click="review('false')">Ложная тревога</button>
        <a v-if="can('download') && detail.has_clip" class="btn" style="font-weight: 700" :href="`/api/incidents/${detail.id}/download`">Скачать клип</a>
      </div>
    </div>
  </div>
</template>

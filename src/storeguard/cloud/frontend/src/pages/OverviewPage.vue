<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { api } from "../api";
import { can, CATEGORY_COLOR, fmtChange, fmtNum, setStatus } from "../store";
import { useLive } from "../composables/useLive";
import Seg from "../components/Seg.vue";
import LiveTile from "../components/LiveTile.vue";

const range = ref("today");
const data = ref(null);
const RANGE_TITLE = { today: "Обзор за сегодня", week: "Обзор за неделю", month: "Обзор за месяц" };
const live = can("live") ? useLive() : null;

async function load() {
  try {
    data.value = await api(`/api/overview?range=${range.value}`);
  } catch (err) {
    setStatus(err.message, true);
  }
}
watch(range, load);
onMounted(() => {
  load();
  live?.init();
});

const traffic = computed(() => {
  const t = data.value?.traffic || [];
  const max = Math.max(1, ...t.map((x) => x.value));
  const top = Math.max(...t.map((x) => x.value), 0);
  return t.map((x) => ({ ...x, pct: Math.round((x.value / max) * 96), top: top > 0 && x.value === top }));
});
const trafficTicks = computed(() => {
  const t = traffic.value;
  if (!t.length) return [];
  const step = Math.max(1, Math.round(t.length / 4));
  const out = t.filter((_, i) => i % step === 0).map((x) => String(x.hour).padStart(2, "0"));
  const last = String(t[t.length - 1].hour + 1).padStart(2, "0");
  if (out[out.length - 1] !== last) out.push(last);
  return out;
});
const funnel = computed(() => {
  const f = data.value?.funnel;
  if (!f) return [];
  const base = Math.max(1, f.entered);
  return [
    ["Зашли в магазин", f.entered, "#6F8A5A"],
    ["Подошли к полкам", f.shelves, "#87A86C"],
    ["Подошли к кассе", f.checkout, "#9FC77D"],
    ["Оплатили на кассе", f.paid, "#B6F24A"],
  ].map(([n, v, c]) => ({ n, v, c, pct: Math.round((v / base) * 100) }));
});
const change = computed(() => fmtChange(data.value?.kpis.visitors_change, {
  suffix: range.value === "today" ? "к вчера" : "к прошлому периоду",
}));
const camTiles = computed(() => {
  const cams = data.value?.cameras.items || [];
  const sess = live?.sessions.value || [];
  return cams.slice(0, 4).map((c, i) => {
    const s = sess.find((x) => x.camera_id === c.id);
    return { ...c, code: `CAM ${String(i + 1).padStart(2, "0")}`, frame: s ? live.frames[s.id] : null, live: Boolean(s && !s.error) };
  });
});
const onlineCount = computed(() => {
  const c = data.value?.cameras;
  if (!c) return "";
  const liveN = camTiles.value.filter((t) => t.live).length;
  return `${Math.max(c.online, liveN)} / ${c.total} онлайн`;
});
</script>

<template>
  <div class="page-head">
    <div style="display: flex; flex-direction: column; gap: 2px">
      <h1>{{ RANGE_TITLE[range] }}</h1>
      <span class="sub">{{ data?.recent_incidents.length ? "Данные с камер вашего магазина" : "Данные появятся, когда камеры начнут работать" }}</span>
    </div>
    <Seg v-model="range" :options="[{ value: 'today', label: 'Сегодня' }, { value: 'week', label: 'Неделя' }, { value: 'month', label: 'Месяц' }]" />
  </div>

  <div v-if="data" class="kpis">
    <div class="kpi"><span class="k">Посетителей</span><strong class="v">{{ fmtNum(data.kpis.visitors) }}</strong>
      <span class="d" :style="{ color: change && change.good ? 'var(--accent)' : undefined }">{{ change ? change.text : "—" }}</span></div>
    <div class="kpi"><span class="k">Подозрений на кражу</span><strong class="v" style="color: var(--alert)">{{ data.kpis.theft }}</strong>
      <span class="d">{{ data.kpis.theft_confirmed }} подтверждено</span></div>
    <div class="kpi"><span class="k">Простоев &gt; 5 мин</span><strong class="v">{{ data.kpis.idle }}</strong>
      <span class="d">у {{ data.kpis.idle_employees }} сотрудников</span></div>
    <div class="kpi"><span class="k">Конверсия в покупку</span><strong class="v">{{ data.kpis.conversion == null ? "—" : data.kpis.conversion + "%" }}</strong>
      <span class="d">дошли до кассы</span></div>
  </div>

  <div v-if="data" class="row">
    <div class="card" style="flex: 2 1 420px; gap: 16px">
      <div class="card-head"><strong>Трафик по часам</strong>
        <span class="muted mono" style="font-size: 13px" v-if="traffic.length">{{ String(traffic[0].hour).padStart(2, "0") }}:00–{{ String(traffic[traffic.length - 1].hour + 1).padStart(2, "0") }}:00</span></div>
      <div class="bars">
        <div v-for="t in traffic" :key="t.hour" class="bar" :class="{ top: t.top }" :style="{ height: t.pct + '%' }" :title="`${t.hour}:00 — ${t.value}`"></div>
      </div>
      <div class="axis"><span v-for="h in trafficTicks" :key="h">{{ h }}</span></div>
    </div>
    <div class="card" style="flex: 1 1 300px">
      <strong>Воронка продаж</strong>
      <div v-for="f in funnel" :key="f.n" class="meter">
        <div class="lbl"><span>{{ f.n }}</span><span class="mono">{{ fmtNum(f.v) }}</span></div>
        <div class="track"><div class="fill" :style="{ width: f.pct + '%', background: f.c }"></div></div>
      </div>
    </div>
  </div>

  <div v-if="data" class="row">
    <div class="card" style="flex: 3 1 460px; gap: 6px">
      <div class="card-head" style="margin-bottom: 8px"><strong>Последние инциденты</strong>
        <router-link v-if="can('archive')" to="/incidents" style="font-size: 13px">Все инциденты</router-link></div>
      <div class="table" v-if="data.recent_incidents.length">
        <div style="min-width: 520px">
          <div class="thead" style="grid-template-columns: 70px 1fr 140px 120px; padding-left: 0; padding-right: 0">
            <span>ВРЕМЯ</span><span>СОБЫТИЕ</span><span>ГДЕ</span><span></span></div>
          <div v-for="i in data.recent_incidents" :key="i.id" class="tr" style="grid-template-columns: 70px 1fr 140px 120px; padding-left: 0; padding-right: 0">
            <span class="mono" style="font-size: 13px">{{ i.time }}</span>
            <span style="display: flex; gap: 8px; align-items: center"><span class="dot" :style="{ background: CATEGORY_COLOR[i.category] }"></span>
              {{ i.employee_id ? `${i.who}: ${i.message}` : i.message }}</span>
            <span class="text-2" style="font-size: 14px">{{ i.camera_name || "—" }}</span>
            <router-link v-if="can('archive')" :to="`/incidents?id=${i.id}`" style="font-size: 13px; font-weight: 600">Смотреть клип</router-link>
          </div>
        </div>
      </div>
      <div v-else class="empty">Нарушений за этот период нет</div>
    </div>
    <div class="card" style="flex: 2 1 320px">
      <div class="card-head"><strong>Камеры</strong><span class="mono accent" style="font-size: 13px">{{ onlineCount }}</span></div>
      <div v-if="camTiles.length" style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px">
        <router-link v-for="c in camTiles" :key="c.id" :to="can('live') ? `/cameras?cam=${c.id}` : '/overview'" style="color: inherit">
          <LiveTile :frame="c.frame" :label="`${c.code} · ${c.name}`" :online="c.live || c.online" height="110px" />
        </router-link>
      </div>
      <div v-else class="empty">Камер пока нет —
        <router-link v-if="can('cams_edit')" to="/cameras?setup=2">добавьте первую</router-link></div>
      <div class="card-head" style="padding-top: 6px; border-top: 1px solid var(--line); font-size: 13px">
        <span class="muted">Edge-сервер</span>
        <span :style="{ color: data.cameras.edge_online || live?.state.value === 'live' ? 'var(--accent)' : 'var(--muted)' }">
          {{ data.cameras.edge_online || live?.state.value === "live" ? "в сети" : "не в сети" }}{{ data.cameras.gpu != null ? ` · GPU ${Math.round(data.cameras.gpu)}%` : "" }}
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { api } from "../api";
import { can, CATEGORY_COLOR, fmtChange, fmtNum, setStatus } from "../store";
import Seg from "../components/Seg.vue";

const range = ref("week");
const data = ref(null);
const MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];

async function load() {
  try {
    data.value = await api(`/api/analytics?range=${range.value}`);
  } catch (err) {
    setStatus(err.message, true);
  }
}
watch(range, load);
onMounted(load);

const period = computed(() => {
  if (!data.value) return "";
  const local = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
  const a = local(data.value.from), b = local(data.value.to);
  if (data.value.from === data.value.to) return `${b.getDate()} ${MONTHS[b.getMonth()]}`;
  return a.getMonth() === b.getMonth()
    ? `${a.getDate()}–${b.getDate()} ${MONTHS[b.getMonth()]}`
    : `${a.getDate()} ${MONTHS[a.getMonth()]} – ${b.getDate()} ${MONTHS[b.getMonth()]}`;
});
const series = computed(() => {
  const s = data.value?.series || [];
  const max = Math.max(1, ...s.map((x) => x.value));
  return s.map((x) => ({ ...x, pct: Math.round((x.value / max) * 85), top: x.value === max && max > 0 }));
});
const funnel = computed(() => {
  const f = data.value?.funnel;
  if (!f) return [];
  const base = Math.max(1, f.entered);
  return [["Зашли в магазин", f.entered, "#6F8A5A"], ["Подошли к полкам", f.shelves, "#87A86C"],
    ["Подошли к кассе", f.checkout, "#9FC77D"], ["Оплатили на кассе", f.paid, "#B6F24A"]]
    .map(([n, v, c]) => ({ n, v, c, pct: Math.round((v / base) * 100) }));
});
const incMax = computed(() => Math.max(1, ...(data.value?.incidents_by_type || []).map((i) => i.value)));
const visitorsChange = computed(() => fmtChange(data.value?.kpis.visitors_change, { suffix: "к прошлому периоду" }));
const incChange = computed(() => fmtChange(data.value?.kpis.incidents_change, { invert: true, suffix: "к прошлому периоду" }));
const heatColor = (v) => {
  const a = Math.round(Math.min(1, v) * 255).toString(16).padStart(2, "0");
  return { background: "#1A2117", backgroundImage: `linear-gradient(#B6F24A${a},#B6F24A${a})` };
};
</script>

<template>
  <div class="page-head">
    <div style="display: flex; flex-direction: column; gap: 2px">
      <h1>Аналитика</h1>
      <span class="sub">{{ period }}</span>
    </div>
    <div class="actions">
      <Seg v-model="range" :options="[{ value: 'day', label: 'День' }, { value: 'week', label: 'Неделя' }, { value: 'month', label: 'Месяц' }]" />
      <a v-if="can('export')" class="btn" :href="`/api/analytics/export.csv?range=${range}`" download>Экспорт</a>
    </div>
  </div>

  <template v-if="data">
    <div class="kpis" style="grid-template-columns: repeat(auto-fit, minmax(190px, 1fr))">
      <div class="kpi"><span class="k">Посетителей</span><strong class="v" style="font-size: 30px">{{ fmtNum(data.kpis.visitors) }}</strong>
        <span class="d" :style="{ color: visitorsChange?.good ? 'var(--accent)' : undefined }">{{ visitorsChange?.text || "—" }}</span></div>
      <div class="kpi"><span class="k">Дошли до кассы</span><strong class="v" style="font-size: 30px">{{ data.kpis.checkout_pct == null ? "—" : data.kpis.checkout_pct + "%" }}</strong>
        <span class="d">{{ fmtNum(data.kpis.checkout_people) }} человек</span></div>
      <div class="kpi"><span class="k">Среднее время в магазине</span><strong class="v" style="font-size: 30px">{{ data.kpis.avg_minutes == null ? "—" : data.kpis.avg_minutes + " мин" }}</strong>
        <span class="d">{{ data.kpis.peak_day ? `пик — ${data.kpis.peak_day}` : "" }}</span></div>
      <div class="kpi"><span class="k">Инцидентов</span><strong class="v" style="font-size: 30px; color: var(--alert)">{{ data.kpis.incidents }}</strong>
        <span class="d" :style="{ color: incChange?.good ? 'var(--accent)' : undefined }">{{ incChange?.text || "—" }}</span></div>
    </div>

    <div class="row">
      <div class="card" style="flex: 3 1 460px; gap: 16px">
        <strong>{{ range === "day" ? "Посетители по часам" : "Посетители по дням" }}</strong>
        <div class="bars" style="height: 220px; gap: 14px" :style="{ gap: series.length > 14 ? '4px' : '14px' }">
          <div v-for="d in series" :key="d.label + d.date" style="flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; gap: 6px; height: 100%; min-width: 0">
            <span v-if="series.length <= 14" class="mono text-2" style="font-size: 12px">{{ fmtNum(d.value) }}</span>
            <div style="width: 100%; border-radius: 6px 6px 0 0; min-height: 2px" :style="{ height: d.pct + '%', background: d.top ? 'var(--accent)' : '#34402F' }" :title="`${d.label}: ${d.value}`"></div>
          </div>
        </div>
        <div style="display: flex; color: var(--muted); font-size: 12px; font-family: var(--mono)" :style="{ gap: series.length > 14 ? '4px' : '14px' }">
          <span v-for="(d, i) in series" :key="i" style="flex: 1; text-align: center; overflow: hidden">{{ series.length > 14 && i % 5 ? "" : d.label }}</span>
        </div>
      </div>
      <div class="card" style="flex: 2 1 320px">
        <strong>Воронка посетителя</strong>
        <div v-for="f in funnel" :key="f.n" class="meter">
          <div class="lbl"><span>{{ f.n }}</span><span class="mono">{{ fmtNum(f.v) }}</span></div>
          <div class="track" style="height: 12px; border-radius: 6px"><div class="fill" :style="{ width: f.pct + '%', background: f.c }"></div></div>
        </div>
        <span class="muted" style="font-size: 12px">Считается по камерам: вход, зоны полок и касса</span>
      </div>
    </div>

    <div class="row">
      <div class="card" style="flex: 3 1 460px; gap: 12px; overflow-x: auto">
        <div class="card-head"><strong>Загруженность по часам</strong>
          <span style="display: flex; align-items: center; gap: 6px; font-size: 12px" class="muted">мало<span style="width: 60px; height: 8px; border-radius: 4px; background: linear-gradient(90deg, #1a2117, #b6f24a)"></span>много</span></div>
        <div style="min-width: 520px; display: flex; flex-direction: column; gap: 4px">
          <div :style="{ display: 'grid', gridTemplateColumns: `32px repeat(${data.hours.length}, minmax(0, 1fr))`, gap: '4px' }" class="muted mono">
            <span></span><span v-for="h in data.hours" :key="h" style="text-align: center; font-size: 11px">{{ h }}</span></div>
          <div v-for="r in data.heatmap" :key="r.label" :style="{ display: 'grid', gridTemplateColumns: `32px repeat(${data.hours.length}, minmax(0, 1fr))`, gap: '4px', alignItems: 'center' }">
            <span class="muted mono" style="font-size: 12px">{{ r.label }}</span>
            <div v-for="(c, i) in r.cells" :key="i" style="height: 26px; border-radius: 4px" :style="heatColor(c)"></div>
          </div>
        </div>
      </div>
      <div class="card" style="flex: 2 1 320px">
        <strong>Инциденты по типам</strong>
        <div v-for="i in data.incidents_by_type" :key="i.category" class="meter">
          <div class="lbl"><span>{{ i.label }}</span><span class="mono">{{ i.value }}</span></div>
          <div class="track"><div class="fill" :style="{ width: Math.round((i.value / incMax) * 100) + '%', background: CATEGORY_COLOR[i.category] }"></div></div>
        </div>
      </div>
    </div>
  </template>
</template>

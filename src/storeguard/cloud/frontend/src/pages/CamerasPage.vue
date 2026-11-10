<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import { can, clock, setStatus } from "../store";
import { useLive } from "../composables/useLive";
import Icon from "../components/Icon.vue";
import LiveTile from "../components/LiveTile.vue";
import CameraFormModal from "../components/CameraFormModal.vue";
import CameraDefaults from "../components/CameraDefaults.vue";

const route = useRoute();
const router = useRouter();
const setup = computed(() => route.query.setup === "2");
const live = useLive({ autostart: true, withFeed: true });
const cameras = ref([]);
const selected = ref(route.query.cam || null);
const showAi = ref(true);
const now = ref(clock());
const editing = ref(null); // camera object | "new" | null
const restarting = ref(false);
const viewer = ref(null);
let timer = null;

async function loadCameras() {
  try {
    cameras.value = (await api("/api/cameras")).cameras || [];
  } catch (err) {
    setStatus(err.message, true);
  }
}

// Every camera of the store, with its live session (if detection runs).
const cams = computed(() =>
  cameras.value.map((c, i) => {
    const s = live.sessions.value.find((x) => x.camera_id === c.id);
    const frame = s ? live.frames[s.id] : null;
    const online = Boolean(s && !s.error && frame);
    return {
      ...c, code: `CAM ${String(i + 1).padStart(2, "0")}`, session: s, frame, online,
      rec: Boolean(frame?.meta.rec),
    };
  })
);
const sel = computed(() => cams.value.find((c) => c.id === selected.value) || cams.value[0] || null);
const onlineCount = computed(() => cams.value.filter((c) => c.online).length);
const counts = computed(() => sel.value?.frame?.meta.counts || { all: 0, staff: 0, visitors: 0 });
const statusLine = computed(() => {
  if (live.state.value === "unavailable") return "детекция не запущена на этом компьютере";
  return `${onlineCount.value} из ${cams.value.length} камер в сети · ИИ анализирует в магазине`;
});

async function restart() {
  restarting.value = true;
  try {
    await live.start();
    setStatus("Камеры перезапущены");
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    restarting.value = false;
  }
}

async function onSaved() {
  editing.value = null;
  await loadCameras();
  // Nothing to restart once no camera is enabled (the server would 400).
  if (live.state.value !== "unavailable" && cameras.value.some((c) => c.enabled)) restart();
}

function fullscreen() {
  viewer.value?.requestFullscreen?.();
}

function feedTime(ts) {
  return clock(new Date(ts * 1000));
}

let unmounted = false;
onMounted(async () => {
  timer = setInterval(() => { now.value = clock(); }, 1000);
  await loadCameras();
  if (!unmounted) await live.init();
});
onUnmounted(() => {
  unmounted = true;
  clearInterval(timer);
});
</script>

<template>
  <div v-if="setup" class="card" style="flex-direction: row; align-items: center; justify-content: space-between; flex-wrap: wrap; border-color: #3d4f2a">
    <div style="display: flex; flex-direction: column; gap: 2px">
      <span class="section-label">ШАГ 2 ИЗ 3</span>
      <strong style="font-size: 17px">Подключите IP-камеры магазина</strong>
      <span class="muted" style="font-size: 14px">Укажите IP-адрес — логин и пароль камер задаются один раз ниже, в «Доступ к камерам».</span>
    </div>
    <div class="actions">
      <button class="btn primary" type="button" @click="editing = 'new'">+ Добавить камеру</button>
      <button class="btn" type="button" @click="router.push('/settings?setup=3')">Далее — Telegram-бот</button>
    </div>
  </div>

  <div class="page-head">
    <div style="display: flex; flex-direction: column; gap: 2px">
      <h1>Камеры онлайн <span class="live-badge"><span class="dot blink" style="background: var(--alert); width: 7px; height: 7px"></span>LIVE</span></h1>
      <span class="sub mono">{{ now }} · {{ statusLine }}</span>
    </div>
    <div class="actions">
      <button class="btn" type="button" :style="showAi ? { background: 'var(--bg-accent-soft)', color: 'var(--accent)', fontWeight: 700 } : {}"
              @click="showAi = !showAi">Разметка ИИ: {{ showAi ? "вкл" : "выкл" }}</button>
      <button v-if="can('cams_edit')" class="btn" type="button" @click="editing = 'new'">+ Добавить камеру</button>
    </div>
  </div>

  <div v-if="!cameras.length" class="card empty">
    Камер пока нет. <a v-if="can('cams_edit')" href="#" @click.prevent="editing = 'new'">Добавьте первую камеру</a>
  </div>

  <div v-else class="row" style="gap: 20px; align-items: flex-start">
    <div style="flex: 3 1 560px; min-width: 0; display: flex; flex-direction: column; gap: 14px">
      <div ref="viewer" class="video" style="height: 480px">
        <img v-if="sel?.frame" class="frame" :src="sel.frame.url" alt="Видео с камеры" />
        <template v-if="sel?.frame && showAi">
          <div v-for="p in sel.frame.meta.people" :key="p.id" class="box"
               :style="{ left: p.box[0] * 100 + '%', top: p.box[1] * 100 + '%', width: p.box[2] * 100 + '%', height: p.box[3] * 100 + '%', borderColor: p.color }">
            <span class="tag" :style="{ background: p.color }">{{ p.label }}</span>
          </div>
        </template>
        <div v-if="sel && !sel.online" style="position: absolute; inset: 0; background: #0f0f0d; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; text-align: center; padding: 16px" class="text-2">
          <Icon name="nosignal" :size="40" style="color: var(--alert)" />
          <span v-if="live.state.value === 'unavailable'">Детекция не запущена. Запустите <span class="mono">make serve</span> на ПК магазина.</span>
          <span v-else-if="sel.session?.error">Нет сигнала — {{ sel.session.error }}. Проверьте питание и сеть камеры.</span>
          <span v-else-if="!sel.session && sel.enabled">Камера не подключена к детекции.</span>
          <span v-else-if="!sel.enabled">Камера выключена в настройках.</span>
          <span v-else>Подключаемся…</span>
          <button v-if="can('live') && live.state.value !== 'unavailable'" class="btn sm" type="button" :disabled="restarting" @click="restart">Перезапустить камеры</button>
        </div>
        <div class="bar-top"><span>{{ sel?.code }} · {{ sel?.name }}</span><span>{{ now }}</span></div>
        <div v-if="sel?.rec" style="position: absolute; left: 16px; bottom: 16px; display: flex; align-items: center; gap: 8px; padding: 6px 12px; border-radius: 8px; background: var(--alert); color: var(--bg); font-weight: 700; font-size: 13px">
          <span class="dot blink" style="background: var(--bg)"></span>Идёт запись клипа → Telegram</div>
      </div>

      <div class="card-head" style="gap: 12px">
        <div style="display: flex; flex-wrap: wrap; gap: 16px; font-size: 13px" class="text-2">
          <span style="display: flex; align-items: center; gap: 6px"><span style="width: 12px; height: 12px; border-radius: 3px; border: 2px solid var(--accent)"></span>Сотрудник</span>
          <span style="display: flex; align-items: center; gap: 6px"><span style="width: 12px; height: 12px; border-radius: 3px; border: 2px solid var(--visitor)"></span>Посетитель</span>
          <span style="display: flex; align-items: center; gap: 6px"><span style="width: 12px; height: 12px; border-radius: 3px; border: 2px solid var(--warn)"></span>Таймер &lt; 5 мин</span>
          <span style="display: flex; align-items: center; gap: 6px"><span style="width: 12px; height: 12px; border-radius: 3px; border: 2px solid var(--alert)"></span>Нарушение — запись</span>
        </div>
        <div class="actions">
          <a class="btn sm" :class="{ disabled: !sel?.online }" :href="sel?.online ? `/live/api/session/${sel.session.id}/clip.mp4` : undefined" download>Записать клип</a>
          <a class="btn sm" :class="{ disabled: !sel?.online }" :href="sel?.online ? `/live/api/session/${sel.session.id}/snapshot.jpg` : undefined" download>Снимок</a>
          <button class="btn sm" type="button" @click="fullscreen">На весь экран</button>
        </div>
      </div>

      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px">
        <button v-for="c in cams" :key="c.id" type="button" @click="selected = c.id"
                :style="{ padding: 0, textAlign: 'left', borderRadius: '12px', overflow: 'hidden', background: 'var(--bg-card)', color: 'var(--text)', cursor: 'pointer', border: `2px solid ${sel?.id === c.id ? 'var(--accent)' : 'var(--line)'}` }">
          <LiveTile :frame="c.frame" :online="c.online" :boxes="showAi" height="96px" style="border: none; border-radius: 0" />
          <div style="padding: 8px 10px; display: flex; justify-content: space-between; align-items: center; gap: 6px">
            <span style="font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis">{{ c.name }}</span>
            <span class="dot" :style="{ background: c.online ? (c.rec ? 'var(--alert)' : 'var(--accent)') : 'var(--faint)' }"></span>
          </div>
        </button>
      </div>
    </div>

    <div style="flex: 1 1 300px; min-width: 0; display: flex; flex-direction: column; gap: 14px">
      <div class="card" style="padding: 20px; gap: 12px">
        <strong>Сейчас в кадре</strong>
        <div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; text-align: center">
          <div style="padding: 10px 4px; border-radius: 10px; background: var(--bg-sunk); display: flex; flex-direction: column"><strong style="font-size: 24px">{{ counts.all }}</strong><span class="muted" style="font-size: 12px">людей</span></div>
          <div style="padding: 10px 4px; border-radius: 10px; background: var(--bg-sunk); display: flex; flex-direction: column"><strong style="font-size: 24px; color: var(--accent)">{{ counts.staff }}</strong><span class="muted" style="font-size: 12px">сотрудн.</span></div>
          <div style="padding: 10px 4px; border-radius: 10px; background: var(--bg-sunk); display: flex; flex-direction: column"><strong style="font-size: 24px; color: var(--visitor)">{{ counts.visitors }}</strong><span class="muted" style="font-size: 12px">посетит.</span></div>
        </div>
      </div>
      <div class="card" style="padding: 20px; gap: 4px">
        <div class="card-head" style="margin-bottom: 8px"><strong>События в реальном времени</strong>
          <router-link v-if="can('archive')" to="/incidents" style="font-size: 13px">Все</router-link></div>
        <div v-for="(e, i) in live.feed.value" :key="e.seq" style="display: flex; gap: 10px; padding: 10px; border-radius: 10px; align-items: flex-start"
             :style="{ background: i === 0 ? 'var(--bg-accent-soft)' : 'transparent' }">
          <span class="dot" :style="{ background: e.color, marginTop: '7px' }"></span>
          <div style="flex: 1; min-width: 0; display: flex; flex-direction: column; line-height: 1.35">
            <span style="font-size: 14px; font-weight: 600">{{ e.title }}</span>
            <span class="muted mono" style="font-size: 12px">{{ feedTime(e.ts) }} · {{ e.cam }}</span>
          </div>
        </div>
        <div v-if="!live.feed.value.length" class="empty" style="padding: 16px 0">Пока тихо — события появятся здесь</div>
      </div>
    </div>
  </div>

  <template v-if="can('cams_edit') && cameras.length">
    <div class="card flush">
      <div class="card-head" style="padding: 16px 20px; border-bottom: 1px solid var(--line)">
        <strong class="card-title">Настройка камер</strong>
        <button class="btn sm" type="button" :disabled="restarting || live.state.value === 'unavailable'" @click="restart">Перезапустить детекцию</button>
      </div>
      <div class="table">
        <div style="min-width: 720px">
          <div class="thead" style="grid-template-columns: 1.6fr 1.6fr 0.8fr 1fr 1fr">
            <span>КАМЕРА</span><span>ПОТОК</span><span>ЗОНЫ</span><span>СТАТУС</span><span></span></div>
          <div v-for="c in cams" :key="c.id" class="tr" style="grid-template-columns: 1.6fr 1.6fr 0.8fr 1fr 1fr">
            <span><strong>{{ c.name }}</strong> <span class="muted mono" style="font-size: 12px">{{ c.code }}</span></span>
            <span class="muted mono" style="font-size: 12px; overflow: hidden; text-overflow: ellipsis">{{ c.label }}</span>
            <span class="mono">{{ c.zones.length }}</span>
            <span :style="{ color: !c.enabled ? 'var(--muted)' : c.online ? 'var(--accent)' : 'var(--alert)' }">{{ !c.enabled ? "выключена" : c.online ? "в сети" : "нет сигнала" }}</span>
            <span style="justify-self: end"><button class="btn sm" type="button" @click="editing = c">Изменить</button></span>
          </div>
        </div>
      </div>
    </div>
    <CameraDefaults />
  </template>
  <CameraDefaults v-else-if="can('cams_edit') && setup" />

  <CameraFormModal v-if="editing" :camera="editing === 'new' ? null : editing"
                   :session-id="editing !== 'new' && editing ? cams.find((c) => c.id === editing.id)?.session?.id : null"
                   @close="editing = null" @saved="onSaved" />
</template>

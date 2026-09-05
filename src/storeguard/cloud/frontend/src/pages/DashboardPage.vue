<script setup>
import { ref, computed, onMounted, onUnmounted } from "vue";
import { api } from "../api";
import { setStatus, isOwner, fmtTime } from "../store";

const CHART_DAYS = 14;
const RECENT_COUNT = 8;
const POLL_MS = 5000;

const cameras = ref([]);
const teamCount = ref(0);
const events = ref([]);
const telegramOn = ref(null); // null = unknown/not owner, else boolean

const cameraCount = computed(() => cameras.value.length);
const enabledCount = computed(() => cameras.value.filter((c) => c.enabled).length);

const eventsLast7Days = computed(() => {
  const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
  return events.value.filter((ev) => new Date(ev.ts).getTime() >= cutoff).length;
});

const recentActivity = computed(() => events.value.slice(0, RECENT_COUNT));

const kindBreakdown = computed(() => {
  const counts = {};
  for (const ev of events.value) {
    counts[ev.kind] = (counts[ev.kind] || 0) + 1;
  }
  return Object.entries(counts)
    .map(([kind, count]) => ({ kind, count }))
    .sort((a, b) => b.count - a.count);
});

const maxKindCount = computed(() =>
  kindBreakdown.value.reduce((m, k) => Math.max(m, k.count), 0)
);

const dayBuckets = computed(() => {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  start.setDate(start.getDate() - (CHART_DAYS - 1));
  const dayMs = 24 * 60 * 60 * 1000;
  const startMs = start.getTime();

  const buckets = Array.from({ length: CHART_DAYS }, (_, i) => {
    const d = new Date(startMs + i * dayMs);
    return { date: d, count: 0 };
  });

  for (const ev of events.value) {
    const t = new Date(ev.ts).getTime();
    if (Number.isNaN(t)) continue;
    const idx = Math.floor((t - startMs) / dayMs);
    if (idx >= 0 && idx < buckets.length) buckets[idx].count++;
  }
  return buckets;
});

const maxDayCount = computed(() =>
  dayBuckets.value.reduce((m, b) => Math.max(m, b.count), 1)
);

function barHeightPct(count, max) {
  if (max <= 0) return 0;
  return Math.max(count > 0 ? 6 : 0, Math.round((count / max) * 100));
}

function dayLabel(date) {
  return date.getDate();
}

function dayTitle(bucket) {
  const d = bucket.date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
  return `${d}: ${bucket.count} event${bucket.count === 1 ? "" : "s"}`;
}

let pollTimer = null;

// Silent refresh for the poll — a dropped tick shouldn't spam an error toast;
// the next tick just tries again.
async function refreshEvents() {
  try {
    const data = await api("/api/events?limit=500");
    events.value = data.events || [];
  } catch {
    /* transient — retried on the next poll tick */
  }
}

async function load() {
  try {
    const [camData, memberData, eventData] = await Promise.all([
      api("/api/cameras"),
      api("/api/org/members"),
      api("/api/events?limit=500"),
    ]);
    cameras.value = camData.cameras || [];
    teamCount.value = (memberData.members || []).length;
    events.value = eventData.events || [];
  } catch (err) {
    setStatus(err.message, true);
  }

  if (isOwner()) {
    try {
      const tg = await api("/api/settings/telegram");
      telegramOn.value = Boolean(tg.enabled);
    } catch {
      telegramOn.value = null;
    }
  }
}

onMounted(() => {
  load();
  // "Watch real time camera events": poll for new events while this page is
  // open, so alerts pushed by the edge agent show up without a manual reload.
  pollTimer = setInterval(refreshEvents, POLL_MS);
});

onUnmounted(() => {
  clearInterval(pollTimer);
});
</script>

<template>
  <div class="page-head">
    <div>
      <h1>Dashboard</h1>
      <p class="page-sub">An overview of your cameras and recent activity.</p>
    </div>
  </div>

  <div class="stat-grid">
    <div class="stat-tile">
      <span class="stat-label">Cameras</span>
      <span class="stat-value">{{ cameraCount }}</span>
      <span class="stat-sub">{{ enabledCount }} enabled</span>
    </div>
    <div class="stat-tile">
      <span class="stat-label">Team members</span>
      <span class="stat-value">{{ teamCount }}</span>
    </div>
    <div class="stat-tile">
      <span class="stat-label">Events, last 7 days</span>
      <span class="stat-value">{{ eventsLast7Days }}</span>
    </div>
    <div class="stat-tile">
      <span class="stat-label">Telegram alerts</span>
      <span class="stat-value">
        {{ telegramOn === null ? "—" : telegramOn ? "On" : "Off" }}
      </span>
    </div>
  </div>

  <div class="dash-grid">
    <div class="card">
      <div class="section-head">
        <h2>Events, last 14 days</h2>
      </div>
      <div class="chart-wrap">
        <div v-if="events.length === 0" class="empty">No events yet.</div>
        <div v-else class="bar-chart">
          <div v-for="(b, i) in dayBuckets" :key="i" class="bar-col" :title="dayTitle(b)">
            <div
              class="bar"
              :style="{ height: barHeightPct(b.count, maxDayCount) + '%' }"
            ></div>
            <span class="bar-label">{{ dayLabel(b.date) }}</span>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="section-head">
        <h2>By kind</h2>
      </div>
      <div v-if="kindBreakdown.length === 0" class="empty">No events yet.</div>
      <div v-else class="kind-list">
        <div v-for="k in kindBreakdown" :key="k.kind" class="kind-row">
          <span class="kind-name">{{ k.kind }}</span>
          <span class="kind-bar-track">
            <span
              class="kind-bar-fill"
              :style="{ width: Math.max(4, (k.count / maxKindCount) * 100) + '%' }"
            ></span>
          </span>
          <span class="kind-count">{{ k.count }}</span>
        </div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="section-head">
      <h2>
        Recent activity
        <span class="live-dot" title="Refreshes automatically every few seconds"
          >● Live</span
        >
      </h2>
      <router-link to="/events" class="muted-link">View all →</router-link>
    </div>
    <div v-if="recentActivity.length === 0" class="empty">No events yet.</div>
    <div v-else class="activity-list">
      <div v-for="ev in recentActivity" :key="ev.id" class="activity-row">
        <span class="activity-time">{{ fmtTime(ev.ts) }}</span>
        <span class="activity-kind">{{ ev.kind }}</span>
        <span class="activity-msg">
          {{ ev.camera_name ? ev.camera_name + " · " : "" }}{{ ev.message || "" }}
        </span>
      </div>
    </div>
  </div>
</template>

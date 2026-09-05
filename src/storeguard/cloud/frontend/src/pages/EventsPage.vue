<script setup>
import { ref, onMounted } from "vue";
import { api } from "../api";
import { setStatus, fmtTime } from "../store";

const events = ref([]);
const filterPersonId = ref(null);

async function loadEvents(personId = null) {
  filterPersonId.value = personId;
  try {
    const path = personId
      ? `/api/events?person_id=${encodeURIComponent(personId)}`
      : "/api/events";
    const data = await api(path);
    events.value = data.events || [];
  } catch (err) {
    setStatus(err.message, true);
  }
}

onMounted(() => loadEvents());
</script>

<template>
  <div class="page-head">
    <h1>Events</h1>
  </div>

  <section class="card" id="events-card">
    <div class="section-head">
      <h2>
        <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h3.5l2-6 4 12 2-8 1.5 2H21"/></svg>
        Events
      </h2>
      <span class="muted">
        {{ events.length }} event{{ events.length === 1 ? "" : "s" }}
        <template v-if="filterPersonId">
          · person {{ filterPersonId.slice(0, 8) }} (Refresh to clear)
        </template>
      </span>
    </div>
    <div class="table-wrap">
      <table class="members">
        <thead>
          <tr>
            <th>Time</th>
            <th>Camera</th>
            <th>Person</th>
            <th>Kind</th>
            <th>Message</th>
            <th>Clip</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="events.length === 0">
            <td class="empty" colspan="6">No events yet.</td>
          </tr>
          <tr v-for="ev in events" :key="ev.id">
            <td>{{ fmtTime(ev.ts) }}</td>
            <td>{{ ev.camera_name || "—" }}</td>
            <td>
              <a
                v-if="ev.person_id"
                href="#"
                class="clip-link"
                :title="`Show all cameras that saw ${ev.person_id}`"
                @click.prevent="loadEvents(ev.person_id)"
              >
                {{ ev.person_id.slice(0, 8) }}
              </a>
              <template v-else>—</template>
            </td>
            <td>{{ ev.kind }}</td>
            <td>{{ ev.message || "" }}</td>
            <td>
              <a
                v-if="ev.has_clip"
                :href="`/api/events/${ev.id}/clip`"
                target="_blank"
                rel="noopener"
                class="clip-link"
              >
                view
              </a>
              <template v-else>—</template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <button class="btn ghost sm" type="button" @click="loadEvents()">Refresh</button>
  </section>
</template>

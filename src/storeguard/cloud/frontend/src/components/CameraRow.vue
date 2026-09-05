<script setup>
import { reactive, ref } from "vue";
import { api } from "../api";
import { setStatus, isOwner } from "../store";

const props = defineProps({ camera: { type: Object, required: true } });
const emit = defineEmits(["changed"]);

const showEdit = ref(false);
const showZones = ref(false);
const testing = ref(false);

const editForm = reactive({
  name: props.camera.name,
  source: props.camera.source,
  process_every: props.camera.process_every,
  enabled: props.camera.enabled,
});

const zonesJson = ref(
  JSON.stringify(
    props.camera.zones.map((z) => ({ name: z.name, points: z.points })),
    null,
    2
  )
);

function toggleEdit() {
  showZones.value = false;
  showEdit.value = !showEdit.value;
}

function toggleZones() {
  showEdit.value = false;
  showZones.value = !showZones.value;
}

async function saveEdit() {
  try {
    await api(`/api/cameras/${props.camera.id}`, {
      method: "PATCH",
      body: {
        name: editForm.name,
        source: editForm.source,
        process_every: Number(editForm.process_every) || 1,
        enabled: editForm.enabled,
      },
    });
    setStatus("Camera updated");
    showEdit.value = false;
    emit("changed");
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function saveZones() {
  let zones;
  try {
    zones = JSON.parse(zonesJson.value || "[]");
  } catch {
    setStatus("Zones must be valid JSON", true);
    return;
  }
  try {
    await api(`/api/cameras/${props.camera.id}/zones`, {
      method: "PUT",
      body: { zones },
    });
    setStatus("Zones saved");
    showZones.value = false;
    emit("changed");
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function testConnection() {
  testing.value = true;
  try {
    const res = await api(`/api/cameras/${props.camera.id}/test`, { method: "POST" });
    setStatus(
      res.ok
        ? `${props.camera.name}: stream connected`
        : `${props.camera.name}: could not connect — check credentials/port/path`,
      !res.ok
    );
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    testing.value = false;
  }
}

async function deleteCamera() {
  if (!window.confirm(`Delete camera "${props.camera.name}"?`)) return;
  try {
    await api(`/api/cameras/${props.camera.id}`, { method: "DELETE" });
    setStatus("Camera deleted");
    emit("changed");
  } catch (err) {
    setStatus(err.message, true);
  }
}
</script>

<template>
  <div class="cam">
    <div class="cam-head">
      <div class="cam-title">
        <span class="cam-name">{{ camera.name }}</span>
        <span class="cam-src muted">{{ camera.label }}</span>
      </div>
      <span class="cam-meta muted">
        every {{ camera.process_every }} · {{ camera.zones.length }}
        zone{{ camera.zones.length === 1 ? "" : "s" }}
      </span>
      <span class="badge" :class="camera.enabled ? 'ok' : 'off'">
        {{ camera.enabled ? "enabled" : "disabled" }}
      </span>
      <div v-if="isOwner()" class="cam-actions">
        <button class="btn ghost sm" type="button" @click="toggleEdit">Edit</button>
        <button class="btn ghost sm" type="button" @click="toggleZones">Zones</button>
        <button class="btn ghost sm" type="button" :disabled="testing" @click="testConnection">
          {{ testing ? "Testing…" : "Test" }}
        </button>
        <button class="btn danger sm" type="button" @click="deleteCamera">Delete</button>
      </div>
    </div>

    <form v-if="showEdit" class="cam-edit form" @submit.prevent="saveEdit">
      <div class="row">
        <label>
          name
          <input type="text" v-model="editForm.name" required maxlength="120" />
        </label>
        <label>
          source
          <input type="text" v-model="editForm.source" required maxlength="1024" />
        </label>
        <label>
          every
          <input type="number" v-model="editForm.process_every" min="1" max="100" />
        </label>
        <label class="chk">
          <input type="checkbox" v-model="editForm.enabled" /> enabled
        </label>
        <button class="btn primary sm" type="submit">Save</button>
        <button class="btn ghost sm" type="button" @click="showEdit = false">Cancel</button>
      </div>
    </form>

    <form v-if="showZones" class="cam-zones-edit form" @submit.prevent="saveZones">
      <div class="muted">
        JSON: [{"name":"checkout","points":[[x,y],…]}] — coordinates 0..1
      </div>
      <textarea class="zones-json" rows="6" v-model="zonesJson"></textarea>
      <div class="row">
        <button class="btn primary sm" type="submit">Save zones</button>
        <button class="btn ghost sm" type="button" @click="showZones = false">Cancel</button>
      </div>
    </form>
  </div>
</template>

<script setup>
import { reactive } from "vue";
import { api } from "../api";
import { setStatus } from "../store";
import Modal from "../components/Modal.vue";

const emit = defineEmits(["close", "added"]);

const form = reactive({ name: "", ip: "", process_every: 1, enabled: true });

async function submit() {
  try {
    await api("/api/cameras", {
      method: "POST",
      body: {
        name: form.name || "",
        ip: form.ip,
        process_every: Number(form.process_every) || 1,
        enabled: form.enabled,
      },
    });
    setStatus("Camera added");
    emit("added");
    emit("close");
  } catch (err) {
    setStatus(err.message, true);
  }
}
</script>

<template>
  <Modal @close="$emit('close')">
    <template #title>Add camera</template>
    <form class="form" @submit.prevent="submit">
      <label>
        Name (optional)
        <input type="text" v-model="form.name" placeholder="e.g. Entrance" />
      </label>
      <label>
        IP address
        <input type="text" v-model="form.ip" placeholder="192.168.1.64" required />
      </label>
      <label>
        Process every Nth frame
        <input type="number" v-model="form.process_every" min="1" max="100" />
      </label>
      <label class="chk">
        <input type="checkbox" v-model="form.enabled" /> enabled
      </label>
      <p class="hint">
        Uses the username/password/port from Camera settings. Need a
        non-default URL for one camera? Add it here, then use
        <em>Edit</em> to paste a full URL.
      </p>
      <button class="btn primary" type="submit">Add camera</button>
    </form>
  </Modal>
</template>

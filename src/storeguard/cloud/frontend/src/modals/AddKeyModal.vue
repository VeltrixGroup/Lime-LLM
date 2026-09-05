<script setup>
import { reactive } from "vue";
import { api } from "../api";
import { setStatus } from "../store";
import Modal from "../components/Modal.vue";

const emit = defineEmits(["close", "added"]);

const form = reactive({ name: "" });

async function submit() {
  try {
    const created = await api("/api/agent-keys", {
      method: "POST",
      body: { name: form.name || "" },
    });
    setStatus("Token created");
    emit("added", created);
    emit("close");
  } catch (err) {
    setStatus(err.message, true);
  }
}
</script>

<template>
  <Modal @close="$emit('close')">
    <template #title>Create device token</template>
    <form class="form" @submit.prevent="submit">
      <label>
        Device name
        <input type="text" v-model="form.name" placeholder="device name (e.g. Store PC)" />
      </label>
      <button class="btn primary" type="submit">Create token</button>
    </form>
  </Modal>
</template>

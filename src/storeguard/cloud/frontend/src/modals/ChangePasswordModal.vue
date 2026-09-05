<script setup>
import { reactive } from "vue";
import { api } from "../api";
import { setStatus } from "../store";
import Modal from "../components/Modal.vue";

const emit = defineEmits(["close"]);

const form = reactive({ current_password: "", new_password: "" });

async function submit() {
  try {
    await api("/api/auth/change-password", { method: "POST", body: { ...form } });
    setStatus("Password updated");
    emit("close");
  } catch (err) {
    setStatus(err.message, true);
  }
}
</script>

<template>
  <Modal @close="$emit('close')">
    <template #title>Change password</template>
    <form class="form" @submit.prevent="submit">
      <label>
        Current password
        <input type="password" v-model="form.current_password" required />
      </label>
      <label>
        New password (min 8 characters)
        <input
          type="password"
          v-model="form.new_password"
          minlength="8"
          maxlength="72"
          required
        />
      </label>
      <button class="btn primary" type="submit">Update password</button>
    </form>
  </Modal>
</template>

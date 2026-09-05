<script setup>
import { reactive } from "vue";
import { api } from "../api";
import { setStatus } from "../store";
import Modal from "../components/Modal.vue";

const emit = defineEmits(["close", "added"]);

const form = reactive({ email: "", full_name: "", password: "", role: "staff" });

async function submit() {
  try {
    await api("/api/org/members", { method: "POST", body: { ...form } });
    setStatus("Member added");
    emit("added");
    emit("close");
  } catch (err) {
    setStatus(err.message, true);
  }
}
</script>

<template>
  <Modal @close="$emit('close')">
    <template #title>Add member</template>
    <form class="form" @submit.prevent="submit">
      <label>
        Email
        <input type="email" v-model="form.email" placeholder="email" required />
      </label>
      <label>
        Name (optional)
        <input type="text" v-model="form.full_name" placeholder="name (optional)" />
      </label>
      <label>
        Initial password
        <input
          type="password"
          v-model="form.password"
          placeholder="initial password"
          minlength="8"
          maxlength="72"
          required
        />
      </label>
      <label>
        Role
        <select v-model="form.role">
          <option value="staff">staff</option>
          <option value="owner">owner</option>
        </select>
      </label>
      <button class="btn primary" type="submit">Add member</button>
    </form>
  </Modal>
</template>

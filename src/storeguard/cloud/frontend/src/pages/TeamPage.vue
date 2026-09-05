<script setup>
import { ref, onMounted } from "vue";
import { api } from "../api";
import { me, setStatus, isOwner } from "../store";
import AddMemberModal from "../modals/AddMemberModal.vue";

const members = ref([]);
const showAddModal = ref(false);

async function loadMembers() {
  try {
    const data = await api("/api/org/members");
    members.value = data.members || [];
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function changeRole(member, role) {
  const previousRole = member.role;
  try {
    await api(`/api/org/members/${member.id}`, { method: "PATCH", body: { role } });
    setStatus("Role updated");
    // If we changed our own role we may no longer be an owner — re-fetch.
    if (member.id === me.value.user.id) {
      me.value = await api("/api/me");
    }
    await loadMembers();
  } catch (err) {
    member.role = previousRole; // revert the select to the server's truth
    setStatus(err.message, true);
  }
}

async function removeMember(member) {
  if (!window.confirm(`Remove ${member.email}?`)) return;
  try {
    await api(`/api/org/members/${member.id}`, { method: "DELETE" });
    setStatus("Member removed");
    await loadMembers();
  } catch (err) {
    setStatus(err.message, true);
  }
}

onMounted(loadMembers);
</script>

<template>
  <div class="page-head">
    <h1>Team</h1>
  </div>

  <section class="card">
    <div class="section-head">
      <h2>
        <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3"/><path d="M3.5 19c0-3 2.5-5 5.5-5s5.5 2 5.5 5"/><circle cx="17" cy="7" r="2.4"/><path d="M15.7 14.2c2.4.3 4.3 2.1 4.3 4.8"/></svg>
        Team
      </h2>
      <div class="section-actions">
        <span class="muted">{{ members.length }} member{{ members.length === 1 ? "" : "s" }}</span>
        <button
          v-if="isOwner()"
          class="btn primary sm"
          type="button"
          @click="showAddModal = true"
        >
          + Add member
        </button>
      </div>
    </div>
    <div class="table-wrap">
      <table class="members">
        <thead>
          <tr>
            <th>Email</th>
            <th>Name</th>
            <th>Role</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in members" :key="m.id">
            <td>{{ m.email }}{{ m.id === me.user.id ? " (you)" : "" }}</td>
            <td>{{ m.full_name || "—" }}</td>
            <td>
              <select v-if="isOwner()" v-model="m.role" @change="changeRole(m, m.role)">
                <option value="owner">owner</option>
                <option value="staff">staff</option>
              </select>
              <span v-else class="role">{{ m.role }}</span>
            </td>
            <td>
              <button
                v-if="isOwner() && m.id !== me.user.id"
                class="btn danger sm"
                type="button"
                @click="removeMember(m)"
              >
                Remove
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <AddMemberModal
    v-if="showAddModal"
    @close="showAddModal = false"
    @added="loadMembers"
  />
</template>

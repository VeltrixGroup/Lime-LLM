<script setup>
import { reactive, ref } from "vue";
import { api } from "../api";
import { me, setStatus } from "../store";

const activeTab = ref("login");

const loginForm = reactive({ email: "", password: "" });
const signupForm = reactive({ org_name: "", full_name: "", email: "", password: "" });

async function submitLogin() {
  try {
    me.value = await api("/api/auth/login", { method: "POST", body: { ...loginForm } });
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function submitSignup() {
  try {
    me.value = await api("/api/auth/signup", { method: "POST", body: { ...signupForm } });
  } catch (err) {
    setStatus(err.message, true);
  }
}
</script>

<template>
  <main class="auth-shell">
    <header class="top">
      <h1 class="brand">storeguard</h1>
      <p class="tagline">cloud control plane</p>
    </header>

    <div class="tabs">
      <button
        type="button"
        class="tab"
        :class="{ active: activeTab === 'login' }"
        @click="activeTab = 'login'"
      >
        Log in
      </button>
      <button
        type="button"
        class="tab"
        :class="{ active: activeTab === 'signup' }"
        @click="activeTab = 'signup'"
      >
        Create organization
      </button>
    </div>

    <form v-if="activeTab === 'login'" class="card form" @submit.prevent="submitLogin">
      <label>
        Email
        <input type="email" v-model="loginForm.email" autocomplete="username" required />
      </label>
      <label>
        Password
        <input
          type="password"
          v-model="loginForm.password"
          autocomplete="current-password"
          required
        />
      </label>
      <button class="btn primary" type="submit">Log in</button>
    </form>

    <form v-else class="card form" @submit.prevent="submitSignup">
      <label>
        Organization name
        <input type="text" v-model="signupForm.org_name" maxlength="200" required />
      </label>
      <label>
        Your name
        <input type="text" v-model="signupForm.full_name" maxlength="200" />
      </label>
      <label>
        Email
        <input type="email" v-model="signupForm.email" autocomplete="username" required />
      </label>
      <label>
        Password (min 8 characters)
        <input
          type="password"
          v-model="signupForm.password"
          autocomplete="new-password"
          minlength="8"
          maxlength="72"
          required
        />
      </label>
      <button class="btn primary" type="submit">Create organization</button>
    </form>
  </main>
</template>

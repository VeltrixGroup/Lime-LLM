<script setup>
import { reactive, ref } from "vue";
import { api } from "../api";
import { me, setStatus } from "../store";

const activeTab = ref("login");

const loginForm = reactive({ email: "", password: "" });
const signupForm = reactive({ org_name: "", full_name: "", email: "", password: "" });
const forgotEmail = ref("");
const forgotSent = ref(false);

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

function openForgot() {
  forgotEmail.value = loginForm.email;
  forgotSent.value = false;
  activeTab.value = "forgot";
}

async function submitForgot() {
  try {
    await api("/api/auth/forgot-password", {
      method: "POST",
      body: { email: forgotEmail.value },
    });
    forgotSent.value = true;
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
      <button type="button" class="link-btn" @click="openForgot">Forgot password?</button>
    </form>

    <div v-else-if="activeTab === 'forgot'" class="card form">
      <template v-if="!forgotSent">
        <p class="hint">Enter your account email and we'll send a link to reset your password.</p>
        <form @submit.prevent="submitForgot">
          <label>
            Email
            <input type="email" v-model="forgotEmail" autocomplete="username" required />
          </label>
          <button class="btn primary" type="submit">Send reset link</button>
        </form>
      </template>
      <template v-else>
        <p class="hint">
          If an account exists for {{ forgotEmail }}, a reset link is on its way — check your inbox.
        </p>
      </template>
      <button type="button" class="link-btn" @click="activeTab = 'login'">Back to log in</button>
    </div>

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

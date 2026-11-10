<script setup>
import { reactive, ref } from "vue";
import { api } from "../api";
import { me, setStatus } from "../store";
import Logo from "./Logo.vue";

defineEmits(["register"]);
const form = reactive({ login: "", password: "", remember: true });
const mode = ref("login"); // login | forgot | sent
const busy = ref(false);

async function submit() {
  busy.value = true;
  try {
    me.value = await api("/api/auth/login", { method: "POST", body: { email: form.login, password: form.password } });
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    busy.value = false;
  }
}

async function forgot() {
  if (!form.login.includes("@")) {
    setStatus("Для сброса пароля укажите email", true);
    return;
  }
  busy.value = true;
  try {
    await api("/api/auth/forgot-password", { method: "POST", body: { email: form.login } });
    mode.value = "sent";
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="auth">
    <section class="auth-art">
      <div class="brand" style="font-size: 20px; padding: 0"><Logo large />Лайм AI</div>
      <div style="position: relative; height: 340px; max-width: 560px; width: 100%" aria-hidden="true">
        <div class="video" style="position: absolute; inset: 0; background: none; border: none">
          <div class="box" style="left: 8%; top: 10%; width: 22%; height: 78%; border-color: #B6F24A">
            <span class="tag" style="background: #B6F24A">Сотрудник · на месте</span></div>
          <div class="box" style="left: 42%; top: 18%; width: 20%; height: 72%; border-color: #8AB4FF">
            <span class="tag" style="background: #8AB4FF">Посетитель · 94%</span></div>
          <div class="box" style="left: 72%; top: 14%; width: 20%; height: 76%; border-color: #FF8F5A">
            <span class="tag" style="background: #FF8F5A; top: auto; bottom: -26px">телефон 05:02 · REC</span></div>
        </div>
      </div>
      <div style="display: flex; flex-direction: column; gap: 10px; max-width: 520px">
        <h2>Видеонаблюдение, которое само замечает нарушения</h2>
        <p style="margin: 0" class="text-2">Кражи, касса без чека, телефон и простои дольше 5 минут — клип сразу в Telegram.</p>
      </div>
    </section>

    <section class="auth-form">
      <form v-if="mode === 'login'" @submit.prevent="submit">
        <div style="display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px">
          <h1>Вход в панель</h1>
          <span class="muted">Войдите, чтобы смотреть камеры и инциденты вашего магазина</span>
        </div>
        <label class="field">Телефон или email
          <input v-model.trim="form.login" type="text" placeholder="+7 900 000-00-00" autocomplete="username" required />
        </label>
        <label class="field">
          <span style="display: flex; justify-content: space-between">
            <span>Пароль</span>
            <a href="#" style="font-size: 13px" @click.prevent="mode = 'forgot'">Забыли пароль?</a>
          </span>
          <input v-model="form.password" type="password" placeholder="••••••••" autocomplete="current-password" required />
        </label>
        <label style="display: flex; align-items: center; gap: 10px; font-size: 14px" class="text-2">
          <input v-model="form.remember" type="checkbox" style="width: 18px; height: 18px" />Запомнить это устройство
        </label>
        <button class="btn primary lg" type="submit" :disabled="busy">Войти</button>
        <span style="text-align: center; font-size: 14px" class="muted">Нет аккаунта?
          <a href="/register" style="font-weight: 700" @click.prevent="$emit('register')">Зарегистрироваться</a></span>
        <span style="text-align: center; font-size: 12px" class="faint">Доступ к разделам зависит от вашей роли. Роли назначает владелец магазина.</span>
      </form>

      <form v-else-if="mode === 'forgot'" @submit.prevent="forgot">
        <h1>Сброс пароля</h1>
        <span class="muted">Пришлём ссылку для нового пароля на email аккаунта.</span>
        <label class="field">Email
          <input v-model.trim="form.login" type="email" placeholder="you@example.com" required />
        </label>
        <button class="btn primary lg" type="submit" :disabled="busy">Отправить ссылку</button>
        <a href="#" style="text-align: center" @click.prevent="mode = 'login'">Назад ко входу</a>
      </form>

      <form v-else @submit.prevent="mode = 'login'">
        <h1>Проверьте почту</h1>
        <span class="muted">Если такой email зарегистрирован, письмо со ссылкой уже в пути. Ссылка действует 1 час.</span>
        <button class="btn primary lg" type="submit">Назад ко входу</button>
      </form>
    </section>
  </div>
</template>

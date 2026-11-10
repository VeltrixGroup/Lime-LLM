<script setup>
import { reactive, ref } from "vue";
import { api } from "../api";
import { me, setStatus } from "../store";
import Logo from "./Logo.vue";

defineEmits(["login"]);
const form = reactive({ full_name: "", phone: "", email: "", org_name: "", address: "", password: "", confirm: "", agree: false });
const busy = ref(false);

async function submit() {
  if (form.password.length < 8) return setStatus("Пароль — минимум 8 символов", true);
  if (form.password !== form.confirm) return setStatus("Пароли не совпадают", true);
  if (!form.agree) return setStatus("Нужно согласие с условиями использования", true);
  busy.value = true;
  try {
    me.value = await api("/api/auth/signup", {
      method: "POST",
      body: {
        full_name: form.full_name, phone: form.phone, email: form.email,
        org_name: form.org_name, address: form.address, password: form.password,
      },
    });
    // Step 2 of 3: cameras.
    window.history.replaceState(null, "", "/cameras?setup=2");
    window.location.reload();
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="auth">
    <section class="auth-art" style="flex: 1 1 440px; justify-content: flex-start; gap: 48px">
      <a href="/" class="brand" style="font-size: 20px; padding: 0" @click.prevent="$emit('login')"><Logo large />Лайм AI</a>
      <div style="display: flex; flex-direction: column; gap: 12px; max-width: 460px">
        <h2>Подключите магазин за три шага</h2>
        <p style="margin: 0" class="text-2">Вы регистрируетесь как владелец. Кассиров, администраторов и охрану добавите потом — каждому своя роль и свой доступ.</p>
      </div>
      <div class="steps">
        <div class="step on"><span class="n">1</span><div class="t"><strong>Аккаунт и магазин</strong><span class="text-2" style="font-size: 14px">Сейчас — данные владельца и точки</span></div></div>
        <div class="step"><span class="n">2</span><div class="t"><strong>Камеры</strong><span class="text-2" style="font-size: 14px">Подключаем IP-камеры по RTSP / ONVIF</span></div></div>
        <div class="step" style="padding-bottom: 0"><span class="n">3</span><div class="t"><strong>Telegram-бот и команда</strong><span class="text-2" style="font-size: 14px">Кому приходят клипы и кто что видит</span></div></div>
      </div>
    </section>

    <section class="auth-form" style="flex: 1 1 560px">
      <form class="wide" @submit.prevent="submit">
        <div style="display: flex; flex-direction: column; gap: 6px; margin-bottom: 6px">
          <span class="section-label">ШАГ 1 ИЗ 3</span>
          <h1>Регистрация</h1>
        </div>
        <div class="grid2">
          <label class="field">Имя и фамилия<input v-model.trim="form.full_name" type="text" autocomplete="name" required /></label>
          <label class="field">Телефон<input v-model.trim="form.phone" type="tel" placeholder="+7 900 000-00-00" autocomplete="tel" /></label>
        </div>
        <label class="field">Email<input v-model.trim="form.email" type="email" placeholder="you@example.com" autocomplete="email" required /></label>
        <div class="grid2">
          <label class="field">Название магазина<input v-model.trim="form.org_name" type="text" required /></label>
          <label class="field">Город и адрес<input v-model.trim="form.address" type="text" /></label>
        </div>
        <div class="grid2">
          <label class="field">Пароль<input v-model="form.password" type="password" autocomplete="new-password" required /></label>
          <label class="field">Повторите пароль<input v-model="form.confirm" type="password" autocomplete="new-password" required /></label>
        </div>
        <label style="display: flex; align-items: flex-start; gap: 10px; font-size: 14px" class="text-2">
          <input v-model="form.agree" type="checkbox" style="width: 18px; height: 18px; margin-top: 2px" />
          Согласен с условиями использования и обработкой видеоданных
        </label>
        <button class="btn primary lg" type="submit" :disabled="busy">Продолжить — подключить камеры</button>
        <span style="text-align: center; font-size: 14px" class="muted">Уже есть аккаунт?
          <a href="/" style="font-weight: 700" @click.prevent="$emit('login')">Войти</a></span>
        <span style="text-align: center; font-size: 12px" class="faint">Получили приглашение от владельца? Войдите с данными из приглашения — роль уже назначена.</span>
      </form>
    </section>
  </div>
</template>

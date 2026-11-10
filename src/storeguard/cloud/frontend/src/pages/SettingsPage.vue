<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import { api } from "../api";
import { fmtTime, me, setStatus } from "../store";
import Icon from "../components/Icon.vue";
import Logo from "../components/Logo.vue";
import Modal from "../components/Modal.vue";

const route = useRoute();
const setup = computed(() => route.query.setup === "3");
const rules = reactive({ phone_min: 5, idle_min: 5, theft: true, cash: true, refund: true, clip_sec: 20 });
const store = reactive({ name: "", address: "", open_time: "09:00", close_time: "21:00" });
const tg = reactive({ enabled: false, bot_token: "", chat_id: "", token_set: false, bot_username: "", format: "clip", buttons: true,
  include_link: true, notify_offline: true, remind_min: 15, quiet_enabled: false, quiet_from: "22:00", quiet_to: "08:00", language: "ru", last_delivered_at: null });
const recipients = ref({ items: [], eligible: [], categories: [] });
const pw = reactive({ current: "", next: "" });
const addRecipient = ref(false);
const recipientUser = ref("");
const busy = ref(false);

async function load() {
  try {
    Object.assign(rules, await api("/api/settings/rules"));
    Object.assign(store, await api("/api/settings/store"));
    Object.assign(tg, await api("/api/settings/telegram"), { bot_token: "" });
    recipients.value = await api("/api/settings/telegram/recipients");
  } catch (err) {
    setStatus(err.message, true);
  }
}

function tgBody() {
  const { token_set, bot_username, last_delivered_at, ...body } = tg;
  return body;
}

async function saveAll() {
  busy.value = true;
  try {
    Object.assign(rules, await api("/api/settings/rules", { method: "PUT", body: { ...rules } }));
    Object.assign(store, await api("/api/settings/store", { method: "PUT", body: { ...store } }));
    Object.assign(tg, await api("/api/settings/telegram", { method: "PUT", body: tgBody() }), { bot_token: "" });
    if (store.name !== me.value.tenant.name) me.value.tenant.name = store.name;
    setStatus("Настройки сохранены");
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    busy.value = false;
  }
}

async function checkBot() {
  try {
    if (tg.bot_token) Object.assign(tg, await api("/api/settings/telegram", { method: "PUT", body: { ...tgBody(), enabled: true } }), { bot_token: "" });
    const res = await api("/api/settings/telegram/check", { method: "POST" });
    if (!res.ok) return setStatus("Telegram не принял токен — проверьте его у @BotFather", true);
    Object.assign(tg, await api("/api/settings/telegram"), { bot_token: "" });
    recipients.value = await api("/api/settings/telegram/recipients");
    setStatus(res.chat_id ? `Бот @${res.bot_username} подключён${res.chat_found ? ", группа найдена" : ""}` : `Бот @${res.bot_username} работает. Добавьте его в группу и нажмите ещё раз`);
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function testBot() {
  try {
    const res = await api("/api/settings/telegram/test", { method: "POST" });
    setStatus(res.sent ? "Тестовое сообщение отправлено" : "Не удалось отправить — проверьте группу и права бота", !res.sent);
    if (res.sent) Object.assign(tg, await api("/api/settings/telegram"), { bot_token: "" });
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function disconnect() {
  if (!window.confirm("Отключить бота? Получатели отвяжутся, токен удалится.")) return;
  await api("/api/settings/telegram/disconnect", { method: "POST" }).catch((e) => setStatus(e.message, true));
  load();
}

async function toggleCategory(r, c) {
  const cats = r.categories.includes(c) ? r.categories.filter((x) => x !== c) : [...r.categories, c];
  await patchRecipient(r, { categories: cats });
}
async function patchRecipient(r, body) {
  try {
    Object.assign(r, await api(`/api/settings/telegram/recipients/${r.id}`, { method: "PATCH", body }));
  } catch (err) {
    setStatus(err.message, true);
  }
}
async function createRecipient() {
  try {
    await api("/api/settings/telegram/recipients", { method: "POST", body: { user_id: recipientUser.value } });
    addRecipient.value = false;
    recipients.value = await api("/api/settings/telegram/recipients");
  } catch (err) {
    setStatus(err.message, true);
  }
}
async function removeRecipient(r) {
  await api(`/api/settings/telegram/recipients/${r.id}`, { method: "DELETE" }).catch((e) => setStatus(e.message, true));
  recipients.value = await api("/api/settings/telegram/recipients");
}
function copy(text) {
  navigator.clipboard?.writeText(text).then(() => setStatus("Скопировано"));
}

async function changePassword() {
  try {
    await api("/api/auth/change-password", { method: "POST", body: { current_password: pw.current, new_password: pw.next } });
    pw.current = pw.next = "";
    setStatus("Пароль изменён");
  } catch (err) {
    setStatus(err.message, true);
  }
}

const connected = computed(() => tg.enabled && tg.token_set);
const previewName = computed(() => store.name || me.value.tenant.name);
onMounted(load);
</script>

<template>
  <div v-if="setup" class="card" style="border-color: #3d4f2a; gap: 4px">
    <span class="section-label">ШАГ 3 ИЗ 3</span>
    <strong style="font-size: 17px">Telegram-бот и команда</strong>
    <span class="muted" style="font-size: 14px">Подключите бота — клипы нарушений будут приходить в рабочую группу. Команду и права настроите в «Доступ и роли».</span>
  </div>

  <div class="page-head">
    <h1>Настройки</h1>
    <button class="btn primary" type="button" :disabled="busy" @click="saveAll">Сохранить изменения</button>
  </div>

  <div class="settings-grid">
    <section class="card" style="gap: 4px">
      <strong class="card-title">Правила срабатывания</strong>
      <span class="muted" style="font-size: 14px; margin-bottom: 10px">Когда ИИ начинает запись и отправляет клип</span>
      <div class="toggle-row"><span>Сотрудник в телефоне дольше</span>
        <label class="muted" style="display: flex; align-items: center; gap: 8px; font-size: 14px"><span class="sr-only">Минут в телефоне</span><input v-model.number="rules.phone_min" class="num" type="number" min="0.5" max="240" step="0.5" />мин</label></div>
      <div class="toggle-row"><span>Бездействие дольше</span>
        <label class="muted" style="display: flex; align-items: center; gap: 8px; font-size: 14px"><span class="sr-only">Минут бездействия</span><input v-model.number="rules.idle_min" class="num" type="number" min="0.5" max="240" step="0.5" />мин</label></div>
      <label class="toggle-row"><span>Попытки краж</span><input v-model="rules.theft" type="checkbox" class="big" /></label>
      <label class="toggle-row"><span>Товар или деньги без пробития чека</span><input v-model="rules.cash" type="checkbox" class="big" /></label>
      <label class="toggle-row"><span>Возврат без чека в руке</span><input v-model="rules.refund" type="checkbox" class="big" /></label>
      <div class="toggle-row"><span>Длина клипа</span>
        <label class="muted" style="display: flex; align-items: center; gap: 8px; font-size: 14px"><span class="sr-only">Длина клипа в секундах</span><input v-model.number="rules.clip_sec" class="num" type="number" min="5" max="120" />сек</label></div>
    </section>

    <section class="card">
      <strong class="card-title">Магазин</strong>
      <label class="field light">Название<input v-model.trim="store.name" type="text" /></label>
      <label class="field light">Адрес<input v-model.trim="store.address" type="text" /></label>
      <div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px">
        <label class="field light">Открытие<input v-model="store.open_time" type="time" /></label>
        <label class="field light">Закрытие<input v-model="store.close_time" type="time" /></label>
      </div>
      <span class="muted" style="font-size: 13px">Контроль сотрудников работает только в рабочие часы.</span>
    </section>
    <section class="card">
      <strong class="card-title">Мой пароль</strong>
      <form class="form" @submit.prevent="changePassword">
        <label class="field light">Текущий пароль<input v-model="pw.current" type="password" autocomplete="current-password" required /></label>
        <label class="field light">Новый пароль<input v-model="pw.next" type="password" minlength="8" autocomplete="new-password" required /></label>
        <div><button class="btn sm" type="submit">Сменить пароль</button></div>
      </form>
    </section>

    <!-- telegram -->
    <section class="card span-all" style="padding: 24px; gap: 22px">
      <div class="card-head">
        <div style="display: flex; gap: 12px; align-items: center">
          <span style="width: 44px; height: 44px; border-radius: 50%; background: var(--tg); display: flex; align-items: center; justify-content: center; color: #fff"><Icon name="send" :size="22" /></span>
          <div style="display: flex; flex-direction: column; line-height: 1.3"><strong style="font-size: 19px">Telegram-бот уведомлений</strong>
            <span class="muted" style="font-size: 13px">Клипы и оповещения о нарушениях{{ tg.bot_username ? ` · @${tg.bot_username}` : "" }}</span></div>
        </div>
        <div class="actions">
          <span class="pill" :class="connected ? 'on' : 'off'" style="padding: 5px 12px; font-size: 13px">
            <span class="dot" :style="{ background: connected ? 'var(--accent)' : 'var(--faint)', width: '7px', height: '7px' }"></span>{{ connected ? "Подключён" : "Не подключён" }}</span>
          <button v-if="tg.token_set" class="btn danger-ghost" type="button" @click="disconnect">Отключить бота</button>
        </div>
      </div>

      <div class="row" style="gap: 24px; align-items: flex-start">
        <div style="flex: 3 1 520px; min-width: 0; display: flex; flex-direction: column; gap: 22px">
          <div style="display: flex; flex-direction: column; gap: 12px">
            <span class="section-label">ПОДКЛЮЧЕНИЕ</span>
            <div class="grid2">
              <label class="field light">Токен бота (от @BotFather)
                <input v-model.trim="tg.bot_token" type="password" class="mono" :placeholder="tg.token_set ? 'сохранён — введите новый, чтобы заменить' : '0000000000:AAAA…'" autocomplete="off" /></label>
              <label class="field light">Группа для оповещений (chat ID)<input v-model.trim="tg.chat_id" type="text" class="mono" placeholder="-100XXXXXXXXXX" /></label>
            </div>
            <label style="display: flex; gap: 10px; align-items: center; font-size: 14px"><input v-model="tg.enabled" type="checkbox" class="big" />Отправлять оповещения</label>
            <div class="actions">
              <button class="btn primary" type="button" @click="checkBot">Проверить подключение</button>
              <button class="btn" type="button" @click="testBot">Отправить тестовое сообщение</button>
              <span class="muted" style="font-size: 13px" v-if="tg.last_delivered_at">Последнее сообщение доставлено {{ fmtTime(tg.last_delivered_at) }}</span>
            </div>
            <div class="note">1. Создайте бота у @BotFather и вставьте токен. 2. Добавьте бота в рабочую группу и сделайте администратором. 3. Нажмите «Проверить подключение» — chat ID подставится сам.</div>
          </div>

          <div style="display: flex; flex-direction: column; gap: 4px">
            <span class="section-label" style="margin-bottom: 8px">ЧТО ОТПРАВЛЯТЬ</span>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin-bottom: 8px">
              <label v-for="f in [['clip', 'Видеоклип'], ['photo', 'Снимок + текст'], ['text', 'Только текст']]" :key="f[0]"
                     style="display: flex; align-items: center; gap: 8px; padding: 12px; border-radius: 10px; font-size: 14px; font-weight: 600; cursor: pointer"
                     :style="{ border: `1px solid ${tg.format === f[0] ? 'var(--accent)' : 'var(--line-strong)'}`, background: tg.format === f[0] ? 'var(--bg-active)' : 'transparent' }">
                <input v-model="tg.format" type="radio" :value="f[0]" style="width: 16px; height: 16px" />{{ f[1] }}</label>
            </div>
            <label class="toggle-row" style="padding: 11px 0"><span>Кнопки «Подтвердить» и «Ложная тревога» в сообщении</span><input v-model="tg.buttons" type="checkbox" class="big" /></label>
            <label class="toggle-row" style="padding: 11px 0"><span>Ссылка на инцидент в панели</span><input v-model="tg.include_link" type="checkbox" class="big" /></label>
            <label class="toggle-row" style="padding: 11px 0"><span>Сообщать, если камера потеряла сигнал</span><input v-model="tg.notify_offline" type="checkbox" class="big" /></label>
            <div class="toggle-row" style="padding: 11px 0"><span>Напомнить, если инцидент не обработан через</span>
              <label class="muted" style="display: flex; align-items: center; gap: 8px; font-size: 14px"><span class="sr-only">Минут до напоминания</span><input v-model.number="tg.remind_min" class="num" type="number" min="0" max="1440" />мин</label></div>
            <div class="toggle-row" style="padding: 11px 0; flex-wrap: wrap">
              <label style="display: flex; align-items: center; gap: 10px"><input v-model="tg.quiet_enabled" type="checkbox" class="big" />Тихие часы (без звука)</label>
              <div class="muted" style="display: flex; align-items: center; gap: 8px; font-size: 14px">
                <label style="display: flex; align-items: center; gap: 6px">с<input v-model="tg.quiet_from" type="time" class="input sm" style="width: auto; padding: 8px 10px" /></label>
                <label style="display: flex; align-items: center; gap: 6px">до<input v-model="tg.quiet_to" type="time" class="input sm" style="width: auto; padding: 8px 10px" /></label>
              </div>
            </div>
            <label class="toggle-row" style="padding: 11px 0"><span>Язык сообщений</span>
              <select v-model="tg.language" class="input sm" style="width: auto"><option value="ru">Русский</option><option value="uz">O‘zbekcha</option><option value="en">English</option></select></label>
          </div>
        </div>

        <div style="flex: 2 1 300px; min-width: 0; display: flex; flex-direction: column; gap: 10px">
          <span class="section-label">ТАК ВЫГЛЯДИТ СООБЩЕНИЕ</span>
          <div style="padding: 16px; border-radius: 16px; background: #17212b; display: flex; flex-direction: column; gap: 10px">
            <div style="display: flex; align-items: center; gap: 10px"><Logo /><strong style="font-size: 14px">{{ tg.bot_username ? "@" + tg.bot_username : "Лайм AI · бот" }}</strong></div>
            <div style="padding: 10px; border-radius: 12px; background: #182533; display: flex; flex-direction: column; gap: 8px">
              <div v-if="tg.format !== 'text'" class="video" style="height: 150px; border-radius: 8px; background-size: 24px 24px; border: none; display: flex; align-items: center; justify-content: center">
                <div class="box" style="left: 36%; top: 18%; width: 22%; height: 68%; border-color: var(--alert); border-radius: 4px"></div>
                <span v-if="tg.format === 'clip'" style="width: 40px; height: 40px; border-radius: 50%; background: #0b0e0ab3; display: flex; align-items: center; justify-content: center; color: var(--text); z-index: 1"><Icon name="play" :size="16" /></span>
                <span v-if="tg.format === 'clip'" class="mono" style="position: absolute; left: 8px; bottom: 6px; font-size: 11px; color: #d5dbd0">0:{{ String(rules.clip_sec).padStart(2, "0") }}</span>
              </div>
              <div style="font-size: 14px; line-height: 1.5; color: #e8eef2"><strong style="color: var(--alert)">Нарушение:</strong> в телефоне 6 мин 12 с<br />Сотрудник: Руслан М.<br />Камера: Торговый зал<br />Магазин: {{ previewName }} · 13:58</div>
              <span v-if="tg.include_link" style="font-size: 13px; color: #6ab3f3">Открыть инцидент в панели</span>
            </div>
            <div v-if="tg.buttons" style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px">
              <span style="padding: 9px; border-radius: 8px; background: #2b5278; text-align: center; font-size: 13px; font-weight: 600; color: #fff">Подтвердить</span>
              <span style="padding: 9px; border-radius: 8px; background: #2b5278; text-align: center; font-size: 13px; font-weight: 600; color: #fff">Ложная тревога</span>
            </div>
          </div>
        </div>
      </div>

      <div style="display: flex; flex-direction: column; gap: 10px">
        <div class="card-head"><span class="section-label">ПОЛУЧАТЕЛИ И СОБЫТИЯ</span>
          <button class="btn sm" type="button" :disabled="!recipients.eligible.length" @click="recipientUser = recipients.eligible[0]?.user_id; addRecipient = true">+ Добавить получателя</button></div>
        <span class="muted" style="font-size: 13px">Добавить можно только пользователей с правом «Получать клипы в Telegram» (<router-link to="/access">Доступ и роли</router-link>). Пользователь привязывает Telegram, нажав /start в боте по своей ссылке.</span>
        <div class="table" style="border-radius: 12px; border: 1px solid var(--line)">
          <div style="min-width: 860px">
            <div class="thead" style="grid-template-columns: 1.6fr 1.2fr 3.2fr 0.6fr; background: var(--bg-sunk); padding: 10px 16px"><span>ПОЛУЧАТЕЛЬ</span><span>TELEGRAM</span><span>СОБЫТИЯ</span><span>ВКЛ</span></div>
            <div v-for="r in recipients.items" :key="r.id" class="tr" style="grid-template-columns: 1.6fr 1.2fr 3.2fr 0.6fr; padding: 12px 16px">
              <div style="display: flex; flex-direction: column; line-height: 1.3"><span>{{ r.name }}</span><span class="muted" style="font-size: 12px">{{ r.role_name }}
                <a href="#" style="margin-left: 6px" @click.prevent="removeRecipient(r)">убрать</a></span></div>
              <span class="mono" style="font-size: 13px" :style="{ color: r.linked ? 'var(--accent)' : 'var(--warn)' }">
                <template v-if="r.linked">привязан</template>
                <template v-else>ждёт /start <a v-if="r.link" href="#" style="font-size: 12px" @click.prevent="copy(r.link)">ссылка</a></template></span>
              <div style="display: flex; flex-wrap: wrap; gap: 6px">
                <button v-for="c in recipients.categories" :key="c.key" type="button" class="pill sm" :class="r.categories.includes(c.key) ? 'on' : 'off'"
                        style="cursor: pointer; border: none" :style="r.categories.includes(c.key) ? {} : { border: '1px solid var(--line-strong)', background: 'transparent' }"
                        @click="toggleCategory(r, c.key)">{{ c.label.replace(/ > 5 мин/, "") }}</button>
              </div>
              <label style="display: flex"><span class="sr-only">Оповещения включены</span>
                <input type="checkbox" class="big" :checked="r.enabled" @change="patchRecipient(r, { enabled: $event.target.checked })" /></label>
            </div>
            <div v-if="!recipients.items.length" class="empty">Получателей нет — сообщения идут только в группу</div>
          </div>
        </div>
      </div>
    </section>

  </div>

  <Modal v-if="addRecipient" @close="addRecipient = false">
    <template #title>Добавить получателя</template>
    <form class="form" @submit.prevent="createRecipient">
      <label class="field light">Пользователь<select v-model="recipientUser" class="input sm">
        <option v-for="u in recipients.eligible" :key="u.user_id" :value="u.user_id">{{ u.name }}</option></select></label>
      <span class="hint">После добавления отправьте ему ссылку «/start» из таблицы — так бот узнает его Telegram.</span>
      <button class="btn primary" type="submit">Добавить</button>
    </form>
  </Modal>
</template>

<script setup>
import { onMounted, reactive, ref } from "vue";
import { api } from "../api";
import { can, initials, setStatus } from "../store";
import Icon from "../components/Icon.vue";
import Modal from "../components/Modal.vue";

const data = ref(null);
const candidates = ref([]);
const modal = ref(null); // { mode: "new" | "edit" | "accept", candidate?, employee? }
const form = reactive({ name: "", position: "", employee_id: "", active: true });

const STATUS_COLOR = { "На кассе": "var(--accent)", "В зале": "var(--accent)", "На складе": "var(--accent)", "На смене": "var(--accent)", "В телефоне": "var(--warn)", "Бездействие": "var(--warn)" };

async function load() {
  try {
    data.value = await api("/api/employees");
    if (can("staff_tag")) candidates.value = (await api("/api/employees/candidates")).items;
  } catch (err) {
    setStatus(err.message, true);
  }
}

function open(mode, extra = {}) {
  Object.assign(form, { name: "", position: "", employee_id: "", active: true });
  if (extra.employee) Object.assign(form, { name: extra.employee.name, position: extra.employee.position, active: extra.employee.active });
  modal.value = { mode, ...extra };
}

async function submit() {
  try {
    const m = modal.value;
    if (m.mode === "accept") {
      const body = form.employee_id ? { employee_id: form.employee_id } : { name: form.name, position: form.position };
      await api(`/api/employees/candidates/${m.candidate.id}/accept`, { method: "POST", body });
      setStatus("Сотрудник отмечен — теперь система узнаёт его сама");
    } else if (m.mode === "edit") {
      await api(`/api/employees/${m.employee.id}`, { method: "PATCH", body: { name: form.name, position: form.position, active: form.active } });
      setStatus("Сохранено");
    } else {
      await api("/api/employees", { method: "POST", body: { name: form.name, position: form.position } });
      setStatus("Сотрудник добавлен. Отметьте его лицо в «Новые лица», чтобы система узнавала его.");
    }
    modal.value = null;
    load();
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function reject(c) {
  try {
    await api(`/api/employees/candidates/${c.id}/reject`, { method: "POST" });
    candidates.value = candidates.value.filter((x) => x.id !== c.id);
  } catch (err) {
    setStatus(err.message, true);
  }
}

async function remove(e) {
  if (!window.confirm(`Удалить сотрудника «${e.name}»? Статистика по нему тоже удалится.`)) return;
  try {
    await api(`/api/employees/${e.id}`, { method: "DELETE" });
    modal.value = null;
    load();
  } catch (err) {
    setStatus(err.message, true);
  }
}

const seen = (c) => `${c.zone.replace(/^(staff|служ\S*)\s*/i, "") || "Служебная зона"} · ${c.seen_count} ${c.seen_count === 1 ? "раз" : c.seen_count < 5 ? "раза" : "раз"}${c.today ? " сегодня" : ""}`;
const busyColor = (p) => (p == null ? "var(--faint)" : p >= 75 ? "var(--accent)" : "var(--warn)");
onMounted(load);
</script>

<template>
  <div class="page-head">
    <div style="display: flex; flex-direction: column; gap: 2px">
      <h1>Сотрудники</h1>
      <span class="sub" v-if="data">{{ data.total }} сотрудников · {{ data.on_shift }} на смене сейчас</span>
    </div>
    <button v-if="can('staff_tag')" class="btn primary" type="button" @click="open('new')">+ Отметить сотрудника</button>
  </div>

  <div v-if="can('staff_tag')" class="card">
    <div style="display: flex; flex-direction: column; gap: 2px">
      <strong style="font-size: 17px">Новые лица в служебных зонах</strong>
      <span class="muted" style="font-size: 14px">Отметьте, кто из них сотрудник — дальше система будет узнавать его сама</span>
    </div>
    <div v-if="candidates.length" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px">
      <div v-for="c in candidates" :key="c.id" style="padding: 14px; border-radius: 12px; background: var(--bg-sunk); border: 1px solid var(--line); display: flex; gap: 12px; align-items: center">
        <div style="width: 64px; height: 64px; flex: none; border-radius: 10px; background: var(--bg-active); display: flex; align-items: center; justify-content: center; overflow: hidden">
          <img v-if="c.has_photo" :src="`/api/employees/candidates/${c.id}/photo.jpg`" alt="" style="width: 100%; height: 100%; object-fit: cover" />
          <Icon v-else name="person" :size="30" style="color: var(--faint)" />
        </div>
        <div style="flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px">
          <span class="muted" style="font-size: 13px">{{ seen(c) }}</span>
          <div style="display: flex; gap: 6px">
            <button class="btn sm primary" type="button" @click="open('accept', { candidate: c })">Сотрудник</button>
            <button class="btn sm" type="button" @click="reject(c)">Нет</button>
          </div>
        </div>
      </div>
    </div>
    <div v-else class="hint">Новых лиц нет. Они появляются, когда незнакомый человек проводит время в зоне, названной «Служебная зона» (склад, за кассой) — зоны рисуются в настройке камер.</div>
  </div>

  <div v-if="data" class="card flush">
    <div class="table">
      <div style="min-width: 860px">
        <div class="thead" style="grid-template-columns: 2fr 1fr 1fr 1fr 1fr 1.6fr">
          <span>СОТРУДНИК</span><span>СЕЙЧАС</span><span>ТЕЛЕФОН, МИН</span><span>ПРОСТОЙ, МИН</span><span>НАРУШЕНИЙ / 7 ДН</span><span>ЗАНЯТОСТЬ</span></div>
        <div v-for="e in data.items" :key="e.id" class="tr" style="grid-template-columns: 2fr 1fr 1fr 1fr 1fr 1.6fr; padding-top: 14px; padding-bottom: 14px; cursor: pointer"
             @click="can('staff_tag') && open('edit', { employee: e })">
          <div class="who"><span class="avatar">
            <img v-if="e.samples" :src="`/api/employees/${e.id}/photo.jpg`" alt="" @error="($event.target.style.display = 'none')" />
            <template v-else>{{ initials(e.name) }}</template></span>
            <div class="t"><strong>{{ e.name }}</strong><small>{{ e.position || "—" }}{{ e.samples ? "" : " · не узнаётся камерой" }}</small></div></div>
          <span style="font-size: 14px" :style="{ color: STATUS_COLOR[e.status] || 'var(--muted)' }">{{ e.status }}</span>
          <span class="mono" :class="{ alert: e.phone_min_today >= 30 }">{{ e.phone_min_today ?? "—" }}</span>
          <span class="mono" :class="{ alert: e.idle_min_today >= 30 }">{{ e.idle_min_today ?? "—" }}</span>
          <span class="mono" :class="{ alert: e.violations_7d > 0 }">{{ e.violations_7d }}</span>
          <div style="display: flex; align-items: center; gap: 8px">
            <div style="flex: 1; height: 8px; border-radius: 4px; background: var(--line-strong)">
              <div style="height: 8px; border-radius: 4px" :style="{ width: (e.busy_pct ?? 0) + '%', background: busyColor(e.busy_pct) }"></div></div>
            <span class="mono" style="font-size: 13px; width: 38px">{{ e.busy_pct == null ? "—" : e.busy_pct + "%" }}</span>
          </div>
        </div>
        <div v-if="!data.items.length" class="empty">Сотрудников пока нет — отметьте их среди новых лиц или добавьте вручную</div>
      </div>
    </div>
  </div>
  <span class="faint" style="font-size: 13px">Занятость = доля смены без телефона и простоев. Считается только в рабочие часы магазина.</span>

  <Modal v-if="modal" @close="modal = null">
    <template #title>{{ modal.mode === "accept" ? "Это сотрудник" : modal.mode === "edit" ? modal.employee.name : "Новый сотрудник" }}</template>
    <form class="form" @submit.prevent="submit">
      <template v-if="modal.mode === 'accept'">
        <img v-if="modal.candidate.has_photo" :src="`/api/employees/candidates/${modal.candidate.id}/photo.jpg`" alt="" style="max-height: 180px; align-self: center; border-radius: 10px" />
        <label class="field light">Это уже известный сотрудник?
          <select v-model="form.employee_id" class="input sm"><option value="">Нет — новый сотрудник</option>
            <option v-for="e in data.items" :key="e.id" :value="e.id">{{ e.name }}</option></select></label>
      </template>
      <template v-if="modal.mode !== 'accept' || !form.employee_id">
        <label class="field light">Имя и фамилия<input v-model.trim="form.name" type="text" required /></label>
        <label class="field light">Должность<input v-model.trim="form.position" type="text" placeholder="Кассир" /></label>
      </template>
      <label v-if="modal.mode === 'edit'" style="display: flex; gap: 10px; align-items: center"><input v-model="form.active" type="checkbox" class="big" />Работает (узнавать на камерах)</label>
      <div class="actions" style="justify-content: space-between">
        <button v-if="modal.mode === 'edit'" class="btn danger-ghost sm" type="button" @click="remove(modal.employee)">Удалить</button><span v-else></span>
        <button class="btn primary" type="submit">Сохранить</button>
      </div>
    </form>
  </Modal>
</template>

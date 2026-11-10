import { reactive, ref } from "vue";

// `me`: { user:{id,email,full_name,phone}, tenant:{id,name,role,role_name,permissions} } | null
export const me = ref(null);
export const stores = ref([]);

export function can(perm) {
  return Boolean(me.value && me.value.tenant && (me.value.tenant.permissions || []).includes(perm));
}

export const status = reactive({ message: "", isError: false, show: false });
let statusTimer = null;

export function setStatus(msg, isError = false) {
  status.message = msg || "";
  status.isError = Boolean(isError);
  clearTimeout(statusTimer);
  if (!msg) {
    status.show = false;
    return;
  }
  status.show = true;
  statusTimer = setTimeout(() => { status.show = false; }, isError ? 7000 : 3500);
}

export function initials(name) {
  const parts = String(name || "").replace(/[^\p{L}\s]/gu, " ").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "··";
  return (parts[0][0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
}

export const fmtNum = (n) => (n == null ? "—" : Number(n).toLocaleString("ru-RU"));

export function fmtChange(pct, { invert = false, suffix = "к прошлому периоду" } = {}) {
  if (pct == null) return null;
  const good = invert ? pct <= 0 : pct >= 0;
  return { text: `${pct > 0 ? "+" : pct < 0 ? "−" : ""}${Math.abs(pct)}% ${suffix}`, good };
}

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const now = new Date();
  const hm = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return hm;
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "вчера";
  const days = Math.floor((now - d) / 86400000);
  if (days < 7) return `${days} ${days < 5 ? "дня" : "дней"} назад`;
  return d.toLocaleDateString("ru-RU");
}

export function clock(d = new Date()) {
  return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// Incident category -> colour of its dot (design: theft/cash/refund orange, staff yellow).
export const CATEGORY_COLOR = {
  theft: "#FF8F5A", cash: "#FF8F5A", refund: "#FF8F5A", phone: "#FFC53D", idle: "#FFC53D", offline: "#FF8F5A",
};
export const CATEGORY_LABEL = {
  theft: "Кражи", cash: "Касса без чека", refund: "Фейковые возвраты",
  phone: "Телефон > 5 мин", idle: "Бездействие > 5 мин", offline: "Камера офлайн",
};
export const STATUS = {
  new: { label: "Новый", bg: "#3A1F14", fg: "#FF8F5A" },
  confirmed: { label: "Подтверждён", bg: "#1F2A1A", fg: "#B6F24A" },
  false: { label: "Ложная тревога", bg: "#22261F", fg: "#B9C2B2" },
};

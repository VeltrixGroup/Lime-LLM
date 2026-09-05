import { reactive, ref } from "vue";

// Singleton app state — small enough not to need Pinia. `me` holds
// { user:{id,email,full_name}, tenant:{id,name,slug,role} } or null (logged out).
export const me = ref(null);

export function isOwner() {
  return Boolean(me.value && me.value.tenant && me.value.tenant.role === "owner");
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
  // Errors stay until the next action; confirmations fade on their own.
  if (!isError) {
    statusTimer = setTimeout(() => {
      status.show = false;
    }, 4000);
  }
}

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

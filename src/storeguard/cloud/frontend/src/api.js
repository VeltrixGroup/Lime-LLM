// Backend error texts -> what the panel shows (Russian UI).
const ERRORS = [
  [/invalid email or password/i, "Неверный телефон/email или пароль"],
  [/email already registered/i, "Этот email уже зарегистрирован"],
  [/phone already registered|user with this phone/i, "Этот телефон уже зарегистрирован"],
  [/user with this email already exists/i, "Пользователь с таким email уже есть"],
  [/invalid phone number/i, "Неверный номер телефона"],
  [/invalid email address/i, "Неверный email"],
  [/permission '([a-z_]+)' required|owner role required/i, "Недостаточно прав для этого действия"],
  [/not authenticated|no active tenant/i, "Войдите заново"],
  [/camera with this name already exists/i, "Камера с таким названием уже есть"],
  [/cannot (demote|remove) the last owner/i, "Нельзя убрать единственного владельца"],
  [/role is still assigned/i, "Роль назначена пользователям — сначала смените им роль"],
  [/built-in roles can't be deleted/i, "Встроенные роли удалить нельзя"],
  [/no 'tg' permission/i, "У роли пользователя нет права «Получать клипы в Telegram»"],
  [/bot token and chat id/i, "Укажите токен бота и chat ID группы (или нажмите «Проверить подключение»)"],
  [/set a bot token/i, "Сначала укажите токен бота"],
  [/set a webhook url/i, "Сначала укажите адрес webhook"],
  [/current password is incorrect/i, "Текущий пароль неверный"],
  [/invalid or expired reset link/i, "Ссылка для сброса пароля устарела"],
  [/server and token are required/i, "Этот кабинет не запускает камеры сам — запустите детекцию на ПК магазина"],
];

export function humanError(detail) {
  for (const [re, text] of ERRORS) if (re.test(detail)) return text;
  return detail;
}

export async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* no JSON body */
  }
  if (!res.ok) {
    let detail = `Ошибка запроса (${res.status})`;
    if (data && data.detail) {
      detail = typeof data.detail === "string"
        ? humanError(data.detail)
        : "Проверьте введённые данные";
    }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return data;
}

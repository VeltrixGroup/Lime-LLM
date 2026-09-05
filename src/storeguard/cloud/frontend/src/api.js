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
    const detail =
      data && data.detail
        ? typeof data.detail === "string"
          ? data.detail
          : "invalid input"
        : `request failed (${res.status})`;
    throw new Error(detail);
  }
  return data;
}

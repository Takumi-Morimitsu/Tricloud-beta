const API_BASE = (import.meta.env.VITE_ADMIN_API_BASE || "http://127.0.0.1:8010").replace(/\/$/, "");
const TOKEN_KEY = "tricloud_admin_session";

export type JsonRecord = Record<string, unknown>;

export function getToken(): string {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token: string): void {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
  window.dispatchEvent(new Event("tricloud-admin-auth"));
}

export async function api<T = JsonRecord>(
  path: string,
  options: RequestInit = {},
  authenticated = true,
): Promise<T> {
  const headers = new Headers(options.headers || {});
  headers.set("Content-Type", "application/json");
  headers.set("X-Request-ID", crypto.randomUUID());
  if (authenticated && getToken()) headers.set("Authorization", `Bearer ${getToken()}`);

  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    if (response.status === 401 && authenticated) setToken("");
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : `${response.status} ${response.statusText}`;
    throw new Error(detail);
  }
  return payload as T;
}

export async function login(email: string, password: string): Promise<void> {
  const result = await api<{ access_token: string }>(
    "/admin/v1/session",
    { method: "POST", body: JSON.stringify({ email, password }) },
    false,
  );
  setToken(result.access_token);
}

export async function logout(): Promise<void> {
  try {
    await api("/admin/v1/session", { method: "DELETE" });
  } finally {
    setToken("");
  }
}

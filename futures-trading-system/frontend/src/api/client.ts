// Read-only API client. The session token arrives in the URL fragment (#token=...), which browsers
// never send to the server; it is moved to sessionStorage and removed from the address bar.

const KEY = "fts.session.token";
let token: string | null = null;

export function initToken(loc: Location = window.location, store: Storage = window.sessionStorage): string | null {
  const m = /(?:^#|&)token=([A-Za-z0-9_-]{16,128})/.exec(loc.hash);
  if (m) {
    token = m[1];
    try { store.setItem(KEY, token); } catch { /* private mode: keep it in memory */ }
    try { window.history.replaceState(null, "", loc.pathname + loc.search); } catch { /* ignore */ }
  } else {
    try { token = store.getItem(KEY); } catch { token = null; }
  }
  return token;
}

export function getToken(): string | null { return token; }
export function setToken(t: string | null): void { token = t; }

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  if (!path.startsWith("/api/")) throw new ApiError(0, "invalid API path");
  const res = await fetch(path, {
    method: "GET",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    credentials: "omit",
    cache: "no-store",
    signal,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = ((await res.json()) as { detail?: string }).detail ?? detail; } catch { /* not JSON */ }
    throw new ApiError(res.status, String(detail));
  }
  return (await res.json()) as T;
}

export function qs(params: Record<string, string | number | null | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") u.set(k, String(v));
  const s = u.toString();
  return s ? `?${s}` : "";
}

export async function downloadCsv(path: string, filename: string): Promise<void> {
  const res = await fetch(path, { headers: token ? { Authorization: `Bearer ${token}` } : {}, credentials: "omit" });
  if (!res.ok) throw new ApiError(res.status, "export failed");
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

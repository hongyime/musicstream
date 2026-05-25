// musicstream/frontend/src/services/api.ts
//
// API client. Audit #22: mutating endpoints now require Bearer auth on the
// daemon (DAEMON_API_TOKEN). The token is read from sessionStorage so it
// survives within a tab but isn't persisted to disk like localStorage —
// closing the tab clears it. The user pastes the token into the dashboard
// once per session via a small prompt UI.
//
// We deliberately do NOT bake the token into env at build time:
//  - Vite-style import.meta.env values get inlined into the bundle and
//    served as plain text from /static/assets — anyone who can hit the
//    dashboard can read the token.
//  - Per-deploy rotation of the build artefact is heavyweight.
// Storing the token in sessionStorage means an operator with the token
// can use the dashboard, but the bundle itself contains no secret.

export interface ApiResponse<T> {
  data: T;
  error: string | null;
  meta: Record<string, any>;
}

const API_BASE = '/api/musicstream';
const TOKEN_STORAGE_KEY = 'musicstream.daemonToken';

export function getDaemonToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setDaemonToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
    else sessionStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // sessionStorage can throw in private browsing modes; degrade gracefully.
  }
}

export async function fetchApi<T>(
  endpoint: string,
  options?: RequestInit,
): Promise<T> {
  const headers = new Headers(options?.headers || {});
  // Attach auth header on every call. The daemon ignores it on read-only
  // GETs but requires it on POSTs; sending it on both keeps the client
  // simple and lets a future read-only ACL refactor work without a UI
  // change.
  const token = getDaemonToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);

  const response = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    headers,
  });

  // 401/403/503 from the auth gate need to surface as actionable errors.
  if (response.status === 401 || response.status === 403) {
    throw new Error(
      'Daemon rejected your token (HTTP ' +
        response.status +
        '). Re-enter it via the dashboard token prompt.',
    );
  }
  if (response.status === 503) {
    // Could be auth not configured, or the daemon is starting up. Surface
    // the body so the user sees the real reason.
    let detail = 'Daemon unavailable (503).';
    try {
      const body = await response.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }

  let result: ApiResponse<T>;
  try {
    result = (await response.json()) as ApiResponse<T>;
  } catch (e) {
    throw new Error(`HTTP ${response.status}: response was not JSON`);
  }

  if (!response.ok || result.error) {
    throw new Error(result.error || `HTTP error! status: ${response.status}`);
  }

  return result.data;
}

export const musicstreamService = {
  getStats: () =>
    fetchApi<{
      total_tracks: number;
      downloaded: number;
      pending: number;
      failed: number;
      active: number;
      progress_pct: number;
    }>('/stats'),

  getTracks: (status: string) =>
    fetchApi<any[]>(`/tracks?status=${encodeURIComponent(status)}`),

  getMetrics: () =>
    fetchApi<
      {
        id: string;
        method: string;
        success: number;
        fail: number;
        total: number;
        rate: number;
      }[]
    >('/metrics'),

  getReport: () => fetchApi<any>('/report'),

  resetFailed: () => fetchApi<any>('/tracks/reset-failed', { method: 'POST' }),

  sync: () => fetchApi<any>('/sync', { method: 'POST' }),

  runIntegrityCheck: () => fetchApi<any>('/integrity', { method: 'POST' }),

  getAuthStatus: () =>
    fetchApi<{
      status: 'authenticated' | 'needs_auth' | 'missing_config';
      client_id: string | null;
      redirect_uri: string;
    }>('/auth/status'),
};

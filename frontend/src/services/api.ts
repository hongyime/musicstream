// musicstream/frontend/src/services/api.ts
//
// API client. Audit #22: mutating endpoints require Bearer auth on the
// daemon (DAEMON_API_TOKEN).  The token is stored in localStorage so it
// survives tab close / browser restart for operator convenience — paste
// once, use forever (until the operator hits Clear or rotates the token
// server-side).
//
// Trade-off accepted: localStorage IS readable by any XSS payload that
// runs in this origin.  Mitigations:
//  - This dashboard ships zero user-generated content (no comments,
//    no playlists from third parties, no rich text).  XSS surface is
//    effectively the operator's own browser extensions.
//  - The token only authorises the SAME operator's daemon, accessible
//    only over loopback / Tailscale.  Even a leaked token from this
//    origin can't be used from the public internet.
//  - The token rotates the moment the operator regenerates DAEMON_API_TOKEN
//    in .env and recreates the daemon container; the stale localStorage
//    value silently 401s and the TokenPrompt re-appears.
// Earlier audit hardening used sessionStorage — switched to localStorage
// at user request to avoid re-pasting after every OAuth redirect chain.
//
// We deliberately do NOT bake the token into env at build time:
//  - Vite-style import.meta.env values get inlined into the bundle and
//    served as plain text from /static/assets — anyone who can hit the
//    dashboard can read the token.
//  - Per-deploy rotation of the build artefact is heavyweight.

export interface ApiResponse<T> {
  data: T;
  error: string | null;
  meta: Record<string, any>;
}

const API_BASE = '/api/musicstream';
const TOKEN_STORAGE_KEY = 'musicstream.daemonToken';

export function getDaemonToken(): string | null {
  try {
    // Read from localStorage first (durable), then fall back to
    // sessionStorage so any token saved under the previous policy keeps
    // working until the user explicitly clears+re-enters it.
    const stored = localStorage.getItem(TOKEN_STORAGE_KEY);
    if (stored) return stored;
    return sessionStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setDaemonToken(token: string | null): void {
  try {
    if (token) {
      localStorage.setItem(TOKEN_STORAGE_KEY, token);
      // Drop any sessionStorage copy left over from older builds so
      // there's only one source of truth.
      sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    } else {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
      sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  } catch {
    // localStorage can throw in private-browsing modes or when the
    // origin's storage quota is exhausted; degrade gracefully.
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
      token_hours_left?: number | null;
      token_degraded?: boolean;
    }>('/auth/status'),

  getArtworkReport: () => fetch('/api/artwork-report', {
    headers: {
      ...(getDaemonToken() ? { 'Authorization': `Bearer ${getDaemonToken()}` } : {})
    }
  }).then(res => res.json()),

  refreshArtwork: (mode: string = "missing", limit: number = 10, dryRun: number = 0) => fetch(`/api/artwork-refresh?mode=${mode}&limit=${limit}&dry_run=${dryRun}`, {
    method: 'POST',
    headers: {
      ...(getDaemonToken() ? { 'Authorization': `Bearer ${getDaemonToken()}` } : {})
    }
  }).then(res => res.json()),

  // ── §W3 T24/T25: Library search ──────────────────────────────────────
  getLibrary: (params: {
    q?: string; artist?: string; album?: string; format?: string;
    status?: string; page?: number; page_size?: number;
  }) => {
    const usp = new URLSearchParams();
    if (params.q) usp.set('q', params.q);
    if (params.artist) usp.set('artist', params.artist);
    if (params.album) usp.set('album', params.album);
    if (params.format) usp.set('format', params.format);
    if (params.status) usp.set('status', params.status);
    usp.set('page', String(params.page ?? 1));
    usp.set('page_size', String(params.page_size ?? 50));
    return fetchApi<{
      items: {
        id: number; title: string; artist: string; album: string | null;
        status: string; format: string | null; blocked: boolean;
        duration_ms: number | null; file_path: string | null;
      }[];
      total: number; page: number; page_size: number;
    }>(`/library?${usp.toString()}`);
  },

  // ── §W3 T13/T26: block / unblock ─────────────────────────────────────
  blockTrack: (id: number) =>
    fetchApi<{ id: number; blocked: boolean }>(`/tracks/${id}/block`, { method: 'POST' }),
  unblockTrack: (id: number) =>
    fetchApi<{ id: number; blocked: boolean }>(`/tracks/${id}/unblock`, { method: 'POST' }),

  // ── §W3 T23: discover-weekly trigger ──────────────────────────────────
  runDiscoverWeekly: () => fetchApi<any>('/discover-weekly', { method: 'POST' }),
};


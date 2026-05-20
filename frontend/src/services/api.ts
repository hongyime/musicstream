export interface ApiResponse<T> {
  data: T;
  error: string | null;
  meta: Record<string, any>;
}

const API_BASE = '/api/musicstream';

export async function fetchApi<T>(endpoint: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${endpoint}`, options);
  const result: ApiResponse<T> = await response.json();
  
  if (!response.ok || result.error) {
    throw new Error(result.error || `HTTP error! status: ${response.status}`);
  }
  
  return result.data;
}

export const musicstreamService = {
  getStats: () => fetchApi<{
    total_tracks: number;
    downloaded: number;
    pending: number;
    failed: number;
    active: number;
    progress_pct: number;
  }>('/stats'),
  
  getTracks: (status: string) => fetchApi<any[]>(`/tracks?status=${status}`),
  
  getMetrics: () => fetchApi<{
    id: string;
    method: string;
    success: number;
    fail: number;
    total: number;
    rate: number;
  }[]>('/metrics'),
  
  getReport: () => fetchApi<any>('/report'),
  
  resetFailed: () => fetchApi<any>('/tracks/reset-failed', { method: 'POST' }),
  
  sync: () => fetchApi<any>('/sync', { method: 'POST' }),

  runIntegrityCheck: () => fetchApi<any>('/integrity', { method: 'POST' }),
  
  getAuthStatus: () => fetchApi<{
    status: 'authenticated' | 'needs_auth' | 'missing_config';
    client_id: string | null;
    redirect_uri: string;
  }>('/auth/status'),
};

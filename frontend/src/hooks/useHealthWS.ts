import { useState, useEffect, useRef } from 'react';

export interface ServiceHealth {
  service: string;
  status:
    | 'online'
    | 'offline'
    | 'warning'
    | 'error'
    | 'processing'
    | 'idle';
  latency_ms?: number;
  updated_at: string;
}

// Audit #24: rewritten for resilience.
//   - exponential backoff (1s → 2s → 4s → … capped at 30s) instead of a
//     fixed 5s loop that hammers a down server
//   - mounted-flag prevents reconnects after unmount (StrictMode in dev
//     mounts twice; the original code leaked a connection per mount)
//   - onerror no longer races onclose — we let the close handler decide
//     whether to reconnect, otherwise both fire and we double-schedule
//   - parse failures invalidate the message but don't kill the connection
export function useHealthWS() {
  const [healthData, setHealthData] = useState<ServiceHealth[]>([]);
  const [isConnected, setIsConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeout = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const attemptRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    attemptRef.current = 0;

    const computeBackoff = (): number => {
      // 1s, 2s, 4s, 8s, 16s, 30s (cap)
      const base = Math.min(30000, 1000 * Math.pow(2, attemptRef.current));
      // Add ±20% jitter so reconnect storms across many tabs don't sync up.
      const jitter = base * (0.8 + Math.random() * 0.4);
      attemptRef.current += 1;
      return jitter;
    };

    const connect = () => {
      if (!mountedRef.current) return;

      const protocol =
        window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const host = window.location.host;
      const wsUrl = import.meta.env.DEV
        ? `ws://localhost:9079/ws/health`
        : `${protocol}//${host}/ws/health`;

      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl);
      } catch (err) {
        // SecurityError on bad URL etc. — schedule retry.
        console.error('WS construction failed:', err);
        if (mountedRef.current) {
          reconnectTimeout.current = window.setTimeout(
            connect,
            computeBackoff(),
          );
        }
        return;
      }

      wsRef.current = ws;

      ws.onopen = () => {
        if (!mountedRef.current) {
          ws.close();
          return;
        }
        attemptRef.current = 0; // reset backoff on successful connect
        setIsConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (Array.isArray(data)) setHealthData(data);
        } catch (err) {
          console.error('Error parsing WS message:', err);
          // Don't kill the connection on a single malformed frame.
        }
      };

      ws.onerror = (err) => {
        // Just log — the browser will fire onclose right after this and
        // that's where we schedule the reconnect. Calling ws.close() here
        // races onclose and can produce double-scheduled reconnects.
        console.error('WS Error:', err);
      };

      ws.onclose = () => {
        setIsConnected(false);
        if (!mountedRef.current) return;
        const delay = computeBackoff();
        reconnectTimeout.current = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      mountedRef.current = false;
      if (reconnectTimeout.current) {
        clearTimeout(reconnectTimeout.current);
        reconnectTimeout.current = null;
      }
      if (wsRef.current) {
        // Unset handlers before close so a final onclose doesn't try to
        // setState on an unmounted component.
        wsRef.current.onopen = null;
        wsRef.current.onmessage = null;
        wsRef.current.onerror = null;
        wsRef.current.onclose = null;
        try {
          wsRef.current.close();
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
    };
  }, []);

  return { healthData, isConnected };
}

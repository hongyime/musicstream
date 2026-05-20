import { useState, useEffect, useCallback, useRef } from 'react';

export interface ServiceHealth {
  service: string;
  status: 'online' | 'offline' | 'warning' | 'error' | 'processing' | 'idle';
  latency_ms?: number;
  updated_at: string;
}

export function useHealthWS() {
  const [healthData, setHealthData] = useState<ServiceHealth[]>([]);
  const [isConnected, setIsConnected] = useState(false);
  const ws = useRef<WebSocket | null>(null);
  const reconnectTimeout = useRef<number | null>(null);

  const connect = useCallback(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    // In development with Vite, we might need to point to the backend port
    const wsUrl = import.meta.env.DEV 
      ? `ws://localhost:9079/ws/health` 
      : `${protocol}//${host}/ws/health`;

    ws.current = new WebSocket(wsUrl);

    ws.current.onopen = () => {
      console.log('WS Connected');
      setIsConnected(true);
    };

    ws.current.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        setHealthData(data);
      } catch (err) {
        console.error('Error parsing WS message:', err);
      }
    };

    ws.current.onclose = () => {
      console.log('WS Disconnected, reconnecting...');
      setIsConnected(false);
      reconnectTimeout.current = window.setTimeout(() => {
        connect();
      }, 5000);
    };

    ws.current.onerror = (err) => {
      console.error('WS Error:', err);
      ws.current?.close();
    };
  }, []);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimeout.current) clearTimeout(reconnectTimeout.current);
      ws.current?.close();
    };
  }, [connect]);

  return { healthData, isConnected };
}

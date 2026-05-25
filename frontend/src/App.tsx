import { useState, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { 
  LayoutDashboard, 
  Database, 
  AlertCircle, 
  Settings, 
  Clock,
  CheckCircle2,
  AlertTriangle,
  Play,
  RotateCcw,
  Activity
} from 'lucide-react';

import { Sidebar } from './components/Sidebar';
import { Header } from './components/Header';
import { MetricCard } from './components/MetricCard';
import { DataTable } from './components/DataTable';
import { StatusBadge } from './components/StatusBadge';
import { Button } from './components/Button';
import { TokenPrompt } from './components/TokenPrompt';
import { musicstreamService } from './services/api';
import { useHealthWS, type ServiceHealth } from './hooks/useHealthWS';

interface TrackItem {
  id: number;
  title: string;
  artist: string;
  album: string;
  status: string;
  method: string | null;
  updated_at: string | null;
}

interface MetricItem {
  id: string;
  method: string;
  success: number;
  fail: number;
  total: number;
  rate: number;
}

function App() {
  const [activeTab, setActiveTab] = useState('dashboard');
  const queryClient = useQueryClient();
  const { healthData, isConnected } = useHealthWS();

  // Queries
  const { data: stats, isLoading: isStatsLoading } = useQuery({
    queryKey: ['stats'],
    queryFn: musicstreamService.getStats,
    refetchInterval: 5000,
  });

  const { data: tracks, isLoading: isTracksLoading } = useQuery({
    queryKey: ['tracks', activeTab],
    queryFn: () => musicstreamService.getTracks(activeTab === 'failed' ? 'failed' : 'pending'),
    enabled: activeTab === 'pending' || activeTab === 'failed',
  });

  const { data: metrics, isLoading: isMetricsLoading } = useQuery({
    queryKey: ['metrics'],
    queryFn: musicstreamService.getMetrics,
    enabled: activeTab === 'database',
  });

  // Mutations
  const syncMutation = useMutation({
    mutationFn: musicstreamService.sync,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['stats'] }),
  });

  const resetFailedMutation = useMutation({
    mutationFn: musicstreamService.resetFailed,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['stats'] });
      queryClient.invalidateQueries({ queryKey: ['tracks', 'failed'] });
    },
  });

  const integrityMutation = useMutation({
    mutationFn: musicstreamService.runIntegrityCheck,
    onSuccess: () => {
      alert('Integrity check started in background. This will restore the status of your existing MP3s.');
      queryClient.invalidateQueries({ queryKey: ['stats'] });
    },
  });

  // Audit #26: keep stale data on transient errors so the banner does
  // not flicker every refetch.  The banner is binary (authenticated or
  // not) — flashing it draws the operator's eye to a non-event.
  const { data: authStatus } = useQuery({
    queryKey: ['auth-status'],
    queryFn: musicstreamService.getAuthStatus,
    refetchInterval: 10000,
    retry: 1,
    placeholderData: (prev) => prev,
    staleTime: 8000,
  });

  const sidebarItems = [
    { id: 'dashboard', label: 'Dashboard', icon: <LayoutDashboard size={18} /> },
    { id: 'pending', label: 'Pending Queue', icon: <Clock size={18} /> },
    { id: 'failed', label: 'Failed Tracks', icon: <AlertCircle size={18} /> },
    { id: 'database', label: 'Metrics', icon: <Activity size={18} /> },
    { id: 'settings', label: 'Settings', icon: <Settings size={18} /> },
  ];

  const healthTableData = useMemo(() => healthData.map(h => ({ ...h, id: h.service })), [healthData]);

  const renderContent = () => {
    switch (activeTab) {
      case 'dashboard':
        return (
          <>
            {/* Audit #25: token entry sits at the top so a fresh tab can
                authenticate before any other query has a chance to fail. */}
            <div className="col-span-12 mb-4">
              <TokenPrompt
                onTokenChange={() => {
                  // Re-validate everything when the token changes.
                  queryClient.invalidateQueries();
                }}
              />
            </div>
            {authStatus?.status !== 'authenticated' && (
              <div className="col-span-12 mb-6">
                <div className="bg-error/10 border border-error/20 rounded-lg p-6">
                  <div className="flex items-start space-x-4">
                    <div className="p-2 bg-error/20 rounded-lg">
                      <AlertTriangle className="text-error w-6 h-6" />
                    </div>
                    <div className="flex-1">
                      <h3 className="text-lg font-bold text-primary">Spotify Authentication Required</h3>
                      <p className="text-sm text-secondary mt-1">
                        Your Spotify token is missing or expired. To enable library scraping, you must authenticate.
                      </p>
                      
                      <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div className="bg-white/5 p-4 rounded border border-white/10">
                          <span className="text-[10px] uppercase tracking-widest text-muted font-bold">1. Setup Redirect URI</span>
                          <p className="text-xs text-secondary mt-2 mb-2">Add this URL to your Spotify Developer Dashboard:</p>
                          <code className="block w-full bg-black/50 p-2 rounded text-[10px] font-mono text-success break-all select-all">
                            {authStatus?.redirect_uri}
                          </code>
                        </div>
                        <div className="bg-white/5 p-4 rounded border border-white/10 flex flex-col justify-center">
                          <span className="text-[10px] uppercase tracking-widest text-muted font-bold mb-3">2. Log In</span>
                          <form action="/auth/spotify/login" method="POST">
                            <Button variant="primary" className="w-full" icon={<Play size={14} />}>
                              Connect Spotify Account
                            </Button>
                          </form>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}

            <div className="grid grid-cols-12 gap-3 mb-6">
              <div className="col-span-12 md:col-span-3">
                <MetricCard 
                  label="Total tracks" 
                  value={stats?.total_tracks ?? '...'} 
                  icon={<Database size={16} />}
                />
              </div>
              <div className="col-span-12 md:col-span-3">
                <MetricCard 
                  label="Downloaded" 
                  value={stats?.downloaded ?? '...'} 
                  sublabel={`${stats?.progress_pct?.toFixed(1) ?? '0'}% complete`}
                  status="success"
                  icon={<CheckCircle2 size={16} />}
                />
              </div>
              <div className="col-span-12 md:col-span-3">
                <MetricCard 
                  label="Pending" 
                  value={stats?.pending ?? '...'} 
                  status="warning"
                  icon={<Clock size={16} />}
                />
              </div>
              <div className="col-span-12 md:col-span-3">
                <MetricCard 
                  label="Failed" 
                  value={stats?.failed ?? '...'} 
                  status="error"
                  icon={<AlertTriangle size={16} />}
                />
              </div>
            </div>

            <div className="grid grid-cols-12 gap-3">
              <div className="col-span-12 lg:col-span-8">
                <div className="flex items-center justify-between mb-3 px-1">
                  <h2 className="text-xs uppercase tracking-wider text-muted font-bold">Active Processes</h2>
                  <StatusBadge 
                    label={isConnected ? 'Connected' : 'Disconnected'} 
                    status={isConnected ? 'online' : 'offline'} 
                  />
                </div>
                <DataTable<ServiceHealth & { id: string }> 
                  data={healthTableData}
                  columns={[
                    { header: 'Process', accessor: 'service' },
                    { header: 'Status', accessor: (item) => <StatusBadge label={item.status} status={item.status as any} /> },
                    { header: 'Latency', accessor: (item) => `${item.latency_ms ?? 0}ms` },
                    { header: 'Updated', accessor: (item) => new Date(item.updated_at).toLocaleTimeString() },
                  ]}
                  maxHeight="400px"
                  emptyMessage="No active processes found."
                />
              </div>
              
              <div className="col-span-12 lg:col-span-4">
                <h2 className="text-xs uppercase tracking-wider text-muted font-bold mb-3 px-1">System Health</h2>
                <div className="card p-4 space-y-4">
                  {healthData.map((service) => (
                    <div key={service.service} className="flex items-center justify-between p-2 rounded hover:bg-white/5 transition-colors">
                      <div className="flex flex-col">
                        <span className="text-sm font-medium">{service.service}</span>
                        <span className="text-[10px] text-muted font-mono">{new Date(service.updated_at).toLocaleTimeString()}</span>
                      </div>
                      <StatusBadge label={service.status} status={service.status as any} />
                    </div>
                  ))}
                  {healthData.length === 0 && <div className="text-center py-8 text-muted text-sm italic">Waiting for telemetry...</div>}
                </div>
              </div>
            </div>
          </>
        );

      case 'pending':
      case 'failed':
        return (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-xs uppercase tracking-wider text-muted font-bold">
                {activeTab === 'failed' ? 'Tracks requiring attention' : 'Upcoming downloads'}
              </h2>
              {activeTab === 'failed' && (
                <Button 
                  variant="danger" 
                  size="sm" 
                  icon={<RotateCcw size={14} />}
                  onClick={() => {
                    if (confirm('Reset all failed tracks to pending?')) {
                      resetFailedMutation.mutate();
                    }
                  }}
                  isLoading={resetFailedMutation.isPending}
                >
                  Reset All Failed
                </Button>
              )}
            </div>
            <DataTable<TrackItem> 
              data={tracks ?? []}
              isLoading={isTracksLoading}
              columns={[
                { header: 'Title', accessor: 'title', className: 'text-primary font-medium' },
                { header: 'Artist', accessor: 'artist' },
                { header: 'Status', accessor: (item) => <span className="capitalize">{item.status.replace('_', ' ')}</span> },
                { header: 'Method', accessor: (item) => <span className="text-xs opacity-60">{item.method || '-'}</span> },
                { header: 'Updated', accessor: (item) => item.updated_at ? new Date(item.updated_at).toLocaleString() : '-' },
              ]}
              maxHeight="calc(100vh - 250px)"
            />
          </div>
        );

      case 'database':
        return (
          <div className="space-y-4">
            <h2 className="text-xs uppercase tracking-wider text-muted font-bold px-1">Tier Performance (All-Time)</h2>
            <DataTable<MetricItem> 
              data={metrics ?? []}
              isLoading={isMetricsLoading}
              columns={[
                { header: 'Tier Method', accessor: 'method', className: 'text-primary font-bold' },
                { header: 'Success', accessor: 'success', className: 'text-success' },
                { header: 'Fail', accessor: 'fail', className: 'text-error' },
                { header: 'Total', accessor: 'total' },
                { header: 'Success Rate', accessor: (item) => (
                  <div className="flex items-center space-x-2 w-full">
                    <div className="flex-1 h-1.5 bg-white/5 rounded-full overflow-hidden">
                      <div 
                        className={`h-full ${item.rate > 80 ? 'bg-success' : item.rate > 40 ? 'bg-warning' : 'bg-error'}`}
                        style={{ width: `${item.rate}%` }}
                      />
                    </div>
                    <span className="w-10 text-right">{item.rate}%</span>
                  </div>
                )},
              ]}
            />
          </div>
        );

      case 'settings':
        return (
          <div className="max-w-2xl">
            <h2 className="text-xs uppercase tracking-wider text-muted font-bold mb-4">Integrations</h2>
            <div className="card p-6 space-y-6">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-lg font-bold">Spotify Authentication</h3>
                  <p className="text-sm text-secondary mt-1">Connect your account to enable library scraping and high-quality streaming.</p>
                </div>
                <form action="/auth/spotify/login" method="POST">
                  <Button variant="primary" icon={<Play size={14} />}>
                    Connect Spotify
                  </Button>
                </form>
              </div>
              
              <div className="pt-6 border-t border-border">
                <h3 className="text-sm uppercase tracking-widest text-muted font-bold mb-4">Maintenance</h3>
                <div className="flex items-center justify-between">
                  <div>
                    <h4 className="text-md font-bold">Integrity Check</h4>
                    <p className="text-xs text-secondary mt-1">Scan disk and restore database status for existing MP3 files.</p>
                  </div>
                  <Button 
                    variant="ghost" 
                    icon={<Database size={14} />}
                    onClick={() => integrityMutation.mutate()}
                    isLoading={integrityMutation.isPending}
                  >
                    Run Integrity Check
                  </Button>
                </div>
              </div>

              <div className="pt-6 border-t border-border">
                <h3 className="text-sm uppercase tracking-widest text-muted font-bold mb-4">Environment Status</h3>
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-1">
                    <span className="text-xs text-muted">Daemon Version</span>
                    <p className="text-sm font-mono">v3.0.0-react-ops</p>
                  </div>
                  <div className="space-y-1">
                    <span className="text-xs text-muted">Node Environment</span>
                    <p className="text-sm font-mono">Production</p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        );
      
      default:
        return null;
    }
  };

  return (
    <div className="flex min-h-screen bg-background text-primary">
      <Sidebar 
        items={sidebarItems} 
        activeId={activeTab} 
        onItemClick={setActiveTab} 
      />
      
      <main className="flex-1 ml-56 p-8 overflow-auto">
        <Header 
          title={sidebarItems.find(i => i.id === activeTab)?.label || 'System'} 
          subtitle={activeTab === 'dashboard' ? "Real-time daemon monitoring and control" : undefined}
          lastUpdated={new Date().toLocaleTimeString()}
          onRefresh={() => queryClient.invalidateQueries()}
          isLoading={isStatsLoading}
          actions={
            activeTab === 'dashboard' && (
              <Button 
                variant="primary" 
                size="sm" 
                icon={<Play size={14} />}
                onClick={() => syncMutation.mutate()}
                isLoading={syncMutation.isPending}
              >
                Trigger Sync
              </Button>
            )
          }
        />

        {renderContent()}
      </main>
    </div>
  );
}

export default App;

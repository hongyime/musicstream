import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Image, ExternalLink, RefreshCw } from 'lucide-react';
import { musicstreamService } from '../services/api';
import { Button } from './Button';
import { StatusBadge } from './StatusBadge';

export const ArtworkCard: React.FC = () => {
  const queryClient = useQueryClient();

  const { data: report, isLoading } = useQuery({
    queryKey: ['artworkReport'],
    queryFn: musicstreamService.getArtworkReport,
  });

  const refreshMutation = useMutation({
    mutationFn: () => musicstreamService.refreshArtwork('missing', 10, 0),
    onSuccess: () => {
      alert('Artwork refresh triggered successfully.');
      queryClient.invalidateQueries({ queryKey: ['artworkReport'] });
    },
    onError: (error: any) => {
      alert(`Failed to trigger artwork refresh: ${error.message}`);
    }
  });

  return (
    <div className="card p-4 space-y-4 h-full flex flex-col justify-between">
      <div>
        <div className="flex justify-between items-start mb-4">
          <div className="flex items-center space-x-2">
            <Image size={18} className="text-muted" />
            <h2 className="text-sm uppercase tracking-wider text-muted font-bold">Artwork Status</h2>
          </div>
          {report?.summary?.artwork_health && (
            <StatusBadge 
              label={report.summary.artwork_health} 
              status={
                report.summary.artwork_health === 'healthy' ? 'online' 
                : report.summary.artwork_health === 'degraded' ? 'warning' 
                : 'error'
              } 
            />
          )}
        </div>

        {isLoading ? (
          <div className="text-sm text-muted animate-pulse">Loading artwork report...</div>
        ) : report ? (
          <div className="space-y-3">
            <div className="flex justify-between items-center text-sm">
              <span className="text-secondary">Database Coverage:</span>
              <span className="font-mono text-primary font-bold">
                {report.database?.coverage_percentage ?? 0}%
              </span>
            </div>
            <div className="flex justify-between items-center text-sm">
              <span className="text-secondary">Missing by Album:</span>
              <span className="font-mono text-primary">
                {report.missing_by_album?.length ?? 0}
              </span>
            </div>
            <div className="flex justify-between items-center text-sm">
              <span className="text-secondary">Missing by Artist:</span>
              <span className="font-mono text-primary">
                {report.missing_by_artist?.length ?? 0}
              </span>
            </div>
          </div>
        ) : (
          <div className="text-sm text-error">Failed to load report.</div>
        )}
      </div>

      <div className="pt-4 border-t border-white/10 flex items-center justify-between mt-auto">
        <a 
          href="/api/artwork-report" 
          target="_blank" 
          rel="noopener noreferrer"
          className="text-xs text-info hover:underline flex items-center"
        >
          <ExternalLink size={12} className="mr-1" />
          Raw Report
        </a>
        <Button 
          variant="ghost" 
          size="sm" 
          icon={<RefreshCw size={14} />}
          onClick={() => refreshMutation.mutate()}
          isLoading={refreshMutation.isPending}
        >
          Refresh Missing
        </Button>
      </div>
    </div>
  );
};

import React from 'react';

type StatusType = 'online' | 'offline' | 'warning' | 'error' | 'processing' | 'idle';

interface StatusBadgeProps {
  label: string;
  status: StatusType;
}

export const StatusBadge: React.FC<StatusBadgeProps> = ({ label, status }) => {
  const statusColors = {
    online: 'bg-success',
    offline: 'bg-muted',
    warning: 'bg-warning',
    error: 'bg-error',
    processing: 'bg-info animate-pulse',
    idle: 'bg-secondary',
  };

  return (
    <div className="inline-flex items-center space-x-2 px-2.5 py-0.5 rounded-full border border-white/10 bg-white/5">
      <span className={`w-2 h-2 rounded-full ${statusColors[status]}`} />
      <span className="text-[11px] font-medium uppercase tracking-tight text-secondary">
        {label}
      </span>
    </div>
  );
};

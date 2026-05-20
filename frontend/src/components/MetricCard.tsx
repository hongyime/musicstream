import React from 'react';

interface MetricCardProps {
  label: string;
  value: string | number;
  sublabel?: string;
  trend?: {
    value: number;
    isUp: boolean;
  };
  icon?: React.ReactNode;
  status?: 'success' | 'error' | 'warning' | 'info';
}

export const MetricCard: React.FC<MetricCardProps> = ({
  label,
  value,
  sublabel,
  trend,
  icon,
  status,
}) => {
  const statusColors = {
    success: 'text-success',
    error: 'text-error',
    warning: 'text-warning',
    info: 'text-info',
  };

  return (
    <div className="card p-4 flex flex-col justify-between min-h-[100px]">
      <div className="flex justify-between items-start">
        <span className="text-xs uppercase tracking-wider text-muted font-medium">
          {label}
        </span>
        {icon && <div className="text-muted">{icon}</div>}
      </div>
      <div className="mt-2">
        <div className={`text-2xl font-bold ${status ? statusColors[status] : 'text-primary'}`}>
          {value}
        </div>
        {(sublabel || trend) && (
          <div className="flex items-center mt-1 space-x-2">
            {trend && (
              <span className={`text-xs ${trend.isUp ? 'text-success' : 'text-error'}`}>
                {trend.isUp ? '↑' : '↓'} {Math.abs(trend.value)}%
              </span>
            )}
            {sublabel && <span className="text-xs text-muted">{sublabel}</span>}
          </div>
        )}
      </div>
    </div>
  );
};

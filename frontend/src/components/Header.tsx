import React from 'react';
import { RefreshCw } from 'lucide-react';
import { Button } from './Button';

interface HeaderProps {
  title: string;
  subtitle?: string;
  lastUpdated?: string;
  onRefresh?: () => void;
  isLoading?: boolean;
  actions?: React.ReactNode;
}

export const Header: React.FC<HeaderProps> = ({
  title,
  subtitle,
  lastUpdated,
  onRefresh,
  isLoading,
  actions,
}) => {
  return (
    <header className="flex items-center justify-between mb-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-primary">{title}</h1>
        {subtitle && <p className="text-sm text-secondary mt-1">{subtitle}</p>}
      </div>
      
      <div className="flex items-center space-x-4">
        {lastUpdated && (
          <div className="text-right hidden sm:block">
            <div className="text-[10px] uppercase tracking-wider text-muted font-medium">Last Updated</div>
            <div className="text-xs text-secondary font-mono">{lastUpdated}</div>
          </div>
        )}
        
        <div className="flex items-center space-x-2">
          {onRefresh && (
            <Button
              variant="ghost"
              size="sm"
              onClick={onRefresh}
              isLoading={isLoading}
              icon={<RefreshCw className={`w-3.5 h-3.5 ${isLoading ? 'animate-spin' : ''}`} />}
            >
              Refresh
            </Button>
          )}
          {actions}
        </div>
      </div>
    </header>
  );
};

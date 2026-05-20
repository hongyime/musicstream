import React from 'react';
import { Music, RefreshCw } from 'lucide-react';

interface SidebarItem {
  id: string;
  label: string;
  icon: React.ReactNode;
  active?: boolean;
}

interface SidebarProps {
  items: SidebarItem[];
  onItemClick: (id: string) => void;
  activeId: string;
}

export const Sidebar: React.FC<SidebarProps> = ({ items, onItemClick, activeId }) => {
  return (
    <aside className="w-56 h-screen border-r border-border bg-surface flex flex-col fixed left-0 top-0">
      <div className="p-6">
        <div className="flex items-center space-x-3 mb-8">
          <div className="w-8 h-8 rounded bg-white flex items-center justify-center">
            <Music className="text-black w-5 h-5" />
          </div>
          <span className="font-bold text-lg tracking-tight">MUSICSTREAM</span>
        </div>
        
        <nav className="space-y-1">
          {items.map((item) => (
            <button
              key={item.id}
              onClick={() => onItemClick(item.id)}
              className={`w-full flex items-center space-x-3 px-3 py-2 rounded-md transition-colors ${
                activeId === item.id
                  ? 'bg-white text-black'
                  : 'text-secondary hover:bg-white/5 hover:text-primary'
              }`}
            >
              <span className={activeId === item.id ? 'text-black' : 'text-muted'}>
                {item.icon}
              </span>
              <span className="text-sm font-medium">{item.label}</span>
            </button>
          ))}
        </nav>
      </div>
      
      <div className="mt-auto p-4 border-t border-border">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10px] uppercase tracking-wider text-muted">System Status</span>
          <div className="w-2 h-2 rounded-full bg-success shadow-[0_0_8px_rgba(34,197,94,0.5)]" />
        </div>
        <div className="text-[11px] text-muted flex items-center">
          <RefreshCw className="w-3 h-3 mr-2 animate-spin-slow" />
          Live updates enabled
        </div>
      </div>
    </aside>
  );
};

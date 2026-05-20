import React from 'react';

interface Column<T> {
  header: string;
  accessor: keyof T | ((item: T) => React.ReactNode);
  className?: string;
}

interface DataTableProps<T> {
  data: T[];
  columns: Column<T>[];
  maxHeight?: string;
  isLoading?: boolean;
  emptyMessage?: string;
}

export function DataTable<T extends { id: string | number }>({
  data,
  columns,
  maxHeight,
  isLoading,
  emptyMessage = 'No data available',
}: DataTableProps<T>) {
  return (
    <div className="card overflow-hidden flex flex-col">
      <div 
        className="overflow-auto" 
        style={{ maxHeight: maxHeight || 'none' }}
      >
        <table className="w-full text-left border-collapse">
          <thead className="sticky top-0 bg-surface z-10">
            <tr className="border-bottom border-border">
              {columns.map((col, i) => (
                <th 
                  key={i} 
                  className="px-4 py-3 text-xs uppercase tracking-wider text-muted font-medium border-b border-border"
                >
                  {col.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {isLoading ? (
              Array.from({ length: 3 }).map((_, i) => (
                <tr key={i} className="animate-pulse">
                  {columns.map((_, j) => (
                    <td key={j} className="px-4 py-4">
                      <div className="h-4 bg-white/5 rounded w-full" />
                    </td>
                  ))}
                </tr>
              ))
            ) : data.length === 0 ? (
              <tr>
                <td 
                  colSpan={columns.length} 
                  className="px-4 py-10 text-center text-muted text-sm"
                >
                  {emptyMessage}
                </td>
              </tr>
            ) : (
              data.map((item) => (
                <tr key={item.id} className="hover:bg-white/5 transition-colors group">
                  {columns.map((col, i) => (
                    <td 
                      key={i} 
                      className={`px-4 py-3 text-sm font-mono text-secondary group-hover:text-primary ${col.className || ''}`}
                    >
                      {typeof col.accessor === 'function' 
                        ? col.accessor(item) 
                        : (item[col.accessor] as React.ReactNode)}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

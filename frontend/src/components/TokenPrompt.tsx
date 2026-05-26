import { useState, useEffect } from 'react';
import { KeyRound, Check, X } from 'lucide-react';
import { getDaemonToken, setDaemonToken } from '../services/api';

/**
 * Audit #25: Token entry UI for the daemon's Bearer-auth gate.
 *
 * Stored in localStorage (durable across tab close + browser restart) — see
 * api.ts header for the security trade-off rationale and why we don't bake
 * it into the bundle.  Exposes a small bar at the top of the dashboard that:
 *   - shows whether a token is currently set
 *   - lets the operator paste/replace it
 *   - lets them clear it
 *
 * On change we call onTokenChange so the parent can invalidate queries
 * (so a fresh token immediately re-fetches data that was rejected by the
 * old one).
 */
export interface TokenPromptProps {
  onTokenChange?: (hasToken: boolean) => void;
}

export function TokenPrompt({ onTokenChange }: TokenPromptProps) {
  const [hasToken, setHasToken] = useState<boolean>(() => !!getDaemonToken());
  const [editing, setEditing] = useState<boolean>(!hasToken);
  const [draft, setDraft] = useState<string>('');

  useEffect(() => {
    onTokenChange?.(hasToken);
  }, [hasToken, onTokenChange]);

  const handleSave = () => {
    const trimmed = draft.trim();
    if (!trimmed) return;
    setDaemonToken(trimmed);
    setHasToken(true);
    setDraft('');
    setEditing(false);
  };

  const handleClear = () => {
    setDaemonToken(null);
    setHasToken(false);
    setEditing(true);
  };

  if (!editing && hasToken) {
    return (
      <div className="flex items-center justify-between bg-success/10 border border-success/20 rounded-lg p-3 text-sm">
        <div className="flex items-center space-x-2">
          <Check size={16} className="text-success" />
          <span className="text-secondary">Daemon token configured (this browser)</span>
        </div>
        <div className="flex items-center space-x-2">
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="text-xs text-secondary hover:text-primary underline"
          >
            Replace
          </button>
          <button
            type="button"
            onClick={handleClear}
            className="text-xs text-error hover:underline"
          >
            Clear
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-warning/10 border border-warning/20 rounded-lg p-4 space-y-2">
      <div className="flex items-center space-x-2">
        <KeyRound size={16} className="text-warning" />
        <span className="text-sm font-medium text-primary">
          Daemon API token required
        </span>
      </div>
      <p className="text-xs text-secondary">
        The daemon protects mutating endpoints with <code>DAEMON_API_TOKEN</code>.
        Paste it here — it is stored in <code>localStorage</code> so it persists
        across tab close and browser restart.  Click <strong>Clear</strong> below
        to remove it from this browser.  It is never sent anywhere except this
        daemon.
      </p>
      <div className="flex items-center space-x-2">
        <input
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') handleSave();
          }}
          placeholder="Paste DAEMON_API_TOKEN…"
          className="flex-1 bg-bg border border-border rounded px-3 py-1.5 text-sm font-mono"
        />
        <button
          type="button"
          onClick={handleSave}
          disabled={!draft.trim()}
          className="px-3 py-1.5 bg-accent text-white rounded text-sm disabled:opacity-50"
        >
          Save
        </button>
        {hasToken && (
          <button
            type="button"
            onClick={() => {
              setEditing(false);
              setDraft('');
            }}
            className="p-1.5 text-secondary hover:text-primary"
            aria-label="Cancel"
          >
            <X size={16} />
          </button>
        )}
      </div>
    </div>
  );
}

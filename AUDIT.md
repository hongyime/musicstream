# AUDIT.md — musicstream

Generated: 20260524

## 0. FILESYSTEM HEALTH REPORT
No corrupted or orphaned files detected in tracked content.

## 1. MASTER FEATURE MAP
| File | Size |
|------|------|
| exchange_spotify_token.py | 1385 bytes |
| frontend/eslint.config.js | 591 bytes |
| frontend/index.html | 360 bytes |
| frontend/postcss.config.js | 80 bytes |
| frontend/src/App.css | 2891 bytes |
| frontend/src/App.tsx | 15795 bytes |
| frontend/src/components/Button.tsx | 1447 bytes |
| frontend/src/components/DataTable.tsx | 2612 bytes |
| frontend/src/components/Header.tsx | 1493 bytes |
| frontend/src/components/MetricCard.tsx | 1462 bytes |
| frontend/src/components/Sidebar.tsx | 2120 bytes |
| frontend/src/components/StatusBadge.tsx | 806 bytes |
| frontend/src/hooks/useHealthWS.ts | 1783 bytes |
| frontend/src/index.css | 270 bytes |
| frontend/src/main.tsx | 525 bytes |
| frontend/src/services/api.ts | 1418 bytes |
| frontend/tailwind.config.js | 604 bytes |
| frontend/vite.config.ts | 161 bytes |
| main.py | 14962 bytes |
| migrations/env.py | 3690 bytes |
| migrations/versions/0001_initial_schema.py | 8969 bytes |
| migrations/versions/0002_track_sources_cascade.py | 1725 bytes |
| spotify_cli_login.py | 1794 bytes |
| src/__init__.py | 30 bytes |
| src/core/config.py | 2098 bytes |
| src/core/tasks.py | 16809 bytes |
| src/daemon.py | 18292 bytes |
| src/db.py | 6533 bytes |
| src/discovery/__init__.py | 25 bytes |
| src/discovery/listenbrainz.py | 16841 bytes |
| src/discovery/plex_playlists.py | 15296 bytes |
| src/exceptions.py | 2862 bytes |
| src/ingestion/__init__.py | 25 bytes |
| src/ingestion/artwork_checker.py | 5322 bytes |
| src/ingestion/downloader.py | 55521 bytes |
| src/ingestion/organiser.py | 16613 bytes |
| src/ingestion/scraper.py | 37047 bytes |
| src/ingestion/spotify_auth.py | 2780 bytes |
| src/ingestion/tagger.py | 37728 bytes |
| src/integrity/__init__.py | 25 bytes |
| ... | +30 more files |

Total: 70 source files | Language: Python | Tests: pytest

## 2. RECONCILIATION SUMMARY
Documentation describes project purpose. Code implements described features.
Production Readiness: N/A (personal project)

## 3-5. GAPS / GHOSTS / DRIFT
No critical gaps identified between documentation and implementation.

## 6. DATA INTEGRITY
N/A — no databases.

## 7. CODE QUALITY FINDINGS
No P0/P1 issues identified. See security_audit.md for detailed SAST/SCA results.

## 8. STRUCTURAL REORGANIZATION
Large project (70 files). Structure follows Python conventions.

## 9. PRODUCTION READINESS CHECKLIST
N/A — personal/educational project scope.

## 10. REMEDIATION ROADMAP
No critical remediation actions required. Ongoing dependency monitoring via Dependabot.
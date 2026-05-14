# musicstream SPEC

## §S Status

P1 ✓ complete. P2 started.

## §D Done (P1)

- `POST /admin/validate-invalid-tracks` ✓
- `POST /admin/cleanup-invalid-tracks` ✓
- `GET /api/artwork-report` ✓
- `src/ingestion/artwork_checker.py` — embedded art check helper ✓
- `tests/conftest.py` — centralized fixtures ✓
- baseline test fixes: `MAX_CONCURRENT`, fixture isolation, SpotiFLAC cond. tests ✓
- `POST /api/refresh-artwork` ✓ (P2 started)

## §I Interfaces

```
api: POST /admin/validate-invalid-tracks → {checked, updated, marked, errors}
api: POST /admin/cleanup-invalid-tracks → {deleted}   ! dry_run?=1
api: GET  /api/artwork-report           → {tracks_with_cover_art_url, tracks_without_cover_art_url, tracks_without_embedded_art, missing_by_album[], missing_by_artist[]}
api: POST /api/refresh-artwork          → mode=missing|all, limit=n, dry_run?=1
```

## §V Invariants

```
V1: validate-invalid → cleanup; never cleanup without prior validate pass
V2: cleanup predicate ! only rows with explicit invalid marker
V3: spotdl HTTP mode behind flag; CLI fallback always available
V4: ∀ artwork refresh → dry_run first option; no silent overwrites
V5: ∀ batch spotdl → track↔file mapping by subdir/manifest; ⊥ fuzzy filename match
V6: admin endpoints ? token gate if token configured
```

## §T Tasks

| id  | status | task                                    | cites     |
|-----|--------|-----------------------------------------|-----------|
| T1  | x      | invalid-data validate endpoint          | V1,V2     |
| T2  | x      | invalid-data cleanup endpoint           | V1,V2,V6  |
| T3  | x      | artwork-report endpoint                 | §I        |
| T4  | x      | embedded artwork check helper           | V4        |
| T5  | x      | baseline test fixes                     | §F        |
| T6  | x      | refresh-artwork endpoint                | V4        |
| T7  | .      | integration tests (daemon live)         |           |
| T8  | .      | dashboard artwork card                  | §I        |
| T9  | .      | spotdl batch PoC (behind flag)          | V3,V5     |
| T10 | .      | folder.jpg generator                    | V4        |
| T11 | .      | spotdl HTTP service PoC (behind flag)   | V3        |

## §P Plan

### P2 (next)
- T7: integration tests — start daemon, hit all 4 endpoints
- T8: dashboard `/` → artwork card; link report; button → refresh (manual only; ⊥ auto)
- T9: spotdl batch PoC

### P3
- T10: folder.jpg generator
- T11: spotdl HTTP service PoC

## §F Test Failures → Fixes

| id  | file                        | cause                                      | fix                                                   |
|-----|-----------------------------|--------------------------------------------|-------------------------------------------------------|
| F1  | `tests/test_downloader.py`  | `MAX_CONCURRENT` hardcoded old value       | `int(os.environ.get("MAX_CONCURRENT_WORKERS", "4"))`  |
| F2  | fixture isolation           | session rollback missing; uri collisions   | centralize `tests/conftest.py`; rollback before close |
| F3  | SpotiFLAC tests             | stale vs `ENABLE_TIER1=false`              | `xfail`/`skip` when tier1 disabled                    |
| F4  | scraper mocks               | `spotipy.cache_handler` patch path wrong   | patch import path used by module under test            |

## §A Acceptance

```
A1: invalid rows
  checked_count = actual invalid count in DB
  Spotify exists → metadata patched
  Spotify ⊥ → explicit marker set; optional delete

A2: artwork
  GET /api/artwork-report → stable JSON schema
  POST /api/refresh-artwork updates subset; tags intact
  ⊥ crash on missing/readonly files

A3: tests
  targeted suite green
  `python main.py validate` green

A4: spotdl batch PoC
  ⊥ event-loop regression
  Tier3 throughput > current CLI baseline
  rollback via env flag
```

## §R Risks

| risk                                    | mitigation                              |
|-----------------------------------------|-----------------------------------------|
| Spotify/artwork API rate limits         | flags + dry-run first                   |
| heavy I/O during artwork refresh        | limit=n param; dry_run                  |
| false-positive delete in cleanup        | narrow SQL predicate; V1 gate           |
| batch track→file mapping errors         | subdir/manifest; V5                     |
| spotdl HTTP mode ops burden             | PoC only; CLI always fallback; V3       |

## §E Env

```
env: SPOTDL_SERVICE_URL   ? HTTP mode URL
env: SPOTDL_MODE          ? cli|http  default=cli
env: MAX_CONCURRENT_WORKERS ! int default=4
env: ENABLE_TIER1         ? bool
```

## §N Next

Start T7 (integration tests): start daemon → smoke all 4 new endpoints → then T8 dashboard.

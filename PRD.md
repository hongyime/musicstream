# musicstream — PRD v3.0

**Repository:** github.com/bryanseah234/musicstream
**Version:** 3.0
**Status:** DRAFT
**Date:** 2026-04-22
**Author:** Bryan Seah

---

## 1. Overview

musicstream is a fully self-hosted, autonomous music ecosystem.
It ingests your entire Spotify library into a PostgreSQL database, downloads every
track at the highest available quality (true lossless FLAC via SpotiFLAC where
possible, MP3 320 kbps via yt-dlp as fallback), tags each file to Plex Media Server
standards using MusicBrainz metadata, and serves the library through Plex on a Docker
stack — with all plays scrobbled to ListenBrainz, and new music discovered automatically
via the ListenBrainz Collaborative Filtering API.

The first full import is a one-time slow backfill. Every addition after that is
near-real-time (< 15 minutes from Spotify save to Plex library appearance).

---

## 2. Plain-English Component Glossary

| Component | What it actually does |
|---|---|
| **PostgreSQL** | The single source of truth. Every track you own, its download status, file path, tags, and history lives here. Backup means backing this up — not the audio files. |
| **SpotiFLAC** | Python library that takes a Spotify URL and downloads the audio in true lossless FLAC from Tidal, Qobuz, Amazon Music, or Deezer. No streaming service account required. Primary downloader. |
| **yt-dlp + FFmpeg** | Fallback downloader. Pulls audio from YouTube/SoundCloud and transcodes to MP3 320 kbps. Used only when SpotiFLAC fails all its sources. |
| **spotdl** | Secondary fallback. Independent Python tool with its own YouTube matching algorithm — catches tracks that yt-dlp's YTM resolver misses. |
| **mutagen** | Python library that writes ID3/MP4/FLAC tags directly to audio files (title, artist, album, cover art, etc.). |
| **musicbrainz-api** | MusicBrainz Web Service v2 client. Used to fill in missing metadata (track number, year, cover art, album artist) after Spotify data. |
| **pyacoustid + chromaprint** | Generates an acoustic fingerprint of a downloaded audio file, then looks it up in the AcoustID database to get a confirmed MusicBrainz Recording ID. More reliable than title+artist matching. |
| **Plex Media Server** | Serves your music library to any device. Reads the organised directory on your external HDD and presents it as a browsable, streamable library. |
| **Plexamp (iOS)** | The Plex music client on your iPhone. Streams over Tailscale. |
| **multi-scrobbler** | Free, open-source Docker container. Polls the Plex API every 10 seconds to detect what is playing, then submits each listen to ListenBrainz. **No Plex Pass required.** |
| **ListenBrainz** | Open-source listening history tracker and music recommendation engine. Stores all your plays and generates weekly Collaborative Filtering (CF) recommendations. |
| **MusicBrainz** | Open music encyclopaedia. Provides accurate metadata: correct album names, track numbers, ISRCs, album art via Cover Art Archive. |
| **musicstream daemon** | Your Python service running inside Docker. Orchestrates the full pipeline on schedule (and on-demand), manages the discovery loop, and exposes an HTTP control plane. |
| **Tailscale** | Zero-config VPN mesh. Your iPhone and home server share a private network. No port forwarding. No public IP exposure. |

---

## 3. Key Architectural Decisions

### 3.1 Database: PostgreSQL over SQLite

| Factor | PostgreSQL | SQLite |
|---|---|---|
| Concurrent writes (daemon + integrity checker simultaneously) | ✅ Native | ⚠️ Locking issues |
| Backup mechanism | `pg_dump` → clean SQL file | File copy (risk of corruption mid-write) |
| Docker integration | Official image, named volume | File on host, simpler but fragile |
| Query capability for complex status reports | ✅ Full SQL | ✅ Full SQL |
| Operational overhead | Slightly higher | Lower |

**Decision:** PostgreSQL. The backup story (`pg_dump`) is cleaner and more reliable,
concurrent access matters once the daemon runs background integrity checks alongside
the main pipeline, and the Docker-native deployment is straightforward.

### 3.2 Audio Format Strategy

SpotiFLAC downloads **true lossless FLAC** (typically 24-bit/44.1kHz) from Tidal or
Qobuz. This is strictly better quality than any MP3 encoding. Storage cost is ~25–35MB
per track vs ~8–10MB for MP3 320 kbps.

| Source | Output Format | Quality |
|---|---|---|
| SpotiFLAC (Tidal/Qobuz/Amazon/Deezer) | FLAC | Lossless — best possible |
| yt-dlp (YouTube Music / YouTube) | MP3 320 kbps | Lossy fallback |
| spotdl | MP3 320 kbps | Lossy fallback |
| yt-dlp (SoundCloud) | MP3 320 kbps | Lossy last resort |

The database records `format` per track. Plex natively plays both FLAC and MP3.
All files in the library follow identical directory and filename conventions regardless
of format (see §6.4). There is **no post-processing transcode** — FLAC from SpotiFLAC
stays FLAC; it is never downgraded to MP3.

### 3.3 SpotiFLAC Service Priority

SpotiFLAC tries these streaming services in order until one succeeds:

```
["qobuz", "tidal", "amazon", "deezer", "youtube"]
```

Qobuz is first because it reliably provides 24-bit Hi-Res. YouTube is included as a
last resort within SpotiFLAC's own fallback chain.

---

## 4. System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         EXTERNAL HDD                                │
│  /music/[Album Artist]/[Album (Year)]/[Track]. [Title].[flac|mp3]  │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ bind-mount (read-only for Plex)
        ┌───────────────────────┼───────────────────────┐
        │                       │                       │
   ┌────▼────┐           ┌──────▼──────┐        ┌──────▼──────┐
   │  plex   │           │   daemon    │        │multi-scrobbl│
   │  :32400 │◄──polling─│   :9079     │        │   er :9078  │
   └────┬────┘  sessions │             │        └──────┬──────┘
        │       API      │  scheduler  │               │
        │                │  pipeline   │          scrobbles
        │                │  discovery  │               │
        │                └──────┬──────┘               ▼
        │                       │               ┌──────────────┐
        │                       │ read/write     │ ListenBrainz │
        │                  ┌────▼────┐          └──────┬───────┘
        │                  │postgres │                  │
        │                  │  :5432  │           CF recs│
        │                  └─────────┘                  │
        │                                         ┌─────▼──────┐
        │                              Plex API   │  daemon    │
        └───────────────────────────────────────► │  playlist  │
                                                  │  creator   │
                                                  └────────────┘
```

---

## 5. Repository Structure

```
musicstream/
├── daemon.py              # Main daemon: scheduler + HTTP control plane
├── ingestion/
│   ├── scraper.py         # Spotify playlist + liked songs ingestion (spotipy)
│   ├── downloader.py      # Download orchestrator: SpotiFLAC → yt-dlp → spotdl
│   ├── tagger.py          # ID3/FLAC tagging: Spotify → AcoustID → MusicBrainz
│   └── organiser.py       # Move file to Plex directory structure + update DB
├── discovery/
│   ├── listenbrainz.py    # LB CF recommendation fetcher + backfill
│   └── plex_playlists.py  # Create/update Plex playlists from LB recs
├── integrity/
│   └── checker.py         # Scan external HDD vs DB; requeue missing/corrupt files
├── models.py              # SQLAlchemy ORM models
├── db.py                  # PostgreSQL connection + session factory
├── rate_limiter.py        # Per-service rate limiting with exponential backoff
├── exceptions.py          # Custom exceptions
├── ui.py                  # Rich CLI output
├── main.py                # CLI entrypoint (scrape / download / status / etc.)
├── Dockerfile.daemon      # Daemon container image
├── docker-compose.yml     # Full stack
├── setup.bat              # One-time initialisation (Windows)
├── startup.bat            # Day-to-day operations menu
├── requirements.txt
├── .env.example
├── .gitignore
├── backups/               # pg_dump outputs — git-ignored
└── logs/                  # Rotating log files — git-ignored
```

---

## 6. Database Schema (PostgreSQL)

### 6.1 Core Tables

```sql
-- ── tracks ───────────────────────────────────────────────────────────────────
CREATE TABLE tracks (
    id                  SERIAL PRIMARY KEY,

    -- Spotify identity (source of truth for what you own)
    spotify_uri         TEXT UNIQUE NOT NULL,
    spotify_id          TEXT NOT NULL,
    spotify_album_id    TEXT,
    isrc                TEXT,           -- used for MusicBrainz precise lookup

    -- Metadata (populated from Spotify first, MusicBrainz fills gaps)
    title               TEXT NOT NULL,
    artist              TEXT NOT NULL,  -- TPE1 / comma-separated
    album_artist        TEXT,           -- TPE2: real artist or "Various Artists"
    album               TEXT,
    year                TEXT,           -- 4-digit
    track_number        INTEGER,        -- best-effort, not required
    disc_number         INTEGER,
    duration_ms         INTEGER,
    cover_art_url       TEXT,
    cover_art_source    TEXT DEFAULT 'none',  -- spotify | musicbrainz | none

    -- MusicBrainz
    mb_recording_id     TEXT,
    mb_release_id       TEXT,
    acoustid_id         TEXT,           -- from pyacoustid fingerprint

    -- Download
    status              TEXT DEFAULT 'pending',
    -- pending | resolving | downloading | downloaded | failed | failed_validation | missing

    download_method     TEXT,
    -- spotiflac_qobuz | spotiflac_tidal | spotiflac_amazon | spotiflac_deezer
    -- ytdlp_ytm | ytdlp_yt | spotdl | ytdlp_soundcloud

    format              TEXT,           -- flac | mp3
    file_path           TEXT,           -- absolute host path (external HDD)
    file_size_bytes     BIGINT,
    file_sha256         TEXT,           -- for integrity check; redownload if mismatch
    plex_verified       BOOLEAN DEFAULT FALSE,

    -- Lifecycle
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    last_checked_at     TIMESTAMPTZ    -- last integrity check timestamp
);

CREATE INDEX idx_tracks_status ON tracks(status);
CREATE INDEX idx_tracks_spotify_uri ON tracks(spotify_uri);
CREATE INDEX idx_tracks_isrc ON tracks(isrc);
CREATE INDEX idx_tracks_mb_recording ON tracks(mb_recording_id);

-- ── sources ───────────────────────────────────────────────────────────────────
CREATE TABLE sources (
    id              SERIAL PRIMARY KEY,
    spotify_id      TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    source_type     TEXT NOT NULL,  -- playlist | liked | listenbrainz
    snapshot_id     TEXT,           -- Spotify playlist snapshot_id for change detection
    track_count     INTEGER DEFAULT 0,
    last_scraped_at TIMESTAMPTZ
);

-- ── track_sources (many-to-many) ──────────────────────────────────────────────
CREATE TABLE track_sources (
    track_id    INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    source_id   INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    added_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (track_id, source_id)
);

-- ── lb_recommendations ────────────────────────────────────────────────────────
CREATE TABLE lb_recommendations (
    id              SERIAL PRIMARY KEY,
    recording_mbid  TEXT UNIQUE NOT NULL,
    title           TEXT,
    artist          TEXT,
    score           REAL,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    track_id        INTEGER REFERENCES tracks(id),
    status          TEXT DEFAULT 'pending'
    -- pending | ingested | failed | skipped
);

-- ── download_attempts (per-track failure audit trail) ────────────────────────
CREATE TABLE download_attempts (
    id          SERIAL PRIMARY KEY,
    track_id    INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
    attempted_at TIMESTAMPTZ DEFAULT NOW(),
    method      TEXT,
    error       TEXT,
    success     BOOLEAN DEFAULT FALSE
);

-- ── daemon_runs (history of pipeline executions) ─────────────────────────────
CREATE TABLE daemon_runs (
    id              SERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    run_type        TEXT,   -- scheduled | manual | integrity | discovery
    tracks_scraped  INTEGER DEFAULT 0,
    tracks_downloaded INTEGER DEFAULT 0,
    tracks_failed   INTEGER DEFAULT 0,
    tracks_requeued INTEGER DEFAULT 0,  -- missing files requeued
    notes           TEXT
);
```

### 6.2 Auto-Update Trigger

```sql
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tracks_updated_at
BEFORE UPDATE ON tracks
FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

---

## 7. Ingestion & Download Pipeline

### 7.1 Stage 1 — Spotify Scrape

Tool: `spotipy` with OAuth PKCE (no client secret required).

Scopes: `playlist-read-private playlist-read-collaborative user-library-read`

**Initial backfill (one-time, slow):**
1. Fetch all playlists via `/me/playlists` (paginated, 50 per page).
2. For each playlist: fetch all tracks via `/playlists/{id}/tracks` (paginated, 100
   per page).
3. Fetch all Liked Songs via `/me/tracks` (paginated, 50 per page).
4. Upsert every track into `tracks` with `status = 'pending'`.
5. Record `snapshot_id` per playlist for efficient subsequent change detection.

**Subsequent sync (near-real-time, runs every 15 minutes):**
1. For each source: compare current `snapshot_id` (from Spotify API) against stored
   `snapshot_id`.
2. If unchanged: skip. Cost: 1 lightweight API call per playlist per cycle.
3. If changed: fetch only the new tracks (offset from stored `track_count`).
4. Insert new tracks as `status = 'pending'`. Immediately trigger the download
   pipeline for only the new tracks.

This means a newly-liked Spotify song reaches your Plex library within ~15 minutes
of being saved, even outside a scheduled full-run.

### 7.2 Stage 2 — Download (SpotiFLAC-First with Full Fallback Chain)

Every track with `status = 'pending'` enters the download pipeline.

The daemon processes downloads in parallel batches of 4 tracks (`MAX_CONCURRENT = 4`)
to balance throughput against rate limiting.

```
┌──────────────────────────────────────────────────────────────────┐
│ DOWNLOAD TIER CHAIN                                              │
│                                                                  │
│ Tier 1 — SpotiFLAC                                               │
│   services=["qobuz", "tidal", "amazon", "deezer", "youtube"]    │
│   output: FLAC (lossless) to temp/                               │
│   timeout: 120s per track                                        │
│                          ↓ fail                                  │
│ Tier 2 — yt-dlp + ytmusicapi                                     │
│   query: ytmusicapi songs filter → videos filter → no filter    │
│   output: bestaudio → FFmpeg → MP3 320 kbps to temp/            │
│   duration validation: ±5s vs Spotify duration_ms               │
│                          ↓ fail                                  │
│ Tier 3 — spotdl                                                  │
│   spotdl.download(spotify_uri)                                   │
│   output: MP3 320 kbps to temp/                                  │
│   independent YouTube matching algorithm                         │
│                          ↓ fail                                  │
│ Tier 4 — yt-dlp YouTube direct search                            │
│   ytsearch12: "{title} {artist} audio"                           │
│   ytsearch12: "{title} {artist} official audio"                  │
│   output: MP3 320 kbps to temp/                                  │
│                          ↓ fail                                  │
│ Tier 5 — yt-dlp SoundCloud                                       │
│   scsearch8: "{title} {artist}"                                  │
│   output: MP3 320 kbps to temp/                                  │
│                          ↓ fail (all 5 tiers exhausted)          │
│                                                                  │
│ After 3 complete tier-chain failures: status = 'failed'          │
│ Log to errors.log. Never blocks rest of queue.                   │
└──────────────────────────────────────────────────────────────────┘
```

Each tier attempt is recorded in `download_attempts` with the error message. The
`download_method` column records which tier succeeded.

**Duration validation (Tiers 2–5 only):**
SpotiFLAC inherently resolves the correct track. yt-dlp results are validated:
`|downloaded_duration_s - spotify_duration_ms / 1000| ≤ 5 seconds`
Validation failure triggers retry on the next tier, not an immediate failure.

### 7.3 Stage 3 — Metadata & Tagging

Executed after every successful download, before the file is moved.

Tags are populated in strict priority order per field. The pipeline advances to the
next source only if the current source returns null or empty for that specific field.

| Tag | Frame | Source 1 | Source 2 | Source 3 |
|---|---|---|---|---|
| Title | `TIT2` / `TITLE` | Spotify | MusicBrainz | yt-dlp embed |
| Artist | `TPE1` / `ARTIST` | Spotify | MusicBrainz | yt-dlp embed |
| Album Artist | `TPE2` / `ALBUMARTIST` | *§7.3.1* | MusicBrainz | copy TPE1 |
| Album | `TALB` / `ALBUM` | Spotify | MusicBrainz | yt-dlp embed |
| Year | `TDRC` / `DATE` | Spotify | MusicBrainz | omit |
| Track Number | `TRCK` / `TRACKNUMBER` | Spotify | MusicBrainz | omit |
| Cover Art | `APIC` | Spotify album images | MusicBrainz CAA | omit |

#### 7.3.1 Album Artist Rule (not always "Various Artists")

- If `Spotify.album.album_type == "compilation"` **OR**
  `Spotify.album.artists[0].name == "Various Artists"`:
  → `TPE2 = "Various Artists"`
- Otherwise:
  → `TPE2 = TPE1` (the track's primary artist)

This preserves full **Artist-based sorting** in Plex for the vast majority of tracks.
Only genuine compilation albums land under "Various Artists".

#### 7.3.2 MusicBrainz Lookup

MusicBrainz is queried when any tag field is missing after the Spotify pass.

**Lookup priority:**

1. **ISRC** (most precise — Spotify provides ISRC for ~95% of tracks):
   `GET /ws/2/recording?isrc={isrc}&inc=releases+artists+recordings&fmt=json`

2. **AcoustID fingerprint** (for downloaded file — more reliable than text search):
   - Generate fingerprint: `pyacoustid.fingerprint_file(file_path)`
   - Look up: `acoustid.lookup(api_key, fingerprint, duration)`
   - Returns MusicBrainz Recording ID directly
   - Use Recording ID: `GET /ws/2/recording/{mbid}?inc=releases+artists&fmt=json`

3. **Title + Artist text search** (last resort):
   `GET /ws/2/recording?query=recording:"{title}" AND artist:"{artist}"&fmt=json`

**Rate limiting:** MusicBrainz enforces 1 req/s. Enforced via `rate_limiter.py`
with `musicbrainz` service config. User-Agent header required:
```
User-Agent: musicstream/3.0.0 ( github.com/bryanseah234/musicstream )
```

**Cover art:** Fetched from Cover Art Archive:
`https://coverartarchive.org/release/{mb_release_id}/front-250`

### 7.4 Stage 4 — File Organisation & Move to Plex

After tagging passes, the file is moved from `temp/` to the external HDD:

```
{EXTERNAL_MEDIA_DRIVE}/
└── {Album Artist}/
    └── {Album} ({Year})/       ← Year omitted if TDRC empty
        └── {NN} - {Title}.{ext}  ← NN = zero-padded track number if available
                                     omitted if TRCK empty → just {Title}.{ext}
```

Filename sanitisation: `< > : " / \ | ? *` → `_`. Max 200 chars. Trim leading/trailing
periods and spaces.

On successful move:
- `tracks.file_path` = absolute host path
- `tracks.file_sha256` = `SHA-256(file)`
- `tracks.file_size_bytes` = recorded
- `tracks.status` = `'downloaded'`
- `tracks.format` = `'flac'` or `'mp3'`
- Plex library refresh triggered via `POST http://localhost:32400/library/sections/{id}/refresh`

### 7.5 Stage 5 — File Integrity Check

Runs on daemon startup and on-demand via `startup.bat`.

For every track with `status = 'downloaded'` and a non-null `file_path`:

1. Check file exists at `file_path`.
   - Missing → reset `status = 'pending'`, clear `file_path` and `file_sha256`.
   - Log: `[FILE_MISSING] {title} | {artist} | {path}`
2. If file exists: compute `SHA-256(file)`.
   - Hash mismatch → log `[FILE_CORRUPT]`, reset to `status = 'pending'` for redownload.
3. Requeued tracks re-enter the download pipeline on the next run.

This ensures your DB and external HDD never drift permanently. Any missing or corrupted
file is automatically redownloaded.

---

## 8. ListenBrainz Discovery

### 8.1 Correct API Endpoint

```
GET https://api.listenbrainz.org/1/cf/recommendation/user/{username}/recording
    ?count=100
    &artist_type=top
```

Returns `recording_mbid` values with confidence scores. There is no "playlist" endpoint
for CF recommendations — the above is the correct resource.

### 8.2 Initial Backfill (First Run)

1. Check `lb_recommendations` table — if empty, this is a backfill run.
2. Fetch 200 recommendations (`?count=200`).
3. Insert all as `status = 'pending'` into `lb_recommendations`.
4. For each: fetch metadata from MusicBrainz WS2 using `recording_mbid`.
5. Construct a synthetic `spotify_uri = "mb:{recording_mbid}"` as the unique key.
6. Insert into `tracks` with `source_type = 'listenbrainz'`, `status = 'pending'`.
7. Enter normal download pipeline.

Backfill is slow. It is intentionally isolated from the Spotify pipeline and runs
as a separate background job so it does not block Spotify ingestion.

### 8.3 Ongoing Discovery (Daily Poll)

The daemon polls the LB CF API once every 24 hours:

1. Fetch 100 latest recommendations.
2. Skip any `recording_mbid` already present in `lb_recommendations`.
3. For each new MBID: fetch MusicBrainz metadata → insert track → trigger download.
4. New tracks reach Plex within minutes of being discovered.

### 8.4 Plex Playlist Sync from LB Recommendations

After each discovery batch, the daemon creates or updates a dated Plex playlist:

- Playlist name: `Discovered: {Month} {Year}` (e.g., `Discovered: April 2026`)
- Created via Plex API: `POST /playlists` with the machine identifier and track keys
  of all successfully downloaded LB recommendation tracks from that month.
- Existing playlists are updated (not recreated) when new tracks for that month arrive.

This means Plex shows a new auto-curated playlist each month from your LB discoveries.

---

## 9. Docker Infrastructure

### 9.1 `docker-compose.yml`

```yaml
version: "3.9"

services:

  postgres:
    image: postgres:16-alpine
    container_name: musicstream-postgres
    environment:
      POSTGRES_DB: musicstream
      POSTGRES_USER: musicstream
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U musicstream"]
      interval: 10s
      timeout: 5s
      retries: 5

  plex:
    image: plexinc/pms-docker:latest
    container_name: musicstream-plex
    network_mode: host
    environment:
      - PLEX_CLAIM=${PLEX_CLAIM_TOKEN}
      - ADVERTISE_IP=http://${TAILSCALE_IP}:32400/
      - TZ=Asia/Singapore
    volumes:
      - ${EXTERNAL_MEDIA_DRIVE}:/media:ro
      - ./plex/config:/config
      - ./plex/transcode:/transcode
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:32400/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s

  scrobbler:
    image: foxxmd/multi-scrobbler:latest
    container_name: musicstream-scrobbler
    volumes:
      - ./scrobbler/config:/config
    ports:
      - "9078:9078"
    environment:
      - TZ=Asia/Singapore
    restart: unless-stopped
    depends_on:
      plex:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:9078/health"]
      interval: 30s
      timeout: 5s
      retries: 3

  daemon:
    build:
      context: .
      dockerfile: Dockerfile.daemon
    container_name: musicstream-daemon
    volumes:
      - ${EXTERNAL_MEDIA_DRIVE}:/media
      - ./logs:/app/logs
      - ./backups:/app/backups
      - ./.env:/app/.env:ro
      - ./cookies.txt:/app/cookies.txt:ro
    ports:
      - "9079:9079"
    environment:
      - TZ=Asia/Singapore
      - PYTHONUNBUFFERED=1
      - DATABASE_URL=postgresql://musicstream:${POSTGRES_PASSWORD}@postgres:5432/musicstream
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:9079/health"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s

volumes:
  postgres_data:
```

### 9.2 `Dockerfile.daemon`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y ffmpeg curl libchromaprint-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 9079

CMD ["python", "daemon.py"]
```

`libchromaprint-dev` is required for `pyacoustid` fingerprinting.

### 9.3 multi-scrobbler Configuration (No Plex Pass)

`./scrobbler/config/config.yaml` (generated by `setup.bat`):

```yaml
sources:
  - name: musicstream-plex
    type: plex
    polling:
      interval: 10        # Poll Plex /status/sessions every 10 seconds
    data:
      user: ${PLEX_USERNAME}
      token: ${PLEX_TOKEN}

scrobbles:
  - name: musicstream-lb
    type: listenbrainz
    data:
      token: ${LISTENBRAINZ_TOKEN}
```

**How polling-mode scrobbling works without Plex Pass:**
multi-scrobbler calls `GET http://plex:32400/status/sessions?X-Plex-Token={token}` every
10 seconds. This endpoint is available to all Plex accounts at no cost. When a track's
play progress crosses the 50% threshold, it is submitted to ListenBrainz as a listen.
Latency vs webhook-based scrobbling: ~5–15 seconds. Functionally equivalent.

### 9.4 Daemon HTTP Control Plane (Port 9079)

```
GET  /health          → {"status": "ok", "uptime_s": 3600, "db_tracks": 4200}
GET  /status          → Full run summary: last 5 runs from daemon_runs table
POST /sync            → Immediately trigger full pipeline (used by startup.bat)
POST /integrity       → Immediately trigger file integrity check
POST /discover        → Immediately trigger ListenBrainz discovery cycle
GET  /metrics         → Download success rate, fallback usage, per-tier stats
```

---

## 10. Daemon Architecture

### 10.1 Startup Sequence

```
1. Connect to PostgreSQL — retry 5× with 5s backoff if unavailable
2. Run Alembic migrations (schema auto-upgrade on new deployments)
3. Print startup summary (last run stats, DB counts, log sizes)
4. Run file integrity check (§7.5) — requeue missing/corrupt files
5. Run Spotify incremental sync (check snapshot_ids)
6. Run download pipeline for all pending tracks
7. Run ListenBrainz discovery poll
8. Create automatic DB backup (pg_dump)
9. Enter scheduler loop
```

### 10.2 Scheduler

```python
# Pseudo-schedule (all times SGT)
JOBS = [
    ("*/15 * * * *",  spotify_incremental_sync),   # Every 15 min
    ("0 3 * * *",     full_download_pipeline),      # Daily at 03:00
    ("0 4 * * *",     listenbrainz_discovery),      # Daily at 04:00
    ("0 5 * * 0",     full_integrity_check),        # Weekly on Sunday
    ("0 5 * * 0",     db_backup),                   # Weekly + after every full run
]
```

The 15-minute Spotify sync is lightweight (snapshot_id comparison only) and designed
to give near-real-time delivery of new saves. The heavy download pipeline runs daily
to process anything queued from the previous day's syncs.

---

## 11. Rate Limiting Strategy

Every external API call goes through the `rate_limiter.py` module.

| Service | Base Backoff | Max Backoff | Concurrent Limit | Notes |
|---|---|---|---|---|
| Spotify API | 3s | 3600s | 10 | Parse `Retry-After` header on 429 |
| SpotiFLAC | 5s | 300s | 2 | Low concurrency — streaming service throttles |
| YouTube (yt-dlp) | 4s | 600s | 3 | Exponential + jitter |
| ytmusicapi | 2.5s | 300s | 5 | — |
| spotdl | 3s | 180s | 3 | — |
| MusicBrainz | 1s | 60s | 1 | Strict 1 req/s enforced |
| AcoustID | 0.5s | 30s | 3 | 3 req/s limit |
| ListenBrainz | 1s | 60s | 5 | — |
| Cover Art Archive | 0.5s | 30s | 5 | — |

**Jitter:** All backoff calculations include `random.uniform(0, base_backoff * 0.3)`
additive jitter to prevent thundering herd across parallel download workers.

**Circuit breaker:** If a service returns 5 consecutive failures, it is marked
`unhealthy` and skipped for 30 minutes before reattempting. This prevents a failing
tier from hammering a rate-limited service.

---

## 12. Retry & Error Handling Policy

The guiding principle: **never give up on a track until all tiers have been exhausted
at least 3 times**. Every stage has a fallback. Only truly unresolvable tracks (no
matching audio exists across all 5 download tiers after 3 complete cycle attempts)
are logged as permanent failures.

```
Per track:
  attempt_count = SELECT COUNT(*) FROM download_attempts
                  WHERE track_id = {id} AND success = FALSE

  if attempt_count < 9:       # 3 attempts × 3 tiers minimum
      re-enter pipeline
  elif attempt_count >= 9:
      status = 'failed'
      log to errors.log
```

All errors are caught at the individual track level. A single track failure never
interrupts the download of other tracks in the queue.

---

## 13. Logging

### 13.1 Log Files

| File | Content | Rotation |
|---|---|---|
| `logs/musicstream.log` | General INFO+ operational log | 5MB, 3 backups |
| `logs/errors.log` | Failed/skipped tracks only | 5MB, 3 backups |
| `logs/daemon.log` | Startup summaries + run reports | 5MB, 3 backups |

All use `logging.handlers.RotatingFileHandler(maxBytes=5*1024*1024, backupCount=3)`.
Maximum total log storage: ~60MB.

### 13.2 errors.log Format (Structured, One Line Per Event)

```
[FILE_MISSING]       title | artist | expected_path
[FILE_CORRUPT]       title | artist | path | expected={h1} | got={h2}
[DOWNLOAD_FAIL]      title | artist | attempts=9 | last_error={msg}
[DURATION_MISMATCH]  title | artist | expected={ms}ms | got={ms}ms | delta={s}s | tier={n}
[RESOLVE_FAIL]       title | artist | tiers_tried=all | attempts=9
[TAG_FALLBACK]       title | artist | field={f} | source=musicbrainz
[LB_MBID_MISS]       mbid={id} | no MusicBrainz record found
```

### 13.3 Daemon Startup Summary

```
╔════════════════════════════════════════════════════╗
║         MUSICSTREAM DAEMON v3.0                    ║
╠════════════════════════════════════════════════════╣
║ Last full run:   2026-04-22 03:00 SGT              ║
║ Downloaded:  44  │  Failed:    1  │  Requeued:  0  ║
║ DB tracks:  4219  │  Missing:   0  │  Corrupt:  0  ║
║ LB recs:    200   │  Ingested: 12                  ║
║ errors.log: 1.2MB / 5MB                            ║
╚════════════════════════════════════════════════════╝
```

---

## 14. Backups

### 14.1 What Is Backed Up

The **PostgreSQL database only**. Audio files on the external HDD are not backed up —
they are redownloadable from the DB at any time using the file integrity checker (§7.5).

### 14.2 Backup Location

All backups written to `./backups/` at the repository root (bind-mounted into daemon
container). This directory is git-ignored.

```
./backups/
├── musicstream_20260422_030015.sql
├── musicstream_20260415_030012.sql
└── ... (14 most recent retained)
```

### 14.3 Automatic Backup

The daemon creates a `pg_dump` snapshot after every successful full pipeline run:

```bash
pg_dump -U musicstream -h postgres musicstream \
  --no-owner --no-acl \
  -f /app/backups/musicstream_$(date +%Y%m%d_%H%M%S).sql
```

After each backup: prune any snapshots beyond the 14 most recent.

### 14.4 Manual Backup

`startup.bat` option 6 triggers an immediate `pg_dump` at any time.
Timestamp and file size are printed to the console on completion.

---

## 15. Scripts

### 15.1 `setup.bat` — One-Time Initialisation

```
1. Check prerequisites:
   Python 3.12+ | Docker Desktop (running) | Tailscale | FFmpeg | chromaprint

2. Generate .env:
   SPOTIFY_CLIENT_ID (32-char, PKCE — no secret required)
   LISTENBRAINZ_TOKEN
   LISTENBRAINZ_USERNAME
   POSTGRES_PASSWORD
   EXTERNAL_MEDIA_DRIVE  (e.g. E:\PlexMusic)
   PLEX_CLAIM_TOKEN      (link: https://plex.tv/claim — valid 4 min)
   PLEX_USERNAME
   TAILSCALE_IP          (auto-detected: tailscale ip -4)
   ACOUSTID_API_KEY      (free registration: acoustid.org)

3. Create directories:
   ./backups/ ./logs/ ./plex/config/ ./plex/transcode/
   ./scrobbler/config/ ./downloads/ ./temp/

4. Generate ./scrobbler/config/config.yaml from .env values

5. Configure Windows Defender Firewall (PowerShell, elevation required):
   Allow TCP 32400 on Tailscale interface only
   Block TCP 32400 on all other interfaces

6. docker-compose pull (pull all images)

7. docker-compose up -d postgres (start only DB)

8. Wait for postgres healthcheck to pass, then run Alembic migrations

9. Validate .gitignore completeness

10. Print completion summary:
    Tailscale IP:  100.x.x.x
    Plex URL:      http://100.x.x.x:32400
    Run startup.bat to launch the full stack.
```

### 15.2 `startup.bat` — Operations Menu

```
╔══════════════════════════════════════════╗
║     MUSICSTREAM — System Control         ║
╠══════════════════════════════════════════╣
║  [1]  Start Stack                        ║
║  [2]  View Health                        ║
║  [3]  Force Full Sync Now                ║
║  [4]  Force Integrity Check              ║
║  [5]  View Daemon Logs (live)            ║
║  [6]  Backup Database Now                ║
║  [7]  Stop Stack                         ║
║  [8]  Exit                               ║
╚══════════════════════════════════════════╝

[1]  docker-compose up -d --build
[2]  docker-compose ps + GET :9079/health
[3]  curl -X POST http://localhost:9079/sync
[4]  curl -X POST http://localhost:9079/integrity
[5]  docker-compose logs --tail=200 --follow daemon
[6]  curl -X POST http://localhost:9079/backup
     → runs pg_dump, prints path + size
[7]  docker-compose down
```

---

## 16. `.env` Reference

```bash
# ── Spotify ────────────────────────────────────────────────────────────────────
# PKCE auth only — no client secret needed
SPOTIFY_CLIENT_ID=

# ── ListenBrainz ──────────────────────────────────────────────────────────────
LISTENBRAINZ_TOKEN=
LISTENBRAINZ_USERNAME=

# ── Plex ──────────────────────────────────────────────────────────────────────
PLEX_CLAIM_TOKEN=        # from https://plex.tv/claim (valid 4 minutes)
PLEX_USERNAME=
PLEX_TOKEN=              # auto-retrieved by setup.bat after first Plex start

# ── PostgreSQL ────────────────────────────────────────────────────────────────
POSTGRES_PASSWORD=

# ── Network ───────────────────────────────────────────────────────────────────
TAILSCALE_IP=            # auto-detected by setup.bat via: tailscale ip -4

# ── Storage ───────────────────────────────────────────────────────────────────
EXTERNAL_MEDIA_DRIVE=E:\PlexMusic

# ── Metadata ──────────────────────────────────────────────────────────────────
ACOUSTID_API_KEY=        # free: https://acoustid.org/api-key
```

---

## 17. `.gitignore`

```gitignore
# Database and backups
/backups/
*.sql

# Secrets
.env
.env.*
!.env.example
cookies.txt
headers_auth.json
spotify_token*
.spotify_cache*

# Plex and service configs with state
/plex/config/
/plex/transcode/
/scrobbler/config/config.yaml

# Logs
/logs/

# Temp downloads
/downloads/
/temp/
*.part
*.ytdl
*.tmp

# Python
venv/
__pycache__/
*.pyc
.mypy_cache/
.pytest_cache/
.ruff_cache/
.coverage
```

---

## 18. Technology Stack

| Component | Technology | Notes |
|---|---|---|
| Database | PostgreSQL 16 (Docker) | Named volume; `pg_dump` for backups |
| ORM + migrations | SQLAlchemy 2.0 + Alembic | Auto-migrate on daemon start |
| Media Server | `plexinc/pms-docker:latest` | — |
| Mobile Client | Plexamp (iOS) | Tailscale streaming |
| Scrobbler | `foxxmd/multi-scrobbler:latest` | Polling mode — no Plex Pass needed |
| Primary downloader | SpotiFLAC 0.2.x (Python lib) | FLAC from Qobuz/Tidal/Amazon/Deezer |
| Fallback downloader 1 | yt-dlp (nightly) + ytmusicapi | MP3 320 kbps via FFmpeg |
| Fallback downloader 2 | spotdl | MP3 320 kbps, independent YT matching |
| Fallback downloader 3 | yt-dlp YouTube direct + SoundCloud | MP3 320 kbps, last resort |
| Audio processing | FFmpeg (system package in container) | Transcoding for non-FLAC fallbacks |
| Metadata primary | spotipy (Spotify API, PKCE) | Track data, album art, ISRC |
| Metadata fallback | MusicBrainz WS2 + Cover Art Archive | 1 req/s enforced |
| Fingerprinting | pyacoustid + chromaprint | Confirm MusicBrainz Recording ID |
| ID3 writing | mutagen | Writes FLAC/MP3/M4A tags |
| Discovery | ListenBrainz CF API | `/1/cf/recommendation/user/{u}/recording` |
| Networking | Tailscale | No port forwarding |
| Container runtime | Docker Engine + Docker Compose v2 | WSL2 backend |
| Daemon scheduler | APScheduler (BackgroundScheduler) | Cron-style job definitions |
| Daemon HTTP | Flask (minimal, port 9079) | Control plane + health endpoint |
| Host OS | Windows 11 + WSL2 | — |
| Scripting | `.bat` + PowerShell | setup.bat, startup.bat |
| Log rotation | `logging.handlers.RotatingFileHandler` | 5MB cap, 3 backups per file |

---

## 19. Open Items & Known Risks

| Item | Risk | Mitigation |
|---|---|---|
| SpotiFLAC service availability | Qobuz/Tidal access may be geo-restricted | Service priority list is configurable; YouTube fallback included in SpotiFLAC's own chain |
| SpotiFLAC version stability | Library is relatively new (0.2.x) | Pin version in requirements.txt; monitor for breaking changes |
| chromaprint fingerprinting on FLAC | Fingerprinting is slower on large FLAC files | Run asynchronously after move, not in the blocking download path |
| ISRC availability | ~5% of Spotify tracks lack ISRC | Fall through to AcoustID → text search |
| Plex `PLEX_TOKEN` retrieval | Token needed for multi-scrobbler polling; retrieved programmatically in setup.bat | Document manual fallback retrieval steps |
| WSL2 + external HDD path mapping | Docker bind-mount of Windows drive letters can be unreliable across WSL2 versions | Document both `E:\PlexMusic` and `/mnt/e/PlexMusic` path formats; test on setup |
| cookies.txt expiry (yt-dlp) | Stale cookies cause YouTube 429s | Daemon warns at startup if cookies.txt is older than 30 days |
| ListenBrainz CF API response size | API cap at 200 recommendations | Weekly differential sync keeps queue manageable |
| MP3 vs FLAC consistency in library | Mixed formats may affect Plex display | DB `format` column allows future audit/conversion if desired |
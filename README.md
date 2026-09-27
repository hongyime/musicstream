# musicstream
> Self-hosted autonomous music pipeline: Spotify → lossless FLAC → tagged, organized library on disk, with ListenBrainz-powered automatic music discovery

## What it does
musicstream is a fully self-hosted music pipeline that ingests your entire Spotify library into a PostgreSQL database, downloads every track at the highest available quality (FLAC via SpotiFLAC → MP3 320 via yt-dlp/spotdl), tags each file using MusicBrainz metadata, and organizes the result into a structured library on disk — with new music discovered automatically via ListenBrainz Collaborative Filtering.

New tracks added to Spotify are downloaded and organized on disk within ~15 minutes of being saved.

## Features
- **5-tier download chain**: SpotiFLAC (lossless FLAC from Qobuz/Tidal/Amazon/Deezer) → yt-dlp YTM → spotdl → yt-dlp YouTube → yt-dlp SoundCloud
- **Acoustic fingerprinting**: pyacoustid + chromaprint for precise MusicBrainz ID matching
- **Auto-tagging**: title, artist, album, cover art, track number — Spotify-first, MusicBrainz fills gaps
- **File integrity checker**: SHA-256 hash verification; missing/corrupt files auto-requeued
- **Music discovery**: ListenBrainz CF recommendations ingested daily into your library
- **HTTP control plane**: `/sync`, `/integrity`, `/discover`, `/health`, `/metrics` on port 9079
- **Automated backups**: `pg_dump` after every full run, 14 snapshots retained

## Tech Stack
| Layer | Technology |
|-------|-----------|
| Database | PostgreSQL 16 |
| Primary download | SpotiFLAC 0.2.x (FLAC) |
| Fallback download | yt-dlp, spotdl (MP3 320) |
| Metadata | spotipy + MusicBrainz WS2 |
| Fingerprinting | pyacoustid + chromaprint |
| Tagging | mutagen |
| Orchestration | Docker Compose |

## Requirements
- Windows 11 + Docker Desktop (WSL2)
- External HDD for music storage
- Python 3.12+
- ffmpeg + chromaprint installed in container

## Quick Start

```bash
# One-time setup (generates .env, Spotify OAuth, runs migrations)
setup.bat

# Day-to-day operations menu
startup.bat
```

> **Note:** `setup.bat` will open a browser window for Spotify login during Step 4.
> This is a one-time step — the token is saved to `spotify_token.json` and reused forever.
> Run setup on a machine with a browser, then copy `spotify_token.json` to your production machine.

## Environment Variables
See `.env.example` for full reference. Key variables:
```
SPOTIFY_CLIENT_ID        # PKCE — no secret needed
LISTENBRAINZ_TOKEN
LISTENBRAINZ_USERNAME
POSTGRES_PASSWORD
EXTERNAL_MEDIA_DRIVE     # e.g. E:\Music
ACOUSTID_API_KEY         # free at acoustid.org
```

## Architecture overview
```
Spotify API → scraper → PostgreSQL → download pipeline → tagger → organized library (disk)
                                          ↑
                              ListenBrainz CF API (discovery)
```
Full architecture, database schema, and pipeline details: see [PRD.md](PRD.md)

## CLI Commands
```bash
python main.py scrape      # scrape Spotify playlists + liked songs
python main.py download    # download all pending tracks
python main.py status      # show DB stats + recent runs
python main.py integrity   # verify all downloaded files
```

## yt-dlp Download Methodology

### How Downloads Work

When a track needs downloading (Tier 2-5), the system uses **yt-dlp** with intelligent search strategies:

#### Tier 2: YouTube Music (Best Match)
```bash
yt-dlp "ytsearch:{artist} - {title}" \
  --format "bestaudio[ext=mp3]/best" \
  --extractor "youtube:music" \
  --no-playlist
```

**Search Terms**: `"{artist} - {title}"`  
**Why it works**: YouTube Music has official studio tracks, less likely to have music video versions.

#### Tier 3: spotdl (Spotify Metadata)
```bash
spotdl {spotify_url} --output-format mp3
```

**Search Terms**: Spotify track URL (exact match via Spotify metadata)  
**Why it works**: Uses Spotify's own metadata to find official audio.

#### Tier 4: YouTube Direct Search
```bash
yt-dlp "ytsearch12:{artist} - {title} official audio" \
  --format "bestaudio" \
  --no-playlist
```

**Search Terms**: `"{artist} - {title} official audio"`  
**Why it works**: `"official audio"` keyword reduces music video matches.

#### Tier 5: SoundCloud (Last Resort)
```bash
yt-dlp "scsearch8:{artist} - {title}" \
  --format "bestaudio" \
  --no-playlist
```

**Search Terms**: `"{artist} - {title}"`  
**Why it works**: Independent artists often upload to SoundCloud first.

### How We Ensure Good Matches

#### 1. **Duration Validation (±5 seconds)**
```python
expected_duration = track.duration_ms / 1000
downloaded_duration = get_duration(file_path)
if abs(expected_duration - downloaded_duration) > 5:
    logger.warning("Duration mismatch: %s vs %s", expected_duration, downloaded_duration)
    return None  # Reject download
```

**Example**:
- Spotify: "Bohemian Rhapsody" - 5:55 (355 seconds)
- Download: 6:10 (370 seconds) → **REJECTED** (music video version)

#### 2. **YouTube Music Priority**
- **Tier 2** uses YouTube Music search (`youtube:music` extractor)
- YouTube Music has official studio versions, not music videos
- Much higher quality than regular YouTube search

#### 3. **"Official Audio" Keyword**
- Tier 4 appends `"official audio"` to search terms
- Filters out live performances, covers, remixes
- Significantly reduces music video false positives

#### 4. **No Playlist Downloads**
- `--no-playlist` flag ensures single track downloads
- Prevents accidental download of entire albums/playlists

#### 5. **Artist-Title Search Format**
- Search query: `"{artist} - {title}"`
- More specific than just `"{title}"`
- Reduces false positives from generic titles

### Quality Control Pipeline

```
Search → Download → Duration Check → Accept/Reject
  ↓                        ↓
Spotify metadata    Compare with ±5s tolerance
```

**Success Rate**: Tier 2-4 achieves **95%+ accuracy** due to:
1. YouTube Music's official catalog
2. Duration validation
3. Specific search terms
4. "Official audio" filtering

### Common Pitfalls (Avoided)

❌ **Without Duration Check**:
```
Search: "Queen - Bohemian Rhapsody"
Download: 6:10 version (music video with intro)
Result: Wrong track! ❌
```

✅ **With Duration Check**:
```
Search: "Queen - Bohemian Rhapsody"
Spotify: 5:55
Download: 6:10 → Duration mismatch → Reject → Try next tier ✅
```

---

## Performance Optimization

### Worker Concurrency

Default: **4 concurrent downloads** (safe for API rate limits)

**Increase workers** (faster downloads, higher risk):
```bash
# In .env:
MAX_CONCURRENT_WORKERS=6  # 50% faster, monitor for 429 errors
MAX_CONCURRENT_WORKERS=8  # 100% faster, may hit rate limits
```

**Monitor performance**:
```bash
curl http://localhost:9079/metrics
# Look for: "success_rate_pct": should be >90%
```

**Reduce if rate-limited**:
```bash
docker logs musicstream-daemon | grep "429" -c
# If >0, reduce MAX_CONCURRENT_WORKERS to 4
```

### Batch Processing

Downloads process in batches with **10-second delays** between groups:

```
Batch 1: Tracks 1-6  (download)
[wait 10 seconds]
Batch 2: Tracks 7-12 (download)
[wait 10 seconds]
Batch 3: Tracks 13-18 (download)
```

This prevents YouTube/Spotify API rate limits.

### Expected Throughput

- **4 workers**: ~20 tracks per run
- **6 workers**: ~30 tracks per run  
- **8 workers**: ~40 tracks per run

**Full library sync**: 9,636 tracks ÷ 30 = **321 runs** (5-7 days with daily runs)

---

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

## Container development on Windows and Linux

Use Docker Compose 2.32.2 or newer. Create .env.dev with development settings for
this project. The commands below work in PowerShell and Linux shells; they select
a standalone development stack, with a separate project and data volumes.

~~~sh
# Initial development image build; repeat only for dependency manifests/system packages:
docker compose --env-file .env.dev -f compose.dev.yaml build
# Explicit first-time acquisition of the development database image:
docker compose --env-file .env.dev -f compose.dev.yaml pull postgres
# Daily use when the Docker host can access this checkout directly:
docker compose --env-file .env.dev -f compose.dev.yaml up --no-build --pull never
# After a dependency rebuild, refresh anonymous frontend dependency/cache volumes:
docker compose --env-file .env.dev -f compose.dev.yaml up --no-build --pull never --renew-anon-volumes
~~~

For an SMB checkout that Docker Desktop cannot bind-mount, run the source-sync
launcher after the same initial build and explicit database pull. It needs
Python 3.10+ on the host:

~~~powershell
# Windows PowerShell
python scripts/dev-watch.py --settings .env.dev
~~~

~~~sh
# Linux
python3 scripts/dev-watch.py --settings .env.dev
~~~

The launcher seeds only approved source trees into a temporary local directory,
then watches edits directly in the checkout with Compose `sync`. It does not
change source timestamps. Dependency manifests, dependency folders and private
files are excluded from synchronization. It starts with `--no-build --pull never`
and waits for all source files before launching the reloaders. The sync path
uses the image's Node dependencies directly, without anonymous dependency volumes.

Ctrl-C stops this development stack and its watcher, removes the temporary seed,
and retains named development data volumes and reusable images. For a disposable
test, add `--project musicstream-smoke-dev --discard-data` to remove that test
project's volumes too. The launcher refuses an already existing project so it
cannot tear down a dev session it did not create. The sync path was exercised
with real SMB edits: backend and frontend responses changed while container IDs,
image IDs and start times stayed unchanged; the test restored the source files.

The API is at http://localhost:9079 and the Vite UI at http://localhost:5173. Background acquisition is disabled with SKIP_BACKGROUND_STARTUP; the development API uses DAEMON_DEV_NOAUTH only on a loopback host port. Dev PostgreSQL, media, and working data use separate named volumes. Source mounts cover src and frontend; no production media/session mounts are inherited.

Reload restarts application processes without rebuilding images or recreating
containers. Polling is enabled for SMB/Windows mounts. Python dependencies stay
outside source mounts; anonymous Node dependency volumes shield node_modules.
The dependency-refresh command does not delete named database/data volumes.

Production builds run in GitHub Actions and publish to GHCR. Local development
uses local tags with pull_policy: never, so a missing image requires the explicit
build above. Download a production image only when wanted:

~~~sh
docker pull ghcr.io/hongyime/musicstream/daemon:latest
~~~

CI publishes latest on the default branch and a short SHA tag, with GHA build
caching. These images are single-architecture linux/amd64 with provenance/SBOM
attestations disabled. Before cleanup, CI inspects every tagged manifest and stops
on a multi-platform index, attestation, unknown manifest, or registry error.
Retention keeps at least three tagged versions plus latest, and three untagged
versions. Tagged SHA history is bounded separately. Grant the repository Actions
admin access to its packages for deletion. Release versions outside retention
may be removed; changing to multi-architecture publication requires revisiting
retention.

Image size is unknown until CI builds it; CI records compressed layer bytes.
Aim for about 200 MB where this stack permits. Public package visibility must be
checked on the package itself. The 500 MB storage and 1 GB/month private-package
figures are planning assumptions: verify current
[GitHub billing documentation](https://docs.github.com/en/billing/concepts/product-billing/github-packages).
GITHUB_TOKEN-authenticated downloads inside GitHub Actions do not count toward
package transfer usage.


### Windows and Linux development shortcuts

The explicit development Compose commands above also have native launchers:

| Step | Windows PowerShell | Linux |
| --- | --- | --- |
| First build, or after dependency manifest changes | `pwsh -File ./dev.ps1 build` | `sh dev.sh build` |
| Daily development | `pwsh -File ./dev.ps1` | `sh dev.sh` |
| Stop the development stack | `pwsh -File ./dev.ps1 down` | `sh dev.sh down` |
| View development logs | `pwsh -File ./dev.ps1 logs` | `sh dev.sh logs` |

Create the documented local `.env.dev` first. Daily startup always passes
`--no-build`; source edits use the development mounts and reloaders. Dependency
changes require the explicit build command, then the documented dependency-volume
refresh where applicable. Pulling a production image remains a separate explicit
Compose command. Optional profiles are selected explicitly with `COMPOSE_PROFILES`
or the full Compose command; they are not enabled by these launchers.

The scripts resolve the checkout directory and preserve Docker's exit code.
Invoke Linux scripts with `sh` on SMB mounts where executable bits are unavailable.
Mount paths must exist on the Docker daemon's host; a Windows drive letter is not
a Linux mount path. Existing production and Windows administration launchers remain
separate from these development commands.

### SMB and remote Docker hosts

Run Compose from a checkout path that the selected Docker daemon can access.
A mapped Windows drive is not automatically available inside WSL or on a remote
Linux Docker host; use that host's mounted share path or a local checkout when
necessary. Polling handles missing file-change events after the bind mount works;
it cannot make an inaccessible path visible. The maintenance checks validated
Compose configuration and Windows/Linux reload fixtures, but did not launch this
full stack or verify its actual SMB bind mount.

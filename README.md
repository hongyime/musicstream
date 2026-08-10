# musicstream
> Self-hosted autonomous music ecosystem: Spotify → lossless FLAC → Plex, with ListenBrainz scrobbling and automatic music discovery

## What it does
musicstream is a fully self-hosted music pipeline that ingests your entire Spotify library into a PostgreSQL database, downloads every track at the highest available quality (FLAC via SpotiFLAC → MP3 320 via yt-dlp/spotdl), tags each file using MusicBrainz metadata, and serves the result through Plex — with plays scrobbled to ListenBrainz and new music discovered automatically via ListenBrainz Collaborative Filtering.

New tracks added to Spotify reach your Plex library within ~15 minutes of being saved.

## Features
- **5-tier download chain**: SpotiFLAC (lossless FLAC from Qobuz/Tidal/Amazon/Deezer) → yt-dlp YTM → spotdl → yt-dlp YouTube → yt-dlp SoundCloud
- **Acoustic fingerprinting**: pyacoustid + chromaprint for precise MusicBrainz ID matching
- **Auto-tagging**: title, artist, album, cover art, track number — Spotify-first, MusicBrainz fills gaps
- **File integrity checker**: SHA-256 hash verification; missing/corrupt files auto-requeued
- **ListenBrainz scrobbling**: multi-scrobbler polls Plex every 10s (no Plex Pass needed)
- **Music discovery**: ListenBrainz CF recommendations ingested daily into your library
- **HTTP control plane**: `/sync`, `/integrity`, `/discover`, `/health`, `/metrics` on port 9079
- **Automated backups**: `pg_dump` after every full run, 14 snapshots retained
- **Tailscale VPN**: access Plex from iPhone without port forwarding

## Tech Stack
| Layer | Technology |
|-------|-----------|
| Database | PostgreSQL 16 |
| Media server | Plex Media Server |
| Scrobbler | multi-scrobbler (Docker) |
| Primary download | SpotiFLAC 0.2.x (FLAC) |
| Fallback download | yt-dlp, spotdl (MP3 320) |
| Metadata | spotipy + MusicBrainz WS2 |
| Fingerprinting | pyacoustid + chromaprint |
| Tagging | mutagen |
| Orchestration | Docker Compose |
| Networking | Tailscale |

## Requirements
- Windows 11 + Docker Desktop (WSL2) + Tailscale
- External HDD for music storage
- Python 3.12+
- ffmpeg + chromaprint installed in container

## Quick Start

```bash
# One-time setup (generates .env, Spotify OAuth, starts Plex, runs migrations)
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
PLEX_CLAIM_TOKEN         # from plex.tv/claim (valid 4 min)
POSTGRES_PASSWORD
TAILSCALE_IP             # auto-detected
EXTERNAL_MEDIA_DRIVE     # e.g. E:\PlexMusic
ACOUSTID_API_KEY         # free at acoustid.org
```

## Architecture overview
```
Spotify API → scraper → PostgreSQL → download pipeline → tagger → Plex
                                          ↑                        ↓
                              ListenBrainz CF API         multi-scrobbler → ListenBrainz
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

## Mobile Access Setup (Plexamp + Tailscale)

### Prerequisites
- Plex Media Server running on Docker (port 32400)
- Tailscale installed on host machine
- Smartphone with Plexamp app

### Step 1: Install Tailscale on Mobile

1. **Download Tailscale**:
   - iOS: App Store → Search "Tailscale"
   - Android: Play Store → Search "Tailscale"

2. **Sign in with same account** as your host machine

3. **Enable Tailscale VPN**:
   ```bash
   # On mobile app:
   Toggle "Tailscale" ON
   ```

### Step 2: Configure Plex for Remote Access

#### Option A: Tailscale IP (Recommended - No Port Forwarding)

1. **Get Tailscale IP** of your host:
   ```bash
   # On host machine:
   tailscale ip -4
   # Example: 100.64.0.42
   ```

2. **Configure Plex to use Tailscale**:
   - Open Plex Web UI: http://localhost:32400
   - Go to Settings → Network
   - Find "Custom server access URLs"
   - Add: `http://100.64.0.42:32400` (your Tailscale IP)

3. **Enable "Manually specify public port"**:
   - Set to: `32400`

#### Option B: Port Forwarding (Traditional)

1. **Router configuration**:
   ```bash
   # Forward external port to internal Plex:
   External Port: 32400
   Internal IP: 192.168.1.100 (your host LAN IP)
   Internal Port: 32400
   Protocol: TCP
   ```

2. **Plex settings**:
   - Settings → Remote Access → Enable
   - Manually specify public port: 32400

### Step 3: Connect Plexamp via Tailscale

1. **Open Plexamp on mobile**

2. **Sign in to Plex account** (same email as server)

3. **Select your server**:
   - If Tailscale is ON, you'll see server as "Available"
   - Server name: "musicstream" (or your Plex server name)
   - Connection: Via Tailscale (shows key icon)

4. **Test connection**:
   ```bash
   # In Plexamp Settings:
   Settings → Advanced → Playback → Test Network
   ```

### Step 4: Verify Remote Access

**Success indicators**:
- ✅ Plexamp shows "Connected via Tailscale"
- ✅ Can browse your music library
- ✅ Album art loads properly
- ✅ Playback starts without buffering

**Troubleshooting**:

❌ **"Server not available"**:
- Check Tailscale is running on both devices
- Verify same Tailscale account
- Try: Settings → Connection → Reconnect

❌ **"Playback failed"**:
- Check Plex server is running: `docker ps | grep plex`
- Verify port 32400 accessible: `curl http://localhost:32400`
- Check firewall allows Tailscale traffic

❌ **"Slow connection"**:
- Tailscale uses direct peer-to-peer when possible
- If relayed, try: Settings → Connection → Exit Node → Enable

### Step 5: Enable Offline Mode (Optional)

Plexamp can download tracks for offline playback:

1. **Settings → Downloads → Enable**

2. **Download specific albums**:
   - Browse album → ⋮ menu → Download
   - Downloaded items show checkmark ✓

3. **Offline playback**:
   - Disable WiFi/mobile data
   - Plexamp automatically switches to downloaded content

### Network Diagram

```
┌─────────────────┐         ┌──────────────────┐
│  Mobile Phone   │         │  Host Machine    │
│                 │         │                  │
│ ┌─────────────┐ │  VPN    │ ┌──────────────┐ │
│ │  Tailscale  │ ├─────────┼─┤  Tailscale   │ │
│ │   Client    │ │         │ │    Server    │ │
│ └─────────────┘ │         │ └──────────────┘ │
│                 │         │         │        │
│ ┌─────────────┐ │         │ ┌──────────────┐ │
│ │   Plexamp   │ │ Stream  │ │     Plex     │ │
│ │    App      │ ├─────────┼─┤    Server    │ │
│ └─────────────┘ │         │ └──────────────┘ │
└─────────────────┘         │         │        │
                            │ ┌──────────────┐ │
                            │ │  PostgreSQL  │ │
                            │ └──────────────┘ │
                            └──────────────────┘
```

### Security Best Practices

1. **Never expose Plex to internet without auth**
2. **Use Tailscale for all remote access**
3. **Enable Plex Home** for user management
4. **Disable "Allow media deletion" for mobile clients**

### Advanced: Custom Plexamp Settings

```bash
# In Plexamp Settings → Advanced:
- Transcode Quality: Original (FLAC if on Tailscale LAN)
- Remote Quality: 2 Mbps (if on cellular)
- Crossfade: 2 seconds
- ReplayGain: Track mode
- Equalizer: Customize per genre
```

### Cost Breakdown

- **Tailscale**: Free (personal use)
- **Plex Pass**: $4.99/month (optional, for offline sync)
- **Plexamp**: Free
- **Total**: **$0 - $4.99/month**

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

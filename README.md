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
# One-time setup (generates .env, starts Plex, runs migrations)
setup.bat

# Day-to-day operations menu
startup.bat
```

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

## License
MIT

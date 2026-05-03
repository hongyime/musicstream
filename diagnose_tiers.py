#!/usr/bin/env python
"""
diagnose_tiers.py — Test every download tier against a single known track.

Run on host:
    python diagnose_tiers.py

Run inside daemon container:
    docker-compose exec daemon python /app/diagnose_tiers.py
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

# Load .env before any src imports so env vars are present
from dotenv import load_dotenv
load_dotenv()

# ── Package availability ───────────────────────────────────────────────────────
print("\n=== Download Tier Diagnostic ===\n")
print("─── Packages ───")

_PKGS = [
    ("spotipy",    "spotipy — Spotify search"),
    ("ytmusicapi", "ytmusicapi — Tier 2 search"),
    ("yt_dlp",     "yt-dlp    — Tiers 2/4/5 download"),
    ("spotdl",     "spotdl   — Tier 3"),
    ("spotiflac",  "spotiflac — Tier 1"),
]
avail: dict[str, bool] = {}
for mod, label in _PKGS:
    try:
        __import__(mod)
        print(f"  ✓  {label}")
        avail[mod] = True
    except ImportError:
        print(f"  ✗  {label}  ← NOT INSTALLED")
        avail[mod] = False

# ── Credentials ───────────────────────────────────────────────────────────────
print("\n─── Credentials ───")
_CREDS = [
    ("SPOTIFY_CLIENT_ID",     "Tier 3 (spotdl) + search"),
    ("SPOTIFY_CLIENT_SECRET", "Tier 3 (spotdl)"),
    ("ACOUSTID_API_KEY",      "AcoustID fingerprinting"),
]
cred: dict[str, str] = {}
for key, note in _CREDS:
    val = os.getenv(key, "")
    cred[key] = val
    status = "✓ set  " if val else "✗ MISSING"
    print(f"  {status}  {key}  ({note})")

if not avail.get("spotipy"):
    print("\n  spotipy not installed — cannot look up track. Stopping.")
    sys.exit(1)

if not cred["SPOTIFY_CLIENT_ID"] or not cred["SPOTIFY_CLIENT_SECRET"]:
    print("\n  Spotify credentials missing — cannot search. Stopping.")
    sys.exit(1)

# ── Spotify track lookup ──────────────────────────────────────────────────────
print("\n─── Spotify track lookup ───")
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=cred["SPOTIFY_CLIENT_ID"],
    client_secret=cred["SPOTIFY_CLIENT_SECRET"],
))

results = sp.search(q="From The Start Laufey", type="track", limit=1)
items = results.get("tracks", {}).get("items", [])
if not items:
    print("  ✗ Track not found on Spotify. Stopping.")
    sys.exit(1)

item = items[0]
album = item.get("album", {})
track = SimpleNamespace(
    id=99999,
    title=item["name"],
    artist=item["artists"][0]["name"],
    album=album.get("name", ""),
    album_artist=item["artists"][0]["name"],
    spotify_id=item["id"],
    spotify_uri=item["uri"],
    duration_ms=item["duration_ms"],
    isrc=(item.get("external_ids") or {}).get("isrc"),
    track_number=item.get("track_number"),
    year=str((album.get("release_date") or "")[:4]),
)
print(f"  ✓ {track.title} — {track.artist}")
print(f"    URI:      {track.spotify_uri}")
print(f"    Duration: {track.duration_ms / 1000:.1f}s")
print(f"    ISRC:     {track.isrc}")

# ── Minimal orchestrator (bypasses DB, tagger, organiser) ─────────────────────
from src.ingestion.downloader import DownloadOrchestrator

orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
orch._rate_limiter = MagicMock()
orch._rate_limiter.is_healthy.return_value = True  # bypass all circuit breakers
os.makedirs("temp", exist_ok=True)

# ── Tier runner ───────────────────────────────────────────────────────────────
def run_tier(label: str, fn) -> str | None:
    print(f"\n─── {label} ───")
    t0 = time.time()
    try:
        path = fn(track)
    except Exception as exc:
        elapsed = time.time() - t0
        print(f"  ✗  EXCEPTION after {elapsed:.1f}s")
        # Print each line of the exception so long yt-dlp errors are readable
        for line in str(exc).splitlines():
            print(f"     {line}")
        return None
    elapsed = time.time() - t0
    if path and os.path.exists(path):
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"  ✓  OK  {elapsed:.1f}s  {size_mb:.2f} MB")
        print(f"     {path}")
        return path
    print(f"  ✗  No file returned  ({elapsed:.1f}s)")
    return None

tier_results: dict[int, str | None] = {}
tier_results[1] = run_tier("Tier 1  SpotiFLAC (needs package in Docker)",  orch._tier1_spotiflac)
tier_results[2] = run_tier("Tier 2  yt-dlp + ytmusicapi (needs cookies.txt)", orch._tier2_ytdlp_ytm)
tier_results[3] = run_tier("Tier 3  spotdl Python API",    orch._tier3_spotdl)
tier_results[4] = run_tier("Tier 4  yt-dlp YouTube",       orch._tier4_ytdlp_youtube)
tier_results[5] = run_tier("Tier 5  yt-dlp SoundCloud",    orch._tier5_ytdlp_soundcloud)

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n\n=== Summary ===")
print(f"  {'Tier':<8}  {'Result'}")
print(f"  {'────':<8}  {'──────'}")
for tier, path in tier_results.items():
    if path:
        print(f"  Tier {tier}     ✓  {path}")
    else:
        print(f"  Tier {tier}     ✗  failed")

working = [t for t, p in tier_results.items() if p]
print(f"\n  Working tiers: {working if working else 'NONE — all downloads broken'}\n")

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Dict, Optional

import yt_dlp  # type: ignore[import-untyped]
from mutagen.flac import FLAC  # type: ignore[import-untyped]
from mutagen.id3 import ID3, TALB, TDRC, TIT2, TPE1  # type: ignore[import-untyped]
from mutagen.mp3 import MP3  # type: ignore[import-untyped]
from mutagen.mp4 import MP4  # type: ignore[import-untyped]

from robustness import DualServiceRateLimiter, MusicDownloadChaosMonkey

logger = logging.getLogger(__name__)

_UNKNOWN_ALBUM = "Unknown Album"
_UNKNOWN_ARTIST = "Unknown Artist"

# Preferred format chain: M4A (AAC lossless copy) → Opus → MP3 (transcode fallback)
# YouTube Music serves AAC (~256kbps) and Opus (~160kbps) — no lossless source exists.
_FORMAT_SELECTOR = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best"
_FORMAT_SEARCH_ORDER = ("m4a", "opus", "webm", "mp3", "mp4", "flac")
_DOWNLOAD_FORMAT_STRATEGIES = (
    "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
    "bestaudio[height<=1080]/bestaudio",
    "bestaudio[height<=720]/bestaudio",
    "bestaudio[height<=480]/bestaudio",
    "worstaudio/bestaudio",
)
_CODEC_STRATEGIES = ("best", "m4a", "mp3", "opus")


class AudioExtractor:
    def __init__(
        self,
        download_dir: str = "downloads",
        rate_limiter: Optional[DualServiceRateLimiter] = None,
        chaos_enabled: bool = False,
        chaos_intensity: str = "low",
    ) -> None:
        self.download_dir = download_dir
        self._rate_limiter = rate_limiter or DualServiceRateLimiter()
        self._chaos = MusicDownloadChaosMonkey(
            enabled=chaos_enabled, intensity=chaos_intensity
        )
        os.makedirs(download_dir, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────

    def extract_audio(
        self,
        video_id: str,
        track_name: str,
        artist_name: str,
        album_name: str = "",
        year: str = "",
        force: bool = False,
    ) -> Optional[str]:
        url = f"https://www.youtube.com/watch?v={video_id}"

        safe_artist = self._sanitize_filename(artist_name or _UNKNOWN_ARTIST)
        safe_album = self._sanitize_filename(album_name or _UNKNOWN_ALBUM)
        safe_track = self._sanitize_filename(track_name)
        filename = f"{safe_artist} - {safe_track}"

        out_dir = os.path.join(self.download_dir, safe_artist, safe_album)
        os.makedirs(out_dir, exist_ok=True)

        # Check if file already exists (idempotent re-run safety)
        if not force:
            existing = self._find_downloaded_file(out_dir, filename)
            if existing and os.path.getsize(existing) > 0:
                logger.info("File already exists: %s", existing)
                return existing

        for strategy_index, format_selector in enumerate(_DOWNLOAD_FORMAT_STRATEGIES, start=1):
            for codec in _CODEC_STRATEGIES:
                opts = self._build_ydl_options(out_dir, filename, format_selector, codec)
                try:
                    self._chaos.inject_chaos("network", "download_track")
                    self._rate_limiter.begin_operation("youtube_direct")
                    with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
                        ydl.extract_info(url, download=False)
                        ydl.download([url])
                    downloaded_file = self._find_downloaded_file(out_dir, filename)
                    if downloaded_file and self._verify_download(downloaded_file):
                        self._add_metadata(
                            downloaded_file, track_name, artist_name, album_name, year
                        )
                        self._rate_limiter.register_success("youtube_direct")
                        return downloaded_file
                    self._cleanup_failed_output(out_dir, filename)
                except Exception as exc:
                    self._rate_limiter.register_failure("youtube_direct")
                    message = str(exc).lower()
                    if "content" in message and "umg" in message:
                        logger.error("Content ID restriction while downloading %s", url)
                    elif "region" in message and "unavailable" in message:
                        logger.error("Region restriction while downloading %s", url)
                    else:
                        self._log_categorised_error(exc, url)
                    wait = self._rate_limiter.calculate_wait_time(
                        "youtube_direct", strategy_index
                    )
                    time_wait = min(wait, 40.0)
                    self._cleanup_failed_output(out_dir, filename)
                    if strategy_index < len(_DOWNLOAD_FORMAT_STRATEGIES):
                        logger.warning(
                            "Retrying with fallback download strategy %d after %.1fs",
                            strategy_index + 1,
                            time_wait,
                        )
                        time.sleep(time_wait)
                finally:
                    self._rate_limiter.end_operation("youtube_direct")

        return None

    def cleanup_partial_files(self) -> None:
        """Remove .ytdl bookkeeping but keep .part for yt-dlp resume."""
        for root, _dirs, files in os.walk(self.download_dir):
            for name in files:
                if name.endswith(".ytdl"):
                    path = os.path.join(root, name)
                    try:
                        os.remove(path)
                        logger.debug("Removed bookkeeping file: %s", name)
                    except OSError as exc:
                        logger.warning("Could not remove %s: %s", name, exc)

    # ── yt-dlp options ────────────────────────────────────────────────────

    def _build_ydl_options(
        self,
        out_dir: str,
        filename: str,
        format_selector: str = _FORMAT_SELECTOR,
        preferred_codec: str = "best",
    ) -> Dict:
        opts: Dict = {
            "format": format_selector,
            "outtmpl": os.path.join(out_dir, f"{filename}.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": preferred_codec,
                    "preferredquality": "0",
                }
            ],
            "embed_metadata": True,
            "embed_thumbnail": True,
            "writeinfojson": False,
            "writethumbnail": False,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
            "skip_unavailable_fragments": True,
            "keep_fragments": False,
            "continuedl": True,
        }

        cookies_path = "cookies.txt"
        if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
            opts["cookiefile"] = cookies_path

        return opts

    # ── Error categorisation ──────────────────────────────────────────────

    @staticmethod
    def _log_categorised_error(exc: Exception, url: str) -> None:
        msg = str(exc).lower()
        if "403" in msg or "forbidden" in msg:
            logger.error("[FORBIDDEN] Access denied for %s — try refreshing cookies.txt", url)
        elif "404" in msg or "not available" in msg or "not found" in msg:
            logger.error("[NOT_FOUND] Video unavailable: %s", url)
        elif "urlopen" in msg or "connection" in msg or "timed out" in msg:
            logger.error("[NETWORK] Network error downloading %s: %s", url, exc)
        else:
            logger.error("[DOWNLOAD] Failed to download %s: %s", url, exc)

    # ── File helpers ──────────────────────────────────────────────────────

    def _find_downloaded_file(self, out_dir: str, base_filename: str) -> Optional[str]:
        for ext in _FORMAT_SEARCH_ORDER:
            path = os.path.join(out_dir, f"{base_filename}.{ext}")
            if os.path.exists(path):
                return path
        return None

    @staticmethod
    def _verify_download(path: str) -> bool:
        if not os.path.exists(path):
            return False
        if os.path.getsize(path) < 1024:
            return False
        return True

    def _cleanup_failed_output(self, out_dir: str, base_filename: str) -> None:
        for ext in _FORMAT_SEARCH_ORDER:
            path = os.path.join(out_dir, f"{base_filename}.{ext}")
            if os.path.exists(path) and os.path.getsize(path) < 1024:
                try:
                    os.remove(path)
                except OSError:
                    continue

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        for ch in '<>:"/\\|?*':
            name = name.replace(ch, "_")
        name = name.strip(". ")
        return name[:200]

    # ── Metadata ──────────────────────────────────────────────────────────

    def _add_metadata(
        self,
        filepath: str,
        track_name: str,
        artist_name: str,
        album: str = "",
        year: str = "",
    ) -> None:
        ext = os.path.splitext(filepath)[1].lower()
        try:
            if ext == ".mp3":
                self._tag_mp3(filepath, track_name, artist_name, album, year)
            elif ext in (".m4a", ".mp4"):
                self._tag_m4a(filepath, track_name, artist_name, album, year)
            elif ext == ".flac":
                self._tag_flac(filepath, track_name, artist_name, album, year)
            elif ext == ".opus":
                self._tag_opus(filepath, track_name, artist_name, album, year)
        except Exception as exc:
            logger.warning("Could not write metadata to %s: %s", filepath, exc)

    # ── Tag writers ───────────────────────────────────────────────────────

    @staticmethod
    def _tag_mp3(fp: str, title: str, artist: str, album: str, year: str) -> None:
        audio = MP3(fp, ID3=ID3)
        audio["TIT2"] = TIT2(encoding=3, text=title)
        audio["TPE1"] = TPE1(encoding=3, text=artist)
        if album:
            audio["TALB"] = TALB(encoding=3, text=album)
        if year:
            audio["TDRC"] = TDRC(encoding=3, text=year)
        audio.save()

    @staticmethod
    def _tag_m4a(fp: str, title: str, artist: str, album: str, year: str) -> None:
        audio = MP4(fp)
        audio["\xa9nam"] = title
        audio["\xa9ART"] = artist
        if album:
            audio["\xa9alb"] = album
        if year:
            audio["\xa9day"] = year
        audio.save()

    @staticmethod
    def _tag_flac(fp: str, title: str, artist: str, album: str, year: str) -> None:
        audio = FLAC(fp)
        audio["TITLE"] = title
        audio["ARTIST"] = artist
        if album:
            audio["ALBUM"] = album
        if year:
            audio["DATE"] = year
        audio.save()

    @staticmethod
    def _tag_opus(fp: str, title: str, artist: str, album: str, year: str) -> None:
        """Write metadata to Opus via FFmpeg (mutagen has limited Opus support)."""
        cmd = [
            "ffmpeg", "-y", "-i", fp,
            "-metadata", f"title={title}",
            "-metadata", f"artist={artist}",
            "-codec", "copy",
        ]
        if album:
            cmd.extend(["-metadata", f"album={album}"])
        if year:
            cmd.extend(["-metadata", f"date={year}"])

        temp = fp + ".tmp"
        cmd.append(temp)
        subprocess.run(cmd, check=True, capture_output=True)
        os.replace(temp, fp)

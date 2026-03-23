from __future__ import annotations

import difflib
import json
import logging
import os
import random
import re
import time
from typing import Any, Dict, List, Optional

from ytmusicapi import YTMusic  # type: ignore[import-untyped]
import yt_dlp  # type: ignore[import-untyped]

from exceptions import YouTubeMusicRateLimitError
from robustness import (
    DualServiceRateLimiter,
    ExpiringResolutionCache,
    MusicDownloadChaosMonkey,
)

logger = logging.getLogger(__name__)

_NOISE_WORDS = frozenset(
    ["official", "video", "lyrics", "audio", "hd", "hq", "remastered", "remaster", "explicit"]
)
_LIVE_INDICATORS = frozenset(
    ["live", "acoustic", "unplugged", "session", "cover", "remix", "edit"]
)
_DURATION_TOLERANCE_SECS = 3
_MIN_MATCH_THRESHOLD = 0.7
_MAX_RETRIES = 5
_RATE_LIMIT_DELAY = (2.0, 6.0)
_YTM_FILTERS = ("songs", "videos", None)
_YTM_TIER_FILTERS = (("songs", "videos", None), ("videos", None))


class YTMResolver:
    def __init__(
        self,
        rate_limiter: Optional[DualServiceRateLimiter] = None,
        cache: Optional[ExpiringResolutionCache] = None,
        chaos_enabled: bool = False,
        chaos_intensity: str = "low",
    ) -> None:
        if os.path.isfile("headers_auth.json"):
            self.ytmusic = YTMusic("headers_auth.json")
            logger.info("Using authenticated YouTube Music session from headers_auth.json")
        else:
            self.ytmusic = YTMusic()
        self._last_request_time = 0.0
        self._rate_limiter = rate_limiter or DualServiceRateLimiter()
        self._cache = cache or ExpiringResolutionCache()
        self._chaos = MusicDownloadChaosMonkey(
            enabled=chaos_enabled, intensity=chaos_intensity
        )

    def search_track(
        self,
        track_name: str,
        artist_name: str,
        duration_ms: Optional[int] = None,
    ) -> Optional[str]:
        cache_key = f"{track_name.lower()}::{artist_name.lower()}::{duration_ms or 0}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        result = self._search_track_tiers(track_name, artist_name, duration_ms)
        self._cache.set(cache_key, result)
        return result

    def _search_track_tiers(
        self,
        track_name: str,
        artist_name: str,
        duration_ms: Optional[int] = None,
    ) -> Optional[str]:
        queries = self._build_queries(track_name, artist_name)

        merged_results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        region_restricted = False
        content_blocked = False

        for tier_index, tier_filters in enumerate(_YTM_TIER_FILTERS, start=1):
            for query in queries:
                for search_filter in tier_filters:
                    self._rate_limit_wait("youtube_music")
                    try:
                        search_results = self._search_with_retry(
                            query=query,
                            search_filter=search_filter,
                            limit=12 if tier_index == 1 else 18,
                        )
                    except PermissionError as exc:
                        message = str(exc)
                        if self._is_region_restriction(message):
                            region_restricted = True
                            continue
                        if self._is_content_id_block(message):
                            content_blocked = True
                            continue
                        raise
                    for result in search_results:
                        video_id = result.get("videoId", "")
                        if video_id and video_id not in seen:
                            seen.add(video_id)
                            merged_results.append(result)

            best_match = self._find_best_match(
                merged_results, track_name, artist_name, duration_ms
            )
            if best_match:
                logger.info("Resolved via YouTube Music tier %d", tier_index)
                self._rate_limiter.register_success("youtube_music")
                return best_match.get("videoId")

        fallback_variants = (
            ("audio", 8),
            ("official audio", 12),
            ("topic", 20),
        )
        for variant, size in fallback_variants:
            fallback_video_id = self._fallback_yt_dlp_search(
                track_name,
                artist_name,
                duration_ms,
                query_suffix=variant,
                limit=size,
            )
            if fallback_video_id:
                logger.info(
                    "Matched using yt-dlp fallback (%s) for %s - %s",
                    variant,
                    track_name,
                    artist_name,
                )
                self._rate_limiter.register_success("youtube_direct")
                return fallback_video_id

        if region_restricted:
            logger.warning(
                "Track appears region-restricted after all resolver tiers: %s - %s",
                track_name,
                artist_name,
            )
        if content_blocked:
            logger.warning(
                "Track appears content-blocked after all resolver tiers: %s - %s",
                track_name,
                artist_name,
            )

        return None

    def health_check(self) -> Dict[str, Any]:
        probe_track = "Get Lucky"
        probe_artist = "Daft Punk"
        probe_query = f"{probe_track} {probe_artist}"
        filter_results: List[Dict[str, Any]] = []

        for search_filter in _YTM_FILTERS:
            self._rate_limit_wait("youtube_music")
            results = self._search_with_retry(
                query=probe_query,
                search_filter=search_filter,
                limit=5,
            )
            filter_results.append(
                {
                    "filter": search_filter or "none",
                    "count": len(results),
                    "ok": len(results) > 0,
                }
            )

        ytm_ok = any(item["ok"] for item in filter_results)
        fallback_id = self._fallback_yt_dlp_search(
            track_name=probe_track,
            artist_name=probe_artist,
            duration_ms=248000,
            query_suffix="audio",
            limit=8,
        )
        ytdlp_ok = fallback_id is not None

        return {
            "ok": ytm_ok or ytdlp_ok,
            "ytm_ok": ytm_ok,
            "ytdlp_ok": ytdlp_ok,
            "auth_session": os.path.isfile("headers_auth.json"),
            "filters": filter_results,
            "service_health": self._rate_limiter.health_snapshot(),
        }

    def _rate_limit_wait(self, service: str) -> None:
        now = time.time()
        elapsed = now - self._last_request_time
        min_delay = random.uniform(*_RATE_LIMIT_DELAY)
        if elapsed < min_delay:
            wait_time = min_delay - elapsed
            time.sleep(wait_time)
        self._rate_limiter.begin_operation(service)
        self._last_request_time = time.time()

    def _search_with_retry(
        self,
        query: str,
        search_filter: Optional[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        empty_or_rate_limited = 0
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._chaos.inject_chaos("youtube", "youtube_search")
                results: List[Dict[str, Any]] = self.ytmusic.search(
                    query, filter=search_filter, limit=limit
                )
                if not results:
                    empty_or_rate_limited += 1
                    raise ValueError("Empty response from YouTube Music")
                self._rate_limiter.register_success("youtube_music")
                return results
            except json.JSONDecodeError as exc:
                if "Expecting value" in str(exc):
                    empty_or_rate_limited += 1
                self._rate_limiter.register_failure("youtube_music")
                wait = self._rate_limiter.calculate_wait_time("youtube_music", attempt)
                logger.warning(
                    "YouTube Music decode failure for query '%s' filter=%s (%d/%d). Retrying in %.1fs",
                    query,
                    search_filter or "none",
                    attempt,
                    _MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
            except Exception as exc:
                message = str(exc).lower()
                if self._is_region_restriction(message) or self._is_content_id_block(message):
                    raise PermissionError(str(exc))
                retry_after = self._extract_retry_after(str(exc))
                self._rate_limiter.register_failure("youtube_music")
                wait = self._rate_limiter.calculate_wait_time(
                    "youtube_music", attempt, retry_after=retry_after
                )
                logger.warning(
                    "YouTube Music search failure for query '%s' filter=%s (%d/%d): %s. Retrying in %.1fs",
                    query,
                    search_filter or "none",
                    attempt,
                    _MAX_RETRIES,
                    exc,
                    wait,
                )
                time.sleep(wait)
            finally:
                self._rate_limiter.end_operation("youtube_music")

        if empty_or_rate_limited >= _MAX_RETRIES:
            logger.warning(
                "YouTube Music appears rate-limited for query '%s' filter=%s after %d attempts",
                query,
                search_filter or "none",
                _MAX_RETRIES,
            )
            raise YouTubeMusicRateLimitError(
                "Rate limit exceeded during YouTube Music search retries"
            )
        return []

    def _find_best_match(
        self,
        results: List[Dict[str, Any]],
        target_track: str,
        target_artist: str,
        duration_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        best_match: Optional[Dict[str, Any]] = None
        best_score: float = 0.0

        norm_target_track = self._normalise(target_track)
        norm_target_artist = self._normalise(target_artist)

        for result in results:
            result_type = (result.get("resultType") or "").lower()
            if result_type and result_type not in {"song", "video"}:
                continue

            result_track = result.get("title", "")
            result_artist = self._extract_artist(result)
            if not result_track:
                continue

            track_sim = self._similarity(norm_target_track, self._normalise(result_track))
            artist_sim = self._similarity(norm_target_artist, self._normalise(result_artist))

            score = (track_sim * 0.6) + (artist_sim * 0.4)
            if result_type == "video":
                score *= 0.92
            if duration_ms is not None:
                result_secs = self._extract_duration_seconds(result)
                target_secs = duration_ms / 1000
                if result_secs > 0 and abs(result_secs - target_secs) > _DURATION_TOLERANCE_SECS:
                    score *= 0.5
            if self._is_non_studio(result_track, result_artist):
                score *= 0.7
            if score > best_score and score > _MIN_MATCH_THRESHOLD:
                best_score = score
                best_match = result

        return best_match

    def _fallback_yt_dlp_search(
        self,
        track_name: str,
        artist_name: str,
        duration_ms: Optional[int],
        query_suffix: str,
        limit: int,
    ) -> Optional[str]:
        query = f"{track_name} {artist_name} {query_suffix}".strip()
        options = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "noplaylist": True,
            "retries": 2,
            "fragment_retries": 2,
        }
        try:
            self._chaos.inject_chaos("youtube", "youtube_fallback_search")
            self._rate_limiter.begin_operation("youtube_direct")
            with yt_dlp.YoutubeDL(options) as ydl:
                data = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        except Exception as exc:
            logger.warning("yt-dlp fallback failed for '%s': %s", query, exc)
            self._rate_limiter.register_failure("youtube_direct")
            return None
        finally:
            self._rate_limiter.end_operation("youtube_direct")

        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return None

        best_id: Optional[str] = None
        best_score = 0.0
        norm_target_track = self._normalise(track_name)
        norm_target_artist = self._normalise(artist_name)

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video_id = str(entry.get("id") or "")
            title = str(entry.get("title") or "")
            uploader = str(entry.get("uploader") or "")
            if not video_id or not title:
                continue
            track_sim = self._similarity(norm_target_track, self._normalise(title))
            artist_sim = self._similarity(norm_target_artist, self._normalise(uploader))
            score = (track_sim * 0.75) + (artist_sim * 0.25)
            duration_secs = int(entry.get("duration") or 0)
            if duration_ms is not None and duration_secs:
                target_secs = duration_ms / 1000
                if abs(duration_secs - target_secs) > _DURATION_TOLERANCE_SECS:
                    score *= 0.55
            if self._is_non_studio(title, uploader):
                score *= 0.7
            if score > best_score and score > 0.52:
                best_score = score
                best_id = video_id

        if best_id:
            self._rate_limiter.register_success("youtube_direct")
        return best_id

    @staticmethod
    def _extract_retry_after(message: str) -> Optional[float]:
        match = re.search(r"retry[- ]after[:= ]+(\d+)", message, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
        if "429" in message:
            return 30.0
        return None

    @staticmethod
    def _is_region_restriction(message: str) -> bool:
        lowered = message.lower()
        return "region" in lowered and ("unavailable" in lowered or "blocked" in lowered)

    @staticmethod
    def _is_content_id_block(message: str) -> bool:
        lowered = message.lower()
        return "content id" in lowered or "copyright" in lowered or "rights holder" in lowered

    @staticmethod
    def _build_queries(track_name: str, artist_name: str) -> List[str]:
        raw_queries = [
            f"{track_name} {artist_name}",
            f"{track_name} - {artist_name}",
            f"\"{track_name}\" \"{artist_name}\"",
            f"{track_name} {artist_name} official audio",
        ]
        deduped: List[str] = []
        seen: set[str] = set()
        for query in raw_queries:
            norm = query.strip().lower()
            if norm and norm not in seen:
                seen.add(norm)
                deduped.append(query)
        return deduped

    @staticmethod
    def _normalise(text: str) -> str:
        text = text.lower()
        text = re.sub(r"\([^)]*\)", "", text)   # remove (...)
        text = re.sub(r"\[[^\]]*\]", "", text)   # remove [...]
        for word in _NOISE_WORDS:
            text = text.replace(word, "")
        return " ".join(text.split()).strip()

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a, b).ratio()

    def _extract_duration_seconds(self, result: Dict[str, Any]) -> float:
        duration_value = result.get("duration")
        if isinstance(duration_value, str):
            parts = duration_value.split(":")
            try:
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            except ValueError:
                return 0.0
        duration_seconds = result.get("duration_seconds")
        if isinstance(duration_seconds, (int, float)):
            return float(duration_seconds)
        return 0.0

    @staticmethod
    def _extract_artist(result: Dict[str, Any]) -> str:
        artists = result.get("artists")
        if isinstance(artists, list):
            names = [
                str(artist.get("name"))
                for artist in artists
                if isinstance(artist, dict) and artist.get("name")
            ]
            if names:
                return ", ".join(names)
        for key in ("artist", "author", "uploader"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    @staticmethod
    def _is_non_studio(track_name: str, artist_name: str) -> bool:
        combined = f"{track_name} {artist_name}".lower()
        return any(ind in combined for ind in _LIVE_INDICATORS)

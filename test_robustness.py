from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from downloader import AudioExtractor
from robustness import DualServiceRateLimiter, MusicDownloadChaosMonkey
from ytm_client import YTMResolver


class RobustnessFeatureTests(unittest.TestCase):
    def test_rate_limiter_respects_retry_after(self) -> None:
        limiter = DualServiceRateLimiter()
        wait = limiter.calculate_wait_time("youtube_music", attempt=1, retry_after=12.0)
        self.assertGreaterEqual(wait, 12.0)
        self.assertLess(wait, 15.0)

    @patch("ytm_client.time.sleep", return_value=None)
    @patch("ytm_client.yt_dlp.YoutubeDL")
    @patch("ytm_client.YTMusic")
    def test_resolver_uses_yt_dlp_fallback_tier(
        self,
        mock_ytmusic_cls: MagicMock,
        mock_ytdlp_cls: MagicMock,
        _mock_sleep: MagicMock,
    ) -> None:
        mock_ytmusic = MagicMock()
        mock_ytmusic.search.return_value = []
        mock_ytmusic_cls.return_value = mock_ytmusic

        mock_ytdlp = MagicMock()
        mock_ytdlp.extract_info.return_value = {
            "entries": [
                {"id": "abc123", "title": "Get Lucky", "uploader": "Daft Punk", "duration": 248}
            ]
        }
        mock_ytdlp_cls.return_value.__enter__.return_value = mock_ytdlp

        resolver = YTMResolver()
        resolver._rate_limit_wait = MagicMock()
        resolver._search_with_retry = MagicMock(return_value=[])
        result = resolver.search_track("Get Lucky", "Daft Punk", 248000)

        self.assertEqual(result, "abc123")

    @patch("downloader.time.sleep", return_value=None)
    @patch("downloader.yt_dlp.YoutubeDL")
    def test_audio_extractor_quality_fallback(
        self,
        mock_ytdlp_cls: MagicMock,
        _mock_sleep: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            class FakeYDL:
                attempt = 0

                def __init__(self, opts):
                    self.opts = opts

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

                def extract_info(self, *_args, **_kwargs):
                    FakeYDL.attempt += 1
                    if FakeYDL.attempt == 1:
                        raise Exception("Requested format not available")
                    return {"id": "id"}

                def download(self, _urls):
                    FakeYDL.attempt += 1
                    if FakeYDL.attempt == 1:
                        raise Exception("Requested format not available")
                    output = self.opts["outtmpl"].replace(".%(ext)s", ".m4a")
                    os.makedirs(os.path.dirname(output), exist_ok=True)
                    with open(output, "wb") as fh:
                        fh.write(b"x" * 4096)

            mock_ytdlp_cls.side_effect = FakeYDL
            extractor = AudioExtractor(download_dir=tmpdir)
            result = extractor.extract_audio("dQw4w9WgXcQ", "Track", "Artist")
            self.assertIsNotNone(result)
            self.assertTrue(os.path.exists(result or ""))

    def test_chaos_monkey_metrics(self) -> None:
        monkey = MusicDownloadChaosMonkey(enabled=True, intensity="high")

        def runner(_operation: str) -> None:
            return

        results = monkey.run_chaos_test_suite(1, runner)
        self.assertGreater(int(results["total_operations"]), 0)
        self.assertGreaterEqual(int(results["failed_operations"]), 0)


if __name__ == "__main__":
    unittest.main()

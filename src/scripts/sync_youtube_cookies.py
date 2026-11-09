"""Export the current local Chrome YouTube session through CDP for yt-dlp."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from websockets.sync.client import connect


_DOMAINS = ("youtube.com", "google.com")


def _cdp_websocket_url(cdp_url: str) -> str:
    with urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=10) as response:  # noqa: S310 - local CDP URL only
        payload = json.load(response)
    websocket_url = payload.get("webSocketDebuggerUrl")
    if not isinstance(websocket_url, str):
        raise RuntimeError("CDP endpoint did not provide a browser WebSocket URL")
    return websocket_url


def _youtube_cookies(cdp_url: str) -> list[dict]:
    websocket_url = _cdp_websocket_url(cdp_url)
    parsed = urlparse(cdp_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    with connect(websocket_url, origin=origin, open_timeout=10, close_timeout=5) as socket:
        socket.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            message = json.loads(socket.recv())
            if message.get("id") != 1:
                continue
            if "error" in message:
                raise RuntimeError(f"CDP Storage.getCookies failed: {message['error']}")
            cookies = message.get("result", {}).get("cookies", [])
            return [cookie for cookie in cookies if _is_youtube_cookie(cookie)]


def _is_youtube_cookie(cookie: dict) -> bool:
    domain = str(cookie.get("domain", "")).lstrip(".").lower()
    return any(domain == suffix or domain.endswith(f".{suffix}") for suffix in _DOMAINS)


def _netscape_cookie_file(cookies: list[dict]) -> bytes:
    rows = ["# Netscape HTTP Cookie File", "# Refreshed from local Chrome CDP."]
    for cookie in sorted(cookies, key=lambda item: (str(item.get("domain", "")), str(item.get("name", "")))):
        name, value = cookie.get("name"), cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or not name:
            continue
        domain = str(cookie.get("domain", ""))
        if not domain:
            continue
        if cookie.get("httpOnly"):
            domain = f"#HttpOnly_{domain}"
        expires = max(0, int(cookie.get("expires", 0)))
        rows.append("\t".join((
            domain,
            "TRUE" if str(cookie.get("domain", "")).startswith(".") else "FALSE",
            str(cookie.get("path") or "/"),
            "TRUE" if cookie.get("secure") else "FALSE",
            str(expires),
            name,
            value,
        )))
    return ("\n".join(rows) + "\n").encode()


def _write_in_place(path: Path, content: bytes) -> bool:
    """Preserve the bind-mounted inode while safely replacing its contents."""
    try:
        if path.read_bytes() == content:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.ftruncate(fd, 0)
        os.write(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-url", default=os.environ.get("YOUTUBE_CDP_URL", "http://127.0.0.1:9224"))
    parser.add_argument("--cookie-file", type=Path, default=Path(os.environ.get("YOUTUBE_COOKIE_FILE", "cookies.txt")))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    cookies = _youtube_cookies(args.cdp_url)
    if not cookies:
        raise RuntimeError("No YouTube/Google cookies found in the configured CDP browser")
    content = _netscape_cookie_file(cookies)
    changed = False if args.dry_run else _write_in_place(args.cookie_file, content)
    print(f"YouTube cookie sync: cookies={len(cookies)} changed={changed} file={args.cookie_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"YouTube cookie sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

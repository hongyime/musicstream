from src.scripts.sync_youtube_cookies import _is_youtube_cookie, _netscape_cookie_file, _write_in_place


def test_cookie_export_filters_domains_and_preserves_existing_inode(tmp_path):
    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text("old\n")
    inode = cookie_path.stat().st_ino
    cookies = [
        {"domain": ".youtube.com", "path": "/", "secure": True, "httpOnly": True, "expires": 1234.9, "name": "SID", "value": "secret"},
        {"domain": ".example.com", "name": "ignore", "value": "secret"},
    ]

    assert _is_youtube_cookie(cookies[0]) is True
    assert _is_youtube_cookie(cookies[1]) is False
    assert _write_in_place(cookie_path, _netscape_cookie_file([cookies[0]])) is True
    assert cookie_path.stat().st_ino == inode
    assert cookie_path.stat().st_mode & 0o777 == 0o600
    assert "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1234\tSID\tsecret" in cookie_path.read_text()

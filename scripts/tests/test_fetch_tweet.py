"""Tests for fetch-tweet.py pure functions."""
import json
import sys
import tempfile
from pathlib import Path

# Add lib to path so we can import
sys.path.insert(0, str(Path(__file__).parent.parent))

# We test the pure functions by importing them directly after a minimal shim
import importlib.util, types

# Load fetch-tweet as module (it uses __name__ guard so main() won't run)
spec = importlib.util.spec_from_file_location(
    "fetch_tweet",
    Path(__file__).parent.parent / "fetch-tweet.py",
)
ft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ft)


def test_extract_url_finds_x_status():
    urls = ft.extract_urls_from_input("check this https://x.com/Ole_S_Hansen/status/12345 please")
    assert urls == ["https://x.com/Ole_S_Hansen/status/12345"]


def test_extract_url_returns_empty_when_absent():
    assert ft.extract_urls_from_input("no url here") == []


def test_extract_url_finds_multiple():
    text = "look at https://x.com/foo/status/111 and https://x.com/bar/status/222"
    urls = ft.extract_urls_from_input(text)
    assert urls == ["https://x.com/foo/status/111", "https://x.com/bar/status/222"]


def test_extract_tweet_id():
    assert ft.extract_tweet_id("https://x.com/user/status/99887766") == "99887766"


def test_format_output_basic():
    data = {"url": "https://x.com/foo/status/1", "author": "foo", "text": "hello", "images": [], "quoted_tweet": None, "reply_to": None}
    out = ft.format_output(data, [])
    assert "[Tweet fetched:" in out
    assert "@foo" in out
    assert "hello" in out


def test_format_output_with_image():
    data = {"url": "https://x.com/foo/status/1", "author": "foo", "text": "chart", "images": [{"url": "http://img"}], "quoted_tweet": None, "reply_to": None}
    out = ft.format_output(data, ["/tmp/img-0.jpg"])
    assert "img-0.jpg" in out


def test_format_output_with_quoted_tweet():
    qt = {"author": "bar", "text": "original", "images": []}
    data = {"url": "https://x.com/foo/status/1", "author": "foo", "text": "re:", "images": [], "quoted_tweet": qt, "reply_to": None}
    out = ft.format_output(data, [])
    assert "Quoted: @bar" in out


def test_cache_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        ft._CACHE_DIR = Path(tmp)
        tweet_data = {"id": "123", "text": "test"}
        ft.save_cache("123", tweet_data)
        loaded = ft.load_cache("123")
        assert loaded == tweet_data


def test_cache_ttl_eviction():
    with tempfile.TemporaryDirectory() as tmp:
        ft._CACHE_DIR = Path(tmp)
        tweet_data = {"id": "456", "text": "stale"}
        ft.save_cache("456", tweet_data)
        # Backdate the file mtime by 25 hours
        cache_file = Path(tmp) / "456" / "tweet.json"
        old_mtime = cache_file.stat().st_mtime - (25 * 3600)
        import os
        os.utime(cache_file, (old_mtime, old_mtime))
        # Should return None and delete the directory
        result = ft.load_cache("456")
        assert result is None
        assert not (Path(tmp) / "456").exists()

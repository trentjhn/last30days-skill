#!/usr/bin/env python3
"""
fetch-tweet.py — Twitter URL fetch wrapper for Claude Code UserPromptSubmit hook.

Reads JSON from stdin (Claude Code hook format), extracts x.com status URLs,
fetches tweet data via bird-fetch.mjs, downloads images to local cache,
and outputs a structured text block for injection as system-reminder.

Usage (hook via stdin): echo '{"prompt": "...url..."}' | python3 fetch-tweet.py
Usage (direct URL):     python3 fetch-tweet.py https://x.com/user/status/ID
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# --- Paths ---
_BIRD_FETCH_MJS = Path(__file__).parent / "lib" / "vendor" / "bird-search" / "bird-fetch.mjs"
_CACHE_DIR = Path.home() / ".claude" / "tweet-cache"
_CACHE_TTL_HOURS = 24


# --- Credentials ---
def _load_credentials():
    """Load AUTH_TOKEN and CT0 from environment or last30days config."""
    if os.environ.get('AUTH_TOKEN') and os.environ.get('CT0'):
        return {'AUTH_TOKEN': os.environ['AUTH_TOKEN'], 'CT0': os.environ['CT0']}

    creds = {}
    config_paths = [
        Path.home() / '.config' / 'last30days' / '.env',
        Path(__file__).parent.parent / '.env',
    ]
    for path in config_paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key in ('AUTH_TOKEN', 'CT0') and val:
                creds[key] = val
        if len(creds) == 2:
            return creds
    return creds


def _subprocess_env(creds):
    env = os.environ.copy()
    env.update(creds)
    if creds.get('AUTH_TOKEN') and creds.get('CT0'):
        env.setdefault('BIRD_DISABLE_BROWSER_COOKIES', '1')
    return env


# --- URL helpers ---
def extract_url_from_input(text):
    """Extract first x.com status URL from text (preserves original case/format)."""
    match = re.search(r'https?://(?:www\.)?x\.com/[^/\s]+/status/(\d+)', text)
    return match.group(0) if match else None


def extract_tweet_id(url):
    match = re.search(r'/status/(\d+)', url)
    return match.group(1) if match else None


# --- Cache ---
def get_cache_dir(tweet_id):
    d = _CACHE_DIR / tweet_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_cache(tweet_id):
    cache_dir = _CACHE_DIR / tweet_id
    cache_file = cache_dir / 'tweet.json'
    if not cache_file.exists():
        return None
    age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
    if age_hours > _CACHE_TTL_HOURS:
        shutil.rmtree(cache_dir, ignore_errors=True)
        return None
    try:
        return json.loads(cache_file.read_text(encoding='utf-8'))
    except Exception:
        return None


def save_cache(tweet_id, data):
    cache_file = _CACHE_DIR / tweet_id / 'tweet.json'
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(data, indent=2), encoding='utf-8')


# --- Image download ---
def download_images(tweet_data, cache_dir):
    """Download photo images to cache dir. Returns list of local paths."""
    local_paths = []
    for i, img in enumerate(tweet_data.get('images', [])):
        url = img.get('url')
        if not url:
            continue
        fetch_url = url if ':' in url.split('/')[-1] else f'{url}:large'
        local_path = cache_dir / f'img-{i}.jpg'
        if local_path.exists():
            local_paths.append(str(local_path))
            continue
        try:
            req = urllib.request.Request(
                fetch_url,
                headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                local_path.write_bytes(resp.read())
            local_paths.append(str(local_path))
        except Exception:
            pass  # Skip failed image, continue
    return local_paths


# --- Fetch ---
def fetch_tweet(url, creds):
    """Run bird-fetch.mjs, return (tweet_data_dict, error_str)."""
    if not shutil.which('node'):
        return None, 'node not found in PATH'
    if not _BIRD_FETCH_MJS.exists():
        return None, f'bird-fetch.mjs not found at {_BIRD_FETCH_MJS}'
    try:
        result = subprocess.run(
            ['node', str(_BIRD_FETCH_MJS), '--url', url],
            capture_output=True,
            text=True,
            timeout=30,
            env=_subprocess_env(creds),
        )
        if result.returncode != 0:
            return None, (result.stderr or 'bird-fetch failed').strip()
        return json.loads(result.stdout), None
    except subprocess.TimeoutExpired:
        return None, 'Timed out after 30s'
    except json.JSONDecodeError as e:
        return None, f'Invalid JSON from bird-fetch: {e}'
    except Exception as e:
        return None, str(e)


# --- Output formatting ---
def format_output(tweet_data, local_image_paths):
    """Format tweet data as hook-injectable text block."""
    lines = [f'[Tweet fetched: {tweet_data["url"]}]']
    lines.append(f'Author: @{tweet_data["author"]}')
    lines.append(f'Text: "{tweet_data["text"]}"')

    if local_image_paths:
        lines.append('Images: ' + ', '.join(local_image_paths))

    qt = tweet_data.get('quoted_tweet')
    if qt and qt.get('author') and qt.get('text'):
        lines.append(f'Quoted: @{qt["author"]} — "{qt["text"]}"')

    rt = tweet_data.get('reply_to')
    if rt and rt.get('author') and rt.get('text'):
        lines.append(f'Reply to: @{rt["author"]} — "{rt["text"]}"')

    return '\n'.join(lines)


# --- Main ---
def main():
    # Determine URL source: direct arg or stdin JSON
    if len(sys.argv) > 1 and sys.argv[1].startswith('http'):
        url = sys.argv[1]
    else:
        raw = sys.stdin.read()
        try:
            data = json.loads(raw)
            prompt_text = (
                data.get('prompt') or
                data.get('message') or
                data.get('userPrompt') or
                ' '.join(str(v) for v in data.values() if isinstance(v, str))
            )
        except Exception:
            prompt_text = raw
        url = extract_url_from_input(prompt_text)

    if not url:
        sys.exit(0)  # No URL — silent exit, don't disrupt conversation

    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        sys.exit(0)

    creds = _load_credentials()
    if not creds.get('AUTH_TOKEN') or not creds.get('CT0'):
        sys.exit(0)  # No credentials — silent exit

    cache_dir = get_cache_dir(tweet_id)

    # Use cache if available
    cached = load_cache(tweet_id)
    if cached:
        tweet_data = cached
    else:
        tweet_data, error = fetch_tweet(url, creds)
        if error or not tweet_data:
            sys.stdout.write(f'[Tweet fetch failed: {error or "unknown error"}]\n')
            sys.exit(0)
        save_cache(tweet_id, tweet_data)

    # Download images (skips already-cached files)
    local_image_paths = download_images(tweet_data, cache_dir)

    # Output the hook-injectable block
    sys.stdout.write(format_output(tweet_data, local_image_paths) + '\n')
    sys.exit(0)


if __name__ == '__main__':
    main()

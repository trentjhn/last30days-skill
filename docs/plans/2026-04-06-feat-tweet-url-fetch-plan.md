# Tweet URL Auto-Fetch Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use executing-plans (if available) or follow manually to implement this plan task-by-task.

**Goal:** When a user pastes an x.com/status URL in chat, automatically fetch tweet text, images, quoted tweet, and reply context — injecting it as context before Claude responds.

**Architecture:** A `UserPromptSubmit` hook detects x.com URLs in the prompt, calls a Python wrapper (`fetch-tweet.py`) that runs `bird-fetch.mjs` (new Node.js script) against Twitter's TweetDetail GraphQL endpoint, downloads images to `~/.claude/tweet-cache/`, and outputs a structured text block that Claude reads silently.

**Tech Stack:** Node.js 22+ (bird-fetch.mjs), Python 3 (fetch-tweet.py), Twitter GraphQL API, existing `TwitterClientBase` + `buildTweetDetailFeatures` + `parseTweetsFromInstructions` from vendor lib.

---

## Key File Locations

- New JS script: `/Users/t-rawww/.claude/skills/last30days/scripts/lib/vendor/bird-search/bird-fetch.mjs`
- New Python wrapper: `/Users/t-rawww/.claude/skills/last30days/scripts/lib/fetch-tweet.py`
- Hook config: `/Users/t-rawww/.claude/settings.json`
- Image cache: `~/.claude/tweet-cache/<tweet-id>/`
- Existing utils (import from): `./lib/twitter-client-utils.js` — has `parseTweetsFromInstructions`, `extractMedia`, `mapTweetResult`, `findTweetInInstructions`
- Existing features (import from): `./lib/twitter-client-features.js` — has `buildTweetDetailFeatures()`
- Existing constants (import from): `./lib/twitter-client-constants.js` — has `TWITTER_API_BASE`, `QUERY_IDS`
- Reference for credential pattern: `bird-search.mjs` (same resolveCredentials call, same client construction)
- Reference for Python subprocess pattern: `scripts/lib/bird_x.py` (`_run_bird_search`, `_subprocess_env`)

---

## Task 1: Write `bird-fetch.mjs`

**Files:**
- Create: `scripts/lib/vendor/bird-search/bird-fetch.mjs`

**Step 1: Create the file**

```javascript
#!/usr/bin/env node
/**
 * bird-fetch.mjs - Fetch a single tweet by URL using TweetDetail GraphQL.
 *
 * Usage:
 *   node bird-fetch.mjs --url <https://x.com/user/status/ID>
 *
 * Output: JSON to stdout with shape:
 *   { id, url, author, text, images[], quoted_tweet, reply_to }
 */

import { resolveCredentials } from './lib/cookies.js';
import { TwitterClientBase } from './lib/twitter-client-base.js';
import { buildTweetDetailFeatures } from './lib/twitter-client-features.js';
import { parseTweetsFromInstructions } from './lib/twitter-client-utils.js';
import { TWITTER_API_BASE } from './lib/twitter-client-constants.js';

// --- Arg parsing ---
const args = process.argv.slice(2);
let tweetUrl = null;

for (let i = 0; i < args.length; i++) {
  if (args[i] === '--url' && args[i + 1]) {
    tweetUrl = args[i + 1];
    i++;
  }
}

if (!tweetUrl) {
  process.stderr.write('Usage: node bird-fetch.mjs --url <x.com/status/ID>\n');
  process.exit(1);
}

// Extract tweet ID
const idMatch = tweetUrl.match(/\/status\/(\d+)/);
if (!idMatch) {
  process.stderr.write(`Error: Cannot extract tweet ID from: ${tweetUrl}\n`);
  process.exit(1);
}
const tweetId = idMatch[1];

// --- Main ---
try {
  const { cookies, warnings } = await resolveCredentials({});

  if (!cookies.authToken || !cookies.ct0) {
    const msg = warnings.length > 0 ? warnings.join('; ') : 'No Twitter credentials found';
    process.stderr.write(`Error: ${msg}\n`);
    process.exit(1);
  }

  const client = new TwitterClientBase({
    cookies: {
      authToken: cookies.authToken,
      ct0: cookies.ct0,
      cookieHeader: cookies.cookieHeader,
    },
    timeoutMs: 30000,
    quoteDepth: 1,
  });

  // Build request
  const features = buildTweetDetailFeatures();
  const variables = {
    focalTweetId: tweetId,
    referrer: 'tweet',
    count: 20,
    with_rux_injections: true,
    includePromotedContent: false,
    withCommunity: false,
    withQuickPromoteEligibilityTweetFields: false,
    withBirdwatchNotes: false,
    withVoice: false,
    withV2Timeline: true,
  };

  // Try each query ID (rotates; same retry pattern as bird-search.mjs)
  const queryIds = await client.getTweetDetailQueryIds();
  let instructions = null;
  let lastError = null;

  for (const queryId of queryIds) {
    const params = new URLSearchParams({ variables: JSON.stringify(variables) });
    const url = `${TWITTER_API_BASE}/${queryId}/TweetDetail?${params.toString()}`;

    try {
      const response = await client.fetchWithTimeout(url, {
        method: 'POST',
        headers: client.getHeaders(),
        body: JSON.stringify({ features, queryId }),
      });

      if (response.status === 404) { lastError = 'HTTP 404'; continue; }
      if (!response.ok) { lastError = `HTTP ${response.status}`; break; }

      const data = await response.json();
      if (data.errors?.length > 0) { lastError = data.errors[0].message; continue; }

      instructions = data.data?.threaded_conversation_with_injections_v2?.instructions;
      if (instructions) break;
      lastError = 'No instructions in response';
    } catch (err) {
      lastError = err.message;
    }
  }

  if (!instructions) {
    process.stderr.write(`Error: ${lastError ?? 'Failed to fetch tweet'}\n`);
    process.exit(1);
  }

  // Parse all tweets in the thread (quoteDepth:1 for quoted tweets)
  const tweets = parseTweetsFromInstructions(instructions, { quoteDepth: 1 });
  const focal = tweets.find(t => t.id === tweetId) ?? tweets[0];

  if (!focal) {
    process.stderr.write(`Error: Tweet ${tweetId} not found in TweetDetail response\n`);
    process.exit(1);
  }

  // Find parent if this is a reply
  let replyTo = null;
  if (focal.inReplyToStatusId) {
    const parent = tweets.find(t => t.id === focal.inReplyToStatusId);
    if (parent) {
      replyTo = {
        id: parent.id,
        author: parent.author?.username ?? null,
        text: parent.text ?? null,
      };
    }
  }

  // Shape output
  const output = {
    id: focal.id,
    url: tweetUrl,
    author: focal.author?.username ?? null,
    text: focal.text ?? null,
    images: (focal.media ?? [])
      .filter(m => m.type === 'photo')
      .map(m => ({ url: m.url, width: m.width ?? null, height: m.height ?? null })),
    quoted_tweet: focal.quotedTweet
      ? {
          id: focal.quotedTweet.id,
          author: focal.quotedTweet.author?.username ?? null,
          text: focal.quotedTweet.text ?? null,
          images: (focal.quotedTweet.media ?? [])
            .filter(m => m.type === 'photo')
            .map(m => ({ url: m.url })),
        }
      : null,
    reply_to: replyTo,
  };

  process.stdout.write(JSON.stringify(output, null, 2) + '\n');
  process.exit(0);

} catch (err) {
  process.stderr.write(`Error: ${err.message}\n`);
  process.exit(1);
}
```

**Step 2: Smoke-test the script directly**

```bash
cd /Users/t-rawww/.claude/skills/last30days/scripts/lib/vendor/bird-search
node bird-fetch.mjs --url https://x.com/Ole_S_Hansen/status/2041192379428884534
```

Expected: JSON output with `id`, `author`, `text`, `images` array.  
If 404 on first query ID: it will retry with fallback IDs automatically.  
If auth error: check `AUTH_TOKEN`/`CT0` are set in environment or last30days config.

**Step 3: Commit**

```bash
cd /Users/t-rawww/.claude/skills/last30days
git add scripts/lib/vendor/bird-search/bird-fetch.mjs
git commit -m "feat: add bird-fetch.mjs — fetch single tweet by URL via TweetDetail GraphQL"
```

---

## Task 2: Write `fetch-tweet.py`

**Files:**
- Create: `scripts/lib/fetch-tweet.py`

**Step 1: Write the tests first**

Create `scripts/tests/test_fetch_tweet.py`:

```python
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
    url = ft.extract_url_from_input("check this https://x.com/Ole_S_Hansen/status/12345 please")
    assert url == "https://x.com/Ole_S_Hansen/status/12345"


def test_extract_url_returns_none_when_absent():
    assert ft.extract_url_from_input("no url here") is None


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
        cache_dir = Path(tmp)
        tweet_data = {"id": "123", "text": "test"}
        ft._CACHE_DIR = Path(tmp)
        ft.save_cache("123", tweet_data)
        loaded = ft.load_cache("123")
        assert loaded == tweet_data
```

**Step 2: Run tests — verify they fail**

```bash
cd /Users/t-rawww/.claude/skills/last30days
python3 -m pytest scripts/tests/test_fetch_tweet.py -v 2>&1 | head -20
```

Expected: `ImportError` or `ModuleNotFoundError` since `fetch-tweet.py` doesn't exist yet.

**Step 3: Create `fetch-tweet.py`**

```python
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
import urllib.request
from pathlib import Path

# --- Paths ---
_BIRD_FETCH_MJS = Path(__file__).parent / "vendor" / "bird-search" / "bird-fetch.mjs"
_CACHE_DIR = Path.home() / ".claude" / "tweet-cache"


# --- Credentials ---
def _load_credentials():
    """Load AUTH_TOKEN and CT0 from environment or last30days config."""
    if os.environ.get('AUTH_TOKEN') and os.environ.get('CT0'):
        return {'AUTH_TOKEN': os.environ['AUTH_TOKEN'], 'CT0': os.environ['CT0']}

    creds = {}
    config_paths = [
        Path.home() / '.config' / 'last30days' / 'config.env',
        Path(__file__).parent.parent.parent / '.env',
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
    cache_file = _CACHE_DIR / tweet_id / 'tweet.json'
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding='utf-8'))
        except Exception:
            pass
    return None


def save_cache(tweet_id, data):
    cache_file = _CACHE_DIR / tweet_id / 'tweet.json'
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
```

**Step 4: Run tests — verify they pass**

```bash
cd /Users/t-rawww/.claude/skills/last30days
python3 -m pytest scripts/tests/test_fetch_tweet.py -v
```

Expected output:
```
PASSED test_extract_url_finds_x_status
PASSED test_extract_url_returns_none_when_absent
PASSED test_extract_tweet_id
PASSED test_format_output_basic
PASSED test_format_output_with_image
PASSED test_format_output_with_quoted_tweet
PASSED test_cache_roundtrip
7 passed
```

**Step 5: Integration test — direct URL mode**

```bash
python3 /Users/t-rawww/.claude/skills/last30days/scripts/lib/fetch-tweet.py \
  https://x.com/Ole_S_Hansen/status/2041192379428884534
```

Expected: formatted text block with Author, Text, and Images (if any).

**Step 6: Integration test — stdin JSON mode (simulates hook)**

```bash
echo '{"prompt": "look at this https://x.com/Ole_S_Hansen/status/2041192379428884534 what do you think"}' | \
  python3 /Users/t-rawww/.claude/skills/last30days/scripts/lib/fetch-tweet.py
```

Expected: same formatted text block.

**Step 7: Commit**

```bash
cd /Users/t-rawww/.claude/skills/last30days
git add scripts/lib/fetch-tweet.py scripts/tests/test_fetch_tweet.py
git commit -m "feat: add fetch-tweet.py — Python hook wrapper with image download and caching"
```

---

## Task 3: Wire the hook in `settings.json`

**Files:**
- Modify: `/Users/t-rawww/.claude/settings.json`

**Step 1: Read current settings**

```bash
cat /Users/t-rawww/.claude/settings.json
```

Current `UserPromptSubmit` structure:
```json
"UserPromptSubmit": [
  {
    "hooks": [
      { "type": "command", "command": "bash ~/.claude/hooks/skill-activation.sh" }
    ]
  }
]
```

**Step 2: Add tweet-fetch hook entry**

Add a second object to the `UserPromptSubmit` array with a matcher. The final array should be:

```json
"UserPromptSubmit": [
  {
    "hooks": [
      { "type": "command", "command": "bash ~/.claude/hooks/skill-activation.sh" }
    ]
  },
  {
    "matcher": "x\\.com/[^/\\s]+/status/[0-9]+",
    "hooks": [
      {
        "type": "command",
        "command": "python3 /Users/t-rawww/.claude/skills/last30days/scripts/lib/fetch-tweet.py"
      }
    ]
  }
]
```

The `matcher` field is a regex tested against the user's prompt. When matched, the hook command receives the full prompt JSON on stdin (same as the skill-activation hook).

**Step 3: Verify settings.json is valid JSON**

```bash
python3 -c "import json; json.load(open('/Users/t-rawww/.claude/settings.json')); print('valid')"
```

Expected: `valid`

---

## Task 4: End-to-end test

**Step 1: Test hook execution manually**

```bash
echo '{"prompt": "check out https://x.com/Ole_S_Hansen/status/2041192379428884534"}' | \
  python3 /Users/t-rawww/.claude/skills/last30days/scripts/lib/fetch-tweet.py
```

Expected: formatted text block appears on stdout.

**Step 2: Check cache was written**

```bash
ls ~/.claude/tweet-cache/2041192379428884534/
```

Expected: `tweet.json` + any `img-N.jpg` files.

**Step 3: Verify image files are readable**

```bash
file ~/.claude/tweet-cache/2041192379428884534/img-0.jpg 2>/dev/null || echo "no images in this tweet"
```

Expected: `JPEG image data` or `no images in this tweet`.

**Step 4: Test silent exit on no URL**

```bash
echo '{"prompt": "what do you think about CORN today?"}' | \
  python3 /Users/t-rawww/.claude/skills/last30days/scripts/lib/fetch-tweet.py
echo "exit code: $?"
```

Expected: no output, exit code 0.

**Step 5: Commit settings change**

```bash
cd /Users/t-rawww/.claude/skills/last30days
git add scripts/lib/fetch-tweet.py  # in case of any fixups
git diff /Users/t-rawww/.claude/settings.json  # verify the diff looks right
# settings.json is not in the last30days repo — no commit needed for it
```

Note: `settings.json` lives in `~/.claude/` which is not a git repo. No commit needed.

---

## Error Reference

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `bird-fetch.mjs` returns HTTP 404 | TweetDetail query ID rotated | Script retries with fallback IDs automatically |
| `No Twitter credentials found` | AUTH_TOKEN/CT0 not in env or config | Check `~/.config/last30days/config.env` has both keys |
| `node not found in PATH` | Node.js not installed | `brew install node` |
| Hook fires but no output in Claude context | Matcher not matching | Test with `echo '{"prompt":"...url..."}' | python3 fetch-tweet.py` |
| Image download 403 | CDN URL expired in cache | Delete `~/.claude/tweet-cache/<id>/` and retry |

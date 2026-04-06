# Design: Tweet URL Auto-Fetch with Images

**Date:** 2026-04-06  
**Status:** Approved — ready for implementation  
**Author:** TJ + Claude

---

## Problem

When a specific X post URL is pasted in chat, Claude cannot view it. The existing `bird-search.mjs` can search X by keyword/handle but has no mechanism to fetch a tweet by ID. Images in tweets (charts, screenshots) are entirely inaccessible.

---

## Goal

Paste any `x.com/*/status/*` URL in chat → Claude automatically fetches tweet text, images, quoted tweet, and one level of reply context → analyzes everything silently → responds without repeating back what the user can already see.

---

## Architecture

```
User pastes x.com URL in chat
         ↓
UserPromptSubmit hook fires
         ↓
fetch-tweet.py (new Python wrapper)
  ├── Regex-extracts tweet ID from URL
  ├── Checks ~/.claude/tweet-cache/<id>/tweet.json (skip if cached)
  ├── Calls bird-fetch.mjs --url <url>
  │     ├── Uses existing TwitterClientBase + TweetDetail query ID
  │     ├── Fetches: text, author, image URLs, quoted tweet, reply parent
  │     └── Returns structured JSON
  ├── Downloads images from pbs.twimg.com → ~/.claude/tweet-cache/<id>/img-N.jpg
  └── Outputs structured text block to stdout
         ↓
Hook injects output as system-reminder
         ↓
Claude reads image files via Read tool, analyzes silently
```

---

## Components

### 1. `bird-fetch.mjs`
New Node.js script alongside `bird-search.mjs` in the vendor directory.

- Accepts `--url <x.com/status/ID>` flag
- Parses tweet ID from URL
- Uses existing `TwitterClientBase` with `TweetDetail` query ID (`97JF30KziU00483E_8elBA`)
- Extracts from GraphQL response:
  - `text` — full tweet text (expanded URLs)
  - `author` — screen name
  - `images[]` — `{ url, width, height }` for each media item (photos only)
  - `quoted_tweet` — `{ id, author, text, images[] }` if present
  - `reply_to` — `{ id, author, text }` if tweet is a reply (one level only)
- Returns JSON to stdout, errors to stderr
- Reuses `bird_x.py`'s credential injection pattern (`AUTH_TOKEN` + `CT0` env vars)

### 2. `fetch-tweet.py`
New Python script in `scripts/lib/` (mirrors `bird_x.py` pattern).

- Called by the hook with a URL argument
- Checks cache: if `~/.claude/tweet-cache/<id>/tweet.json` exists, skip fetch
- Runs `bird-fetch.mjs` subprocess, captures JSON output
- Downloads each image URL to `~/.claude/tweet-cache/<id>/img-0.jpg`, `img-1.jpg`, etc.
  - Images on `pbs.twimg.com` are publicly accessible (no auth required for CDN)
- Saves `tweet.json` to cache directory
- Outputs hook-injectable text block:

```
[Tweet fetched: x.com/<author>/status/<id>]
Author: @<author>
Text: "<full text>"
Images: /Users/.../.claude/tweet-cache/<id>/img-0.jpg
Quoted: @<quoted_author> — "<quoted_text>"
Reply to: @<parent_author> — "<parent_text>"
```

### 3. Hook in `settings.json`
New `UserPromptSubmit` hook entry:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "x\\.com/.*/status/\\d+",
        "command": "python3 /Users/t-rawww/.claude/skills/last30days/scripts/lib/fetch-tweet.py \"$CLAUDE_USER_PROMPT\""
      }
    ]
  }
}
```

The hook matches any message containing an X status URL, extracts it, and runs the fetch. Output is injected as a `system-reminder` before Claude processes the message.

---

## Data Model

```json
{
  "id": "2038579381614850267",
  "url": "https://x.com/Ole_S_Hansen/status/2038579381614850267",
  "author": "Ole_S_Hansen",
  "text": "Full tweet text with expanded URLs...",
  "images": [
    { "url": "https://pbs.twimg.com/media/...", "local_path": "~/.claude/tweet-cache/.../img-0.jpg" }
  ],
  "quoted_tweet": {
    "id": "...",
    "author": "IamZeroIka",
    "text": "Soft commodities are looking good...",
    "images": []
  },
  "reply_to": null
}
```

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| No X credentials configured | Hook exits silently (code 0), no injection |
| Tweet fetch fails (rate limit, deleted, auth expired) | Hook injects `[Tweet fetch failed: <reason>]`, Claude acknowledges and asks user to paste text |
| No images in tweet | Image line omitted from hook output |
| Non-tweet x.com URL (profile, search) | Hook regex doesn't match, ignored |
| Image download fails | Skip that image, continue with others |
| Tweet already cached | Skip fetch, read from `tweet.json` + existing image files |

---

## Cache

```
~/.claude/tweet-cache/
  └── <tweet-id>/
        ├── tweet.json      ← raw structured data
        ├── img-0.jpg
        └── img-1.jpg
```

Persists across sessions. Same URL pasted twice → instant cache hit, no network call.

---

## Constraints & Non-Goals

- **Thread depth:** One reply parent only. Fetching full thread chains is out of scope.
- **Videos:** Not fetched. Video URLs included in text if present, not downloaded.
- **Claude output:** Claude reads and analyzes tweet content silently. Does not repeat back what the user can see.
- **Settings location:** Hook added to `~/.claude/settings.json` (global), not project-level.

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

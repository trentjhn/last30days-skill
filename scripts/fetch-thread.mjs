#!/usr/bin/env node
/**
 * fetch-thread.mjs — Dump the full author chain of an X thread to stdout.
 *
 * Companion to fetch-tweet.py (which only returns the focal tweet). Reuses the
 * vendored bird-search client to call TweetDetail once; the response already
 * contains the entire threaded conversation, so we just filter to tweets by
 * the focal author that trace back to the focal via inReplyToStatusId.
 *
 * Loads AUTH_TOKEN / CT0 from env, falling back to ~/.config/last30days/.env
 * so it can be invoked standalone without sourcing the env first.
 *
 * Usage:
 *   node fetch-thread.mjs <x.com/user/status/ID>
 *
 * Output:
 *   --- <tweet_id> ---
 *   <tweet text>
 *
 *   ... (repeats for each tweet in the author's chain, oldest first)
 *
 *   (total in chain: N; total in conversation: M)
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { TwitterClientBase } from '/Users/t-rawww/.claude/skills/last30days/scripts/lib/vendor/bird-search/lib/twitter-client-base.js';
import { buildTweetDetailFeatures } from '/Users/t-rawww/.claude/skills/last30days/scripts/lib/vendor/bird-search/lib/twitter-client-features.js';
import { parseTweetsFromInstructions } from '/Users/t-rawww/.claude/skills/last30days/scripts/lib/vendor/bird-search/lib/twitter-client-utils.js';
import { TWITTER_API_BASE } from '/Users/t-rawww/.claude/skills/last30days/scripts/lib/vendor/bird-search/lib/twitter-client-constants.js';

function loadEnvFile() {
  const envPath = path.join(os.homedir(), '.config', 'last30days', '.env');
  if (!fs.existsSync(envPath)) return;
  for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#') || !trimmed.includes('=')) continue;
    const [k, ...rest] = trimmed.split('=');
    const v = rest.join('=').trim().replace(/^["']|["']$/g, '');
    if (!process.env[k.trim()]) process.env[k.trim()] = v;
  }
}

const url = process.argv[2];
if (!url || !/\/status\/\d+/.test(url)) {
  process.stderr.write('Usage: node fetch-thread.mjs <x.com/.../status/ID>\n');
  process.exit(1);
}
const tweetId = url.match(/\/status\/(\d+)/)[1];

loadEnvFile();
const authToken = process.env.AUTH_TOKEN;
const ct0 = process.env.CT0;
if (!authToken || !ct0) {
  process.stderr.write('Error: AUTH_TOKEN and CT0 must be set (env or ~/.config/last30days/.env)\n');
  process.exit(1);
}

const client = new TwitterClientBase({
  cookies: { authToken, ct0, cookieHeader: `auth_token=${authToken}; ct0=${ct0}` },
  timeoutMs: 30000,
  quoteDepth: 1,
});

const features = buildTweetDetailFeatures();
const variables = {
  focalTweetId: tweetId, referrer: 'tweet', count: 100,
  with_rux_injections: true, includePromotedContent: false,
  withCommunity: false, withQuickPromoteEligibilityTweetFields: false,
  withBirdwatchNotes: false, withVoice: false, withV2Timeline: true,
};

const queryIds = await client.getTweetDetailQueryIds();
let instructions = null;
for (const queryId of queryIds) {
  const params = new URLSearchParams({ variables: JSON.stringify(variables) });
  const u = `${TWITTER_API_BASE}/${queryId}/TweetDetail?${params.toString()}`;
  const r = await client.fetchWithTimeout(u, {
    method: 'POST',
    headers: client.getHeaders(),
    body: JSON.stringify({ features, queryId }),
  });
  if (!r.ok) continue;
  const data = await r.json();
  instructions = data.data?.threaded_conversation_with_injections_v2?.instructions;
  if (instructions) break;
}
if (!instructions) {
  process.stderr.write('Error: Failed to fetch thread (all queryIds exhausted)\n');
  process.exit(1);
}

const tweets = parseTweetsFromInstructions(instructions, { quoteDepth: 1 });
const focal = tweets.find(t => t.id === tweetId);
if (!focal) {
  process.stderr.write(`Error: focal tweet ${tweetId} not in response\n`);
  process.exit(1);
}
const author = focal.author?.username;
const byId = new Map(tweets.map(t => [t.id, t]));

function tracesToFocal(t) {
  let cur = t;
  while (cur) {
    if (cur.id === tweetId) return true;
    if (!cur.inReplyToStatusId) return false;
    cur = byId.get(cur.inReplyToStatusId);
  }
  return false;
}

const chain = tweets
  .filter(t => t.author?.username === author && tracesToFocal(t))
  .sort((a, b) => (BigInt(a.id) < BigInt(b.id) ? -1 : 1));

for (const t of chain) {
  console.log(`--- ${t.id} ---`);
  console.log(t.text);
  console.log();
}
console.log(`(total in chain: ${chain.length}; total in conversation: ${tweets.length})`);

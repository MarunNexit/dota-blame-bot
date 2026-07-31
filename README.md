# Dota Blame Bot

Posts an automatic "who's guilty" analysis to a Discord channel after every
loss, using OpenDota match data. Runs as a scheduled script on GitHub
Actions (via webhook), not a persistent bot connection — see the note below.

## How it works

1. GitHub Actions runs `dota_blame_bot.py` every ~5 minutes.
2. The script asks OpenDota for your recent matches and compares against
   `last_match_id` saved in `state.json`.
3. For any new match:
   - if it was a **win**, it's skipped (no blame on wins)
   - if OpenDota hasn't parsed it yet, the script requests a parse and
     retries it on the next run (parsing usually takes 30s-2min)
   - if it was a **loss**, it scores every teammate on a "badness" scale
     built from KDA, kill participation, GPM/damage/net-worth share,
     ward/stun stats for supports, early deaths, and buyback usage — then
     turns those scores into guilt probabilities (softmax, sums to 100%)
     and posts an embed to your Discord webhook.
4. It keeps running per-teammate counters (games played together, times
   blamed) in `state.json`. Every 10 games with the same teammate
   (configurable), it posts a checkpoint summary for that person.
5. The workflow commits `state.json` back to the repo so state survives
   between runs (GitHub Actions runners are thrown away after each job).

## Why webhook instead of a "real" bot

A real Discord bot (`discord.py` `Client`, slash commands, etc.) needs a
process that stays connected to Discord 24/7. GitHub Actions only runs
jobs for a few minutes at a time on a schedule — it can't host a
long-running connection. A webhook post is the right tool for "post a
message automatically on a schedule" and works perfectly with GH Actions.
If you later want interactive commands (`/blame @someone`, etc.), that
needs a small always-on host instead (a $5/mo VPS, Railway, Fly.io, etc.)
running an actual bot process — happy to build that version too if you want it.

## Setup

### 1. Get your Steam account ID (32-bit)

This is **not** your 64-bit SteamID. Easiest way: go to
`https://www.opendota.com/players/<your_id>` — if you search your profile
on opendota.com, the number in the URL is what you need. Or convert your
SteamID64 by subtracting `76561197960265728`.

### 2. Create a Discord webhook

In your Discord server: **Channel Settings → Integrations → Webhooks →
New Webhook**. Copy the webhook URL.

### 3. (Optional) Get an OpenDota API key

Not required — the free tier works fine for one player polled every 5
minutes — but an API key raises your rate limit if you expand this later.
Get one at https://www.opendota.com/api-keys.

### 4. Create the GitHub repo

```
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<you>/dota-blame-bot.git
git push -u origin main
```

### 5. Add repository secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|---|---|
| `STEAM_ACCOUNT_ID` | your 32-bit account id from step 1 |
| `DISCORD_WEBHOOK_URL` | your webhook URL from step 2 |
| `OPENDOTA_API_KEY` | optional, from step 3 |

### 6. Enable Actions

The workflow at `.github/workflows/dota_blame.yml` is already set to run
every 5 minutes (`workflow_dispatch` also lets you trigger it manually
from the Actions tab to test it immediately). Make sure Actions is enabled
for the repo (Settings → Actions → General → Allow all actions).

That's it — after your next Dota game, within ~5-10 minutes (game
processing + OpenDota parse time) you should see a post in your channel.

## Config (all via env vars / secrets)

| Variable | Default | Meaning |
|---|---|---|
| `STEAM_ACCOUNT_ID` | required | your 32-bit account id |
| `DISCORD_WEBHOOK_URL` | required | webhook to post to |
| `STATE_FILE` | `state.json` | where counters/last-match are stored |
| `SUMMARY_EVERY` | `10` | post a checkpoint every N games with a teammate |
| `RECENT_MATCHES_LIMIT` | `5` | how many recent matches to check per run |
| `OPENDOTA_API_KEY` | none | optional, raises rate limits |

## Known limitations (be aware of these)

- **This is a heuristic, not a real verdict.** It's built from box-score
  stats (KDA, GPM, damage share, wards, stuns, early deaths, buybacks). It
  doesn't know about map decisions, calls, itemization quality, or who
  actually made the bad rotation — OpenDota's parsed data doesn't expose
  that level of detail. Treat the output as "for entertainment," not as
  ground truth, especially since it'll be reading this in your Discord.
- **Private Steam/Dota profiles**: if a teammate has their match history
  private, OpenDota can't resolve their `account_id` or nickname for that
  match, so they'll show as "Anonymous" and won't be tracked consistently
  across games (their anonymized ID isn't stable). Ask your regular
  stack to set "Expose Public Match Data" on in Dota's settings if you
  want reliable long-term tracking of them.
- **Unparsed matches**: OpenDota only auto-parses some matches. The bot
  requests a parse for anything new and just retries next cycle — so
  expect the post for a given game to sometimes land 5-15 minutes after
  the game actually ends, not immediately.
- **GitHub Actions cron isn't exact**: `*/5 * * * *` is a minimum
  interval, not a guarantee — under GitHub's load, runs can be delayed by
  several minutes.
- **Party detection**: "games played with the same person" is tracked
  per teammate individually (whoever's on your team that game), not by
  detecting a specific 5-stack. If you play with a mixed group it'll just
  track each person's own running total.

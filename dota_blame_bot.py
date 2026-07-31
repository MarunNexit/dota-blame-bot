#!/usr/bin/env python3
"""
Dota 2 "Who's Guilty" bot.

What it does, once per run:
  1. Loads state.json (last processed match, retry queue, per-player counters).
  2. Retries any matches that were queued for parsing last time.
  3. Fetches your recent matches from OpenDota.
  4. For every match newer than the last one seen:
       - skip it if it's not a loss (we only assign blame on losses)
       - if OpenDota hasn't parsed it yet, request a parse and queue a retry
       - otherwise, run the analysis and post an embed to Discord
  5. Updates per-teammate "games played together" / "times blamed" counters.
  6. Every SUMMARY_EVERY (default 10) games with the same teammate, posts a
     leaderboard summary for that teammate.
  7. Saves state.json.

This is meant to be run on a schedule (e.g. GitHub Actions cron) as a
one-shot script, not a long-running process. It talks to Discord via a
webhook, not a live bot connection.

Environment variables (see README.md):
  STEAM_ACCOUNT_ID       - your 32-bit Dota/Steam account id (required)
  DISCORD_WEBHOOK_URL    - Discord webhook URL to post to (required)
  STATE_FILE             - path to the JSON state file (default: state.json)
  SUMMARY_EVERY           - post a leaderboard every N games together (default: 10)
  RECENT_MATCHES_LIMIT    - how many recent matches to look at per run (default: 5)
  OPENDOTA_API_KEY        - optional OpenDota API key, raises your rate limit
"""

import json
import math
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests

from leaderboard import top_ruiners, top_allies

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

OPENDOTA_BASE = "https://api.opendota.com/api"

STEAM_ACCOUNT_ID = os.environ.get("STEAM_ACCOUNT_ID")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SUMMARY_EVERY = int(os.environ.get("SUMMARY_EVERY", "10"))
RECENT_MATCHES_LIMIT = int(os.environ.get("RECENT_MATCHES_LIMIT", "50"))
OPENDOTA_API_KEY = os.environ.get("OPENDOTA_API_KEY")  # optional

if not STEAM_ACCOUNT_ID or not DISCORD_WEBHOOK_URL:
    print("ERROR: STEAM_ACCOUNT_ID and DISCORD_WEBHOOK_URL must be set as env vars.")
    sys.exit(1)

STEAM_ACCOUNT_ID = int(STEAM_ACCOUNT_ID)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "dota-blame-bot/1.0"})


# --------------------------------------------------------------------------
# Small OpenDota client (with basic retry / rate-limit friendliness)
# --------------------------------------------------------------------------

def opendota_get(path: str, params: Optional[dict] = None, retries: int = 3) -> Any:
    params = dict(params or {})
    if OPENDOTA_API_KEY:
        params["api_key"] = OPENDOTA_API_KEY

    url = f"{OPENDOTA_BASE}{path}"
    for attempt in range(1, retries + 1):
        resp = SESSION.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = 5 * attempt
            print(f"  rate limited on {path}, waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(2 * attempt)
            continue
        # 4xx other than 429: don't retry
        print(f"  OpenDota GET {path} failed: {resp.status_code} {resp.text[:200]}")
        return None
    return None


def opendota_post(path: str) -> Any:
    url = f"{OPENDOTA_BASE}{path}"
    params = {}
    if OPENDOTA_API_KEY:
        params["api_key"] = OPENDOTA_API_KEY
    try:
        resp = SESSION.post(url, params=params, timeout=30)
        if resp.status_code in (200, 201):
            return resp.json()
    except requests.RequestException as e:
        print(f"  OpenDota POST {path} error: {e}")
    return None


def fetch_recent_matches(account_id: int, limit: int) -> List[dict]:
    data = opendota_get(f"/players/{account_id}/matches", {"limit": limit})
    return data or []


def fetch_match(match_id: int) -> Optional[dict]:
    return opendota_get(f"/matches/{match_id}")


def request_parse(match_id: int) -> None:
    print(f"  requesting parse for match {match_id}")
    opendota_post(f"/request/{match_id}")


_HERO_CACHE: Dict[int, str] = {}


def hero_name(hero_id: int) -> str:
    if not _HERO_CACHE:
        heroes = opendota_get("/heroes") or []
        for h in heroes:
            _HERO_CACHE[h["id"]] = h.get("localized_name", f"Hero {h['id']}")
    return _HERO_CACHE.get(hero_id, f"Hero {hero_id}")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def default_state() -> dict:
    return {
        "last_match_id": 0,
        "pending_parse": [],

        # player statistics
        "players": {},

        # every analyzed loss
        "history": []
    }


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        # backfill any missing keys if the schema grows later
        for k, v in default_state().items():
            state.setdefault(k, v)
        return state
    return default_state()


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Match helpers
# --------------------------------------------------------------------------

def is_parsed(match: dict) -> bool:
    """Heuristic: a parsed match has purchase_log / obs_log populated."""
    players = match.get("players", [])
    if not players:
        return False
    return any(p.get("purchase_log") not in (None, []) for p in players)


def find_my_player(match: dict, account_id: int, fallback_slot: Optional[int]) -> Optional[dict]:
    for p in match.get("players", []):
        if p.get("account_id") == account_id:
            return p
    # private profile: account_id may be null in the match detail, fall back
    # to the player_slot we already knew from the recent-matches list
    if fallback_slot is not None:
        for p in match.get("players", []):
            if p.get("player_slot") == fallback_slot:
                return p
    return None


def is_radiant_slot(player_slot: int) -> bool:
    return player_slot < 128


def did_i_lose(match: dict, my_player: dict) -> bool:
    my_radiant = is_radiant_slot(my_player["player_slot"])
    radiant_win = match.get("radiant_win")
    return my_radiant != radiant_win


def get_team(match: dict, my_player: dict) -> List[dict]:
    my_radiant = is_radiant_slot(my_player["player_slot"])
    return [
        p for p in match.get("players", [])
        if is_radiant_slot(p["player_slot"]) == my_radiant
    ]


def player_nickname(p: dict) -> str:
    name = p.get("personaname")
    if name:
        return name
    acc = p.get("account_id")
    if acc:
        return f"Anonymous#{acc}"
    return f"Anonymous (hidden profile)"


def early_death_count(p: dict, cutoff_seconds: int = 600) -> int:
    """Count deaths that happened before `cutoff_seconds` using life_state,
    if OpenDota gave us that timeline. Returns -1 if unavailable."""
    life_state = p.get("life_state")
    if not life_state:
        return -1
    count = 0
    was_alive = True
    for second, state in enumerate(life_state):
        if second > cutoff_seconds:
            break
        dead = state == 1
        if dead and was_alive:
            count += 1
        was_alive = not dead
    return count


# --------------------------------------------------------------------------
# Blame analysis
# --------------------------------------------------------------------------

def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def analyze_team(match: dict, team: List[dict]) -> List[dict]:
    """Returns a list of dicts, one per teammate, with computed features,
    a 0-100 'badness' score, and a normalized guilt probability."""
    duration_min = max(1.0, match.get("duration", 0) / 60.0)

    team_kills = sum(p.get("kills", 0) for p in team) or 1
    team_gpm = sum(p.get("gold_per_min", 0) for p in team) or 1
    team_dmg = sum(p.get("hero_damage", 0) for p in team) or 1
    team_networth = sum(p.get("net_worth", p.get("total_gold", 0)) for p in team) or 1
    avg_gpm = team_gpm / len(team)

    analyzed = []
    for p in team:
        kills = p.get("kills", 0)
        deaths = p.get("deaths", 0)
        assists = p.get("assists", 0)
        gpm = p.get("gold_per_min", 0)
        hero_damage = p.get("hero_damage", 0)
        net_worth = p.get("net_worth", p.get("total_gold", 0))
        obs = p.get("obs_placed", 0)
        sen = p.get("sen_placed", 0)
        stuns = p.get("stuns", 0) or 0
        buyback_count = len(p.get("buyback_log", []) or [])
        early_deaths = early_death_count(p)

        kill_participation = safe_div(kills + assists, team_kills)
        gpm_share = safe_div(gpm, team_gpm)
        dmg_share = safe_div(hero_damage, team_dmg)
        networth_share = safe_div(net_worth, team_networth)
        death_rate = deaths / duration_min

        is_support = gpm < avg_gpm and (obs + sen) > 0

        # ---- badness score (higher = more to blame) ----
        badness = 0.0

        # Dying a lot, especially with little to show for it, is always bad.
        badness += death_rate * 9.0
        if kills + assists < deaths:
            badness += 6.0

        # Low participation in fights.
        badness += (1.0 - kill_participation) * 18.0

        # Early deaths (feeding the laning stage) hurt disproportionately.
        if early_deaths >= 0:
            badness += early_deaths * 5.0

        if is_support:
            # Supports are judged on utility, not gold/damage.
            badness += max(0.0, (1.0 - safe_div(obs + sen, 8))) * 10.0
            badness += max(0.0, (400 - stuns) / 100.0) * 2.0
        else:
            # Cores are judged on economy/damage share.
            badness += max(0.0, (0.22 - gpm_share)) * 60.0
            badness += max(0.0, (0.22 - dmg_share)) * 45.0
            badness += max(0.0, (0.22 - networth_share)) * 40.0

        # Repeated buybacks without a corresponding good outcome is a minor flag;
        # we don't have "did it help" so just weight it lightly.
        badness += buyback_count * 1.5

        badness = max(0.0, badness)

        analyzed.append({
            "account_id": p.get("account_id"),
            "nickname": player_nickname(p),
            "hero": hero_name(p.get("hero_id", 0)),
            "is_support": is_support,
            "kills": kills, "deaths": deaths, "assists": assists,
            "gpm": gpm, "xpm": p.get("xp_per_min", 0),
            "hero_damage": hero_damage,
            "net_worth": net_worth,
            "kill_participation": kill_participation,
            "gpm_share": gpm_share,
            "dmg_share": dmg_share,
            "networth_share": networth_share,
            "obs_placed": obs, "sen_placed": sen,
            "stuns": round(stuns, 1),
            "early_deaths": early_deaths,
            "buyback_count": buyback_count,
            "badness": badness,
        })

    # Softmax the badness scores into "guilt probabilities" that sum to 1.
    scores = [a["badness"] for a in analyzed]
    m = max(scores) if scores else 0.0
    exps = [math.exp((s - m) * 0.12) for s in scores]  # lower = more even spread
    total = sum(exps) or 1.0
    for a, e in zip(analyzed, exps):
        a["guilt_probability"] = e / total

    analyzed.sort(key=lambda a: a["guilt_probability"], reverse=True)
    return analyzed


def build_facts(a: dict) -> List[str]:
    facts = []
    facts.append(f"KDA {a['kills']}/{a['deaths']}/{a['assists']} "
                  f"(kill participation {a['kill_participation']*100:.0f}%)")
    if a["is_support"]:
        facts.append(f"Wards: {a['obs_placed']} obs / {a['sen_placed']} sen, "
                      f"{a['stuns']:.0f}s of stuns")
    else:
        facts.append(f"GPM {a['gpm']} ({a['gpm_share']*100:.0f}% of team), "
                      f"hero dmg share {a['dmg_share']*100:.0f}%, "
                      f"net worth share {a['networth_share']*100:.0f}%")
    if a["early_deaths"] >= 0:
        facts.append(f"Died {a['early_deaths']}x before the 10 minute mark")
    if a["buyback_count"]:
        facts.append(f"Used buyback {a['buyback_count']}x")
    return facts


# --------------------------------------------------------------------------
# Discord posting
# --------------------------------------------------------------------------

def post_to_discord(embed: dict, content: Optional[str] = None) -> None:
    payload = {"embeds": [embed]}
    if content:
        payload["content"] = content
    try:
        resp = SESSION.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if resp.status_code >= 300:
            print(f"  Discord post failed: {resp.status_code} {resp.text[:300]}")
    except requests.RequestException as e:
        print(f"  Discord post error: {e}")


def build_match_embed(match: dict, analyzed: List[dict]) -> dict:
    top = analyzed[0]
    duration_min = match.get("duration", 0) // 60
    duration_sec = match.get("duration", 0) % 60

    lines = []
    for rank, a in enumerate(analyzed, start=1):
        marker = "**" if rank == 1 else ""
        lines.append(
            f"{marker}{rank}. {a['nickname']} ({a['hero']}) — {a['guilt_probability']*100:.1f}%{marker}"
        )
    standings = "\n".join(lines)

    facts = "\n".join(f"• {f}" for f in build_facts(top))

    return {
        "title": f"💀 Loss analysis — match {match.get('match_id')}",
        "description": (
            f"Duration: {duration_min}m{duration_sec:02d}s\n\n"
            f"**Guilt probabilities:**\n{standings}\n\n"
            f"**Most likely guilty: {top['nickname']} ({top['hero']})**\n{facts}"
        ),
        "color": 0xE74C3C,
        "footer": {"text": "Heuristic analysis via OpenDota — not a real verdict, please don't actually fight."},
    }


def build_summary_embed(nickname: str, entry: dict) -> dict:
    games = entry["games_together"]
    blamed = entry["guilty_count"]
    pct = (blamed / games * 100) if games else 0.0
    return {
        "title": f"📊 10-game checkpoint: {nickname}",
        "description": (
            f"Games played together: **{games}**\n"
            f"Times found guilty: **{blamed}** ({pct:.0f}%)"
        ),
        "color": 0xF1C40F,
    }



def build_leaderboard_embed(state):

    ruiners = top_ruiners(state)
    allies = top_allies(state)


    ruiners_text = ""

    for i,p in enumerate(ruiners,1):

        ruiners_text += (
            f"**{i}. {p['name']}**\n"
            f"💀 {p['guilty']}/{p['games']} "
            f"({p['percent']:.1f}%)\n\n"
        )


    allies_text = ""

    for i,p in enumerate(allies,1):

        allies_text += (
            f"**{i}. {p['name']}**\n"
            f"🎮 {p['games']} games\n\n"
        )


    return {
        "title":"🏆 Dota Leaderboards",

        "description":
            "💀 **TOP RUINERS**\n\n"
            + ruiners_text
            +
            "\n━━━━━━━━━━━━━━\n\n"
            +
            "🤝 **TOP ALLIES**\n\n"
            + allies_text,

        "color":0x3498DB
    }

# --------------------------------------------------------------------------
# Counters
# --------------------------------------------------------------------------

def update_counters(
    state: dict,
    team: List[dict],
    analyzed: List[dict],
    guilty_account_id: Optional[int],
    match_id: int
) -> List[str]:

    hit_milestone = []

    # save match history
    state["history"].append({
        "match_id": match_id,
        "guilty": guilty_account_id,
        "players": [
            {
                "account_id": p.get("account_id"),
                "nickname": p.get("nickname"),
                "hero": p.get("hero"),
                "guilt_probability": p.get("guilt_probability")
            }
            for p in analyzed
        ]
    })


    for p in team:

        acc = p.get("account_id")

        if not acc or acc == STEAM_ACCOUNT_ID:
            continue


        key = str(acc)


        entry = state["players"].setdefault(
            key,
            {
                "nickname": player_nickname(p),
                "games_together": 0,
                "guilty_count": 0
            }
        )


        entry["nickname"] = player_nickname(p)

        entry["games_together"] += 1


        if acc == guilty_account_id:
            entry["guilty_count"] += 1


        if entry["games_together"] % SUMMARY_EVERY == 0:
            hit_milestone.append(key)


    return hit_milestone


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def process_match(state: dict, match_id: int) -> bool:
    """Returns True if the match was fully handled (parsed + posted or
    correctly skipped as a win), False if it should be retried later."""
    match = fetch_match(match_id)
    if not match:
        print(f"  could not fetch match {match_id}, will retry later")
        return False

    if not is_parsed(match):
        request_parse(match_id)
        return False

    # find our slot from the recent-matches list we cached, if we have it
    fallback_slot = state.get("_slot_cache", {}).get(str(match_id))
    my_player = find_my_player(match, STEAM_ACCOUNT_ID, fallback_slot)
    if not my_player:
        print(f"  couldn't locate our player in match {match_id}, skipping")
        return True  # nothing more we can do with this one

    if not did_i_lose(match, my_player):
        print(f"  match {match_id} was a win, skipping blame analysis")
        return True

    team = get_team(match, my_player)
    analyzed = analyze_team(match, team)
    guilty = analyzed[0]


    embed = build_match_embed(
    match,
    analyzed
)

    post_to_discord(embed)



    milestones = update_counters(
        state,
        team,
        analyzed,
        guilty.get("account_id"),
        match_id
    )

    # NEW MESSAGE AFTER EVERY GAME

    leaderboard_embed = build_leaderboard_embed(
        state
    )

    post_to_discord(
        leaderboard_embed
    )

    for key in milestones:
        entry = state["players"][key]
        summary_embed = build_summary_embed(entry["nickname"], entry)
        post_to_discord(summary_embed)

    return True


def main() -> None:
    state = load_state()
    state.setdefault("_slot_cache", {})

    # 1) retry anything still waiting on OpenDota's parser
    still_pending = []
    for mid in state.get("pending_parse", []):
        print(f"Retrying pending match {mid}")
        if not process_match(state, mid):
            still_pending.append(mid)
        else:
            state["last_match_id"] = max(state["last_match_id"], mid)
    state["pending_parse"] = still_pending

    # 2) look for new matches
    recent = fetch_recent_matches(STEAM_ACCOUNT_ID, RECENT_MATCHES_LIMIT)
    new_matches = [m for m in recent if m["match_id"] > state["last_match_id"]]
    new_matches.sort(key=lambda m: m["match_id"])  # oldest first

    if not new_matches:
        print("No new matches.")
    for m in new_matches:
        mid = m["match_id"]
        state["_slot_cache"][str(mid)] = m.get("player_slot")
        print(f"Processing match {mid}...")
        done = process_match(state, mid)
        state["last_match_id"] = max(state["last_match_id"], mid)
        if not done:
            state["pending_parse"].append(mid)

    # trim slot cache so state.json doesn't grow forever
    keep_ids = {str(m["match_id"]) for m in recent} | set(state["pending_parse"])
    state["_slot_cache"] = {k: v for k, v in state["_slot_cache"].items() if k in map(str, keep_ids) or True}
    if len(state["_slot_cache"]) > 50:
        # keep only the most recent 50 by match id
        keys_sorted = sorted(state["_slot_cache"].keys(), key=lambda k: int(k), reverse=True)[:50]
        state["_slot_cache"] = {k: state["_slot_cache"][k] for k in keys_sorted}

    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
blame_calculator.py

All the "who's guilty" scoring logic, separated out from the bot/posting
code so it's easier to tune independently.

Pipeline, per loss:

  1. assign_roles()          -> guess each of the 5 players' role
                                 (carry / mid / offlane / support_hard / support_soft)
                                 using OpenDota's `lane_role` field plus a
                                 gold+xp "economy" tiebreak, since lane_role
                                 alone doesn't distinguish a laner from the
                                 support standing in the same lane.
  2. compute_pick_order()    -> map hero_id -> (pick_number 1-10, draft phase)
                                 using match['picks_bans'].
  3. compute_lane_outcomes() -> compare each lane's gold+xp at the 10 minute
                                 mark against the opposing lane to get a
                                 won/lost/stomp signal per lane.
  4. analyze_team()          -> combine all of the above into a per-role
                                 weighted "badness" score, then softmax it
                                 into guilt probabilities.

Everything here is a heuristic. OpenDota doesn't give us a ground-truth
"position 1-5" or "lane result", so both are inferred from the same raw
player fields the bot already fetches (no extra API calls).
"""

import math
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

ROLE_CARRY = "carry"                # position 1 - safelane carry
ROLE_MID = "mid"                    # position 2 - mid
ROLE_OFFLANE = "offlane"            # position 3 - offlane (tank/secondary carry)
ROLE_SUPPORT_HARD = "support_hard"  # position 4 - hard support (roams the offlane side)
ROLE_SUPPORT_SOFT = "support_soft"  # position 5 - soft support (babysits the safelane)

ROLE_LABELS = {
    ROLE_CARRY: "Safelane Carry (pos 1)",
    ROLE_MID: "Mid (pos 2)",
    ROLE_OFFLANE: "Offlane (pos 3)",
    ROLE_SUPPORT_HARD: "Hard Support (pos 4)",
    ROLE_SUPPORT_SOFT: "Soft Support (pos 5)",
}

CORE_ROLES = (ROLE_CARRY, ROLE_MID, ROLE_OFFLANE)
SUPPORT_ROLES = (ROLE_SUPPORT_HARD, ROLE_SUPPORT_SOFT)

# OpenDota lane_role values: 1 = Safe Lane, 2 = Mid Lane, 3 = Off Lane.
# (4/"jungle" shows up occasionally but is not part of the modern meta and
# is treated as "unknown" here, same as a missing value.)
LANE_ROLE_SAFE = 1
LANE_ROLE_MID = 2
LANE_ROLE_OFF = 3


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def economy_score(p: dict) -> float:
    """Rough 'how core is this player' ranking signal for a single game."""
    return (p.get("gold_per_min", 0) or 0) * 0.6 + (p.get("xp_per_min", 0) or 0) * 0.4


def player_nickname(p: dict) -> str:
    name = p.get("personaname")
    if name:
        return name
    acc = p.get("account_id")
    if acc:
        return f"Anonymous#{acc}"
    return "Anonymous (hidden profile)"


def early_death_count(p: dict, cutoff_seconds: int = 600) -> int:
    """Count deaths before `cutoff_seconds` using the life_state timeline,
    if OpenDota gave us that (only present on parsed matches). Returns -1
    if unavailable."""
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


def _time_series_value(arr: Optional[List[Any]], minute: int) -> float:
    """gold_t / xp_t are per-minute arrays. Grab the value at `minute`,
    clamped to the array length (covers games that ended before `minute`)."""
    if not arr:
        return 0.0
    idx = min(len(arr) - 1, max(0, minute))
    val = arr[idx]
    return float(val or 0)


def lane_group(role: str) -> str:
    """Which physical lane a role is associated with, for lane-outcome
    comparisons. Carry <-> soft support share the safelane; offlane <->
    hard support share the offlane; mid is mid."""
    if role in (ROLE_CARRY, ROLE_SUPPORT_SOFT):
        return "safelane"
    if role in (ROLE_OFFLANE, ROLE_SUPPORT_HARD):
        return "offlane"
    return "mid"


def describe_lane_outcome(score: float) -> str:
    if score >= 0.6:
        return "won big (lane stomp)"
    if score >= 0.2:
        return "won the lane"
    if score > -0.2:
        return "even lane"
    if score > -0.6:
        return "lost the lane"
    return "lost big (got stomped)"


# --------------------------------------------------------------------------
# 1. Role assignment
# --------------------------------------------------------------------------

def assign_roles(team: List[dict]) -> Dict[int, str]:
    """Returns {player_slot: role}. Best-effort heuristic:

    - Bucket players by OpenDota's lane_role (safe/mid/off).
    - Within a lane bucket that has 2 players (e.g. safelane carry + pos 5,
      or offlane + roaming pos 4), the higher-economy one is the core,
      the other is the support.
    - Anyone with a missing/unrecognized lane_role (turbo mode, disconnects,
      jungle, etc.) is thrown into a leftover pool and slotted into
      whichever role is still missing, ranked by economy.
    """
    buckets: Dict[Any, List[dict]] = {LANE_ROLE_SAFE: [], LANE_ROLE_MID: [], LANE_ROLE_OFF: [], "unknown": []}
    for p in team:
        lr = p.get("lane_role")
        if lr in (LANE_ROLE_SAFE, LANE_ROLE_MID, LANE_ROLE_OFF):
            buckets[lr].append(p)
        else:
            buckets["unknown"].append(p)

    mid_sorted = sorted(buckets[LANE_ROLE_MID], key=economy_score, reverse=True)
    mid_player = mid_sorted[0] if mid_sorted else None
    leftover = list(buckets["unknown"]) + mid_sorted[1:]

    safe_sorted = sorted(buckets[LANE_ROLE_SAFE], key=economy_score, reverse=True)
    carry_player = safe_sorted[0] if safe_sorted else None
    safe_leftover = safe_sorted[1:]

    off_sorted = sorted(buckets[LANE_ROLE_OFF], key=economy_score, reverse=True)
    offlane_player = off_sorted[0] if off_sorted else None
    off_leftover = off_sorted[1:]

    support_pool = safe_leftover + off_leftover + leftover

    core = {ROLE_CARRY: carry_player, ROLE_MID: mid_player, ROLE_OFFLANE: offlane_player}
    for role_name in (ROLE_MID, ROLE_CARRY, ROLE_OFFLANE):
        if core[role_name] is None and support_pool:
            support_pool.sort(key=economy_score, reverse=True)
            pick = support_pool.pop(0)
            core[role_name] = pick

    roles: Dict[int, str] = {}
    for role_name, p in core.items():
        if p is not None:
            roles[p["player_slot"]] = role_name

    # Remaining players (should be exactly 2) become the supports. Prefer
    # to keep hard support tied to the offlane side and soft support tied
    # to the safelane side, using their original lane_role as a hint.
    remaining = support_pool
    hard_support = next((p for p in remaining if p.get("lane_role") == LANE_ROLE_OFF), None)
    soft_support = next((p for p in remaining if p.get("lane_role") == LANE_ROLE_SAFE), None)

    for p in remaining:
        if p is hard_support or p is soft_support:
            continue
        if hard_support is None:
            hard_support = p
        elif soft_support is None:
            soft_support = p

    if hard_support is not None:
        roles[hard_support["player_slot"]] = ROLE_SUPPORT_HARD
    if soft_support is not None:
        roles[soft_support["player_slot"]] = ROLE_SUPPORT_SOFT

    # Ultra-fallback so every player always has a role even in weird data.
    for p in team:
        roles.setdefault(p["player_slot"], ROLE_SUPPORT_SOFT)

    return roles


# --------------------------------------------------------------------------
# 2. Draft / pick order
# --------------------------------------------------------------------------

def compute_pick_order(match: dict) -> Dict[int, dict]:
    """Returns {hero_id: {"pick_number": 1-10, "phase": 1|2|3}}.

    Phase 1 (picks 1-4): both teams pick 2 heroes blind/simultaneously.
    Phase 2 (picks 5-8): picked with the first 4 heroes visible.
    Phase 3 (picks 9-10): last picks, everything else is visible.
    """
    picks_bans = match.get("picks_bans") or []
    picks = [x for x in picks_bans if x.get("is_pick")]
    picks.sort(key=lambda x: x.get("order", 0))

    result: Dict[int, dict] = {}
    for i, x in enumerate(picks, start=1):
        phase = 1 if i <= 4 else (2 if i <= 8 else 3)
        result[x.get("hero_id")] = {"pick_number": i, "phase": phase}
    return result


# Small, deliberately low-weight nudge: later picks had more draft
# information available, so a bad game from a late pick is slightly less
# excusable. Phase 1 (blind) gets no penalty at all.
PICK_PHASE_BONUS = {1: 0.0, 2: 0.5, 3: 1.0}


# --------------------------------------------------------------------------
# 3. Lane outcome (win / even / lose / stomp)
# --------------------------------------------------------------------------

def compute_lane_outcomes(
    my_team: List[dict],
    enemy_team: List[dict],
    my_roles: Dict[int, str],
    enemy_roles: Dict[int, str],
    minute: int = 10,
) -> Dict[str, float]:
    """Returns {'safelane': score, 'mid': score, 'offlane': score} from
    *my_team*'s perspective, where score is roughly -1 (stomped) to +1
    (stomping), based on combined gold+xp at `minute` for each lane's
    occupants vs. the players occupying the opposing lane."""

    def total_for(players: List[dict], roles_map: Dict[int, str], group: str) -> float:
        s = 0.0
        for p in players:
            role = roles_map.get(p["player_slot"])
            if role and lane_group(role) == group:
                gold = _time_series_value(p.get("gold_t"), minute)
                xp = _time_series_value(p.get("xp_t"), minute)
                s += gold + xp * 0.5
        return s

    outcomes: Dict[str, float] = {}
    opposite = {"safelane": "offlane", "offlane": "safelane", "mid": "mid"}
    for group, opp in opposite.items():
        own = total_for(my_team, my_roles, group)
        enemy = total_for(enemy_team, enemy_roles, opp)
        denom = own + enemy
        if denom <= 0:
            outcomes[group] = 0.0
            continue
        ratio = own / denom  # 0.5 == even
        outcomes[group] = max(-1.0, min(1.0, (ratio - 0.5) * 4.0))
    return outcomes


# --------------------------------------------------------------------------
# 4. Role-weighted badness scoring
# --------------------------------------------------------------------------
#
# Every role shares a common core (death rate, kill participation, early
# deaths, lane outcome, pick order, buybacks). Cores (carry/mid/offlane)
# additionally get judged on economy share; supports get judged on
# utility (wards, healing, stuns, camps) with economy as a minor factor.
#
# Tuning notes (per the brief):
#   - carry:    net worth dominates, deaths matter a lot, lane outcome minor.
#   - mid:      everything weighted roughly evenly.
#   - offlane:  lane outcome + early-game activity matter most, economy less.
#   - supports: wards/kda always matter; healing is high priority, damage
#               low priority; net worth is a minor factor; hard support
#               leans on lane outcome like the offlane it partners with,
#               soft support leans less on it.

ROLE_WEIGHTS: Dict[str, Dict[str, float]] = {
    ROLE_CARRY: {
        "death_rate": 7.0,
        "kill_participation": 10.0,
        "early_death_weight": 3.0,
        "lane_outcome_weight": 3.0,
        "pick_order_weight": 1.0,
        "networth_share_target": 0.30, "networth_share_weight": 55.0,
        "gpm_share_target": 0.28, "gpm_share_weight": 40.0,
        "dmg_share_target": 0.25, "dmg_share_weight": 20.0,
    },
    ROLE_MID: {
        "death_rate": 8.0,
        "kill_participation": 14.0,
        "early_death_weight": 5.0,
        "lane_outcome_weight": 5.0,
        "pick_order_weight": 1.0,
        "networth_share_target": 0.22, "networth_share_weight": 25.0,
        "gpm_share_target": 0.22, "gpm_share_weight": 25.0,
        "dmg_share_target": 0.22, "dmg_share_weight": 25.0,
    },
    ROLE_OFFLANE: {
        "death_rate": 7.0,
        "kill_participation": 12.0,
        "early_death_weight": 7.0,
        "lane_outcome_weight": 9.0,
        "pick_order_weight": 1.2,
        "networth_share_target": 0.16, "networth_share_weight": 15.0,
        "gpm_share_target": 0.16, "gpm_share_weight": 10.0,
        "dmg_share_target": 0.16, "dmg_share_weight": 8.0,
    },
    ROLE_SUPPORT_HARD: {
        "death_rate": 5.0,
        "kill_participation": 12.0,
        "early_death_weight": 6.0,
        "lane_outcome_weight": 8.0,
        "pick_order_weight": 0.8,
        "networth_share_target": 0.08, "networth_share_weight": 6.0,
        "ward_target_per_10min": 4.0, "ward_weight": 10.0,
        "stun_target": 400, "stun_weight": 2.0,
        "heal_target_per_min": 10.0, "heal_weight": 6.0,
        "dmg_weight": 1.5,
        "camp_target_per_20min": 2.0, "camp_weight": 3.0,
    },
    ROLE_SUPPORT_SOFT: {
        "death_rate": 5.0,
        "kill_participation": 12.0,
        "early_death_weight": 4.0,
        "lane_outcome_weight": 4.0,
        "pick_order_weight": 0.8,
        "networth_share_target": 0.10, "networth_share_weight": 6.0,
        "ward_target_per_10min": 4.0, "ward_weight": 10.0,
        "stun_target": 400, "stun_weight": 2.0,
        "heal_target_per_min": 10.0, "heal_weight": 6.0,
        "dmg_weight": 1.5,
        "camp_target_per_20min": 1.0, "camp_weight": 2.0,
    },
}


def compute_badness(role: str, feats: dict, weights: Dict[str, Dict[str, float]] = ROLE_WEIGHTS) -> float:
    w = weights.get(role, weights[ROLE_CARRY])
    b = 0.0

    # ---- shared across every role ----
    b += feats["death_rate"] * w["death_rate"]
    if (feats["kills"] + feats["assists"]) < feats["deaths"]:
        b += 6.0
    b += (1.0 - feats["kill_participation"]) * w["kill_participation"]
    if feats["early_deaths"] >= 0:
        b += feats["early_deaths"] * w["early_death_weight"]
    b += feats["buyback_count"] * 1.5

    if role in CORE_ROLES:
        b += max(0.0, w["networth_share_target"] - feats["networth_share"]) * w["networth_share_weight"]
        b += max(0.0, w["gpm_share_target"] - feats["gpm_share"]) * w["gpm_share_weight"]
        b += max(0.0, w["dmg_share_target"] - feats["dmg_share"]) * w["dmg_share_weight"]
    else:
        # supports: utility-first, economy as a minor factor
        b += max(0.0, w["networth_share_target"] - feats["networth_share"]) * w["networth_share_weight"]

        target_wards = w["ward_target_per_10min"] * (feats["duration_min"] / 10.0)
        if target_wards > 0:
            b += (max(0.0, target_wards - feats["wards_placed"]) / target_wards) * w["ward_weight"]

        b += max(0.0, (w["stun_target"] - feats["stuns"]) / 100.0) * w["stun_weight"]

        target_heal = w["heal_target_per_min"] * feats["duration_min"]
        if target_heal > 0:
            b += (max(0.0, target_heal - feats["healing"]) / target_heal) * w["heal_weight"]

        # damage is a low-priority signal for supports; only flag very low participation
        b += max(0.0, 0.08 - feats["dmg_share"]) * w["dmg_weight"] * 10.0

        target_camp = w["camp_target_per_20min"] * (feats["duration_min"] / 20.0)
        if target_camp > 0:
            b += (max(0.0, target_camp - feats["camps_stacked"]) / target_camp) * w["camp_weight"]

    # lane outcome: winning your lane REDUCES badness, losing it INCREASES it
    b += (-feats["lane_outcome_score"]) * w["lane_outcome_weight"]

    # later draft picks get slightly less benefit of the doubt
    b += feats["pick_order_bonus"] * w["pick_order_weight"]

    return max(0.0, b)


# --------------------------------------------------------------------------
# Facts (human-readable bullets for the Discord embed)
# --------------------------------------------------------------------------

def build_facts(a: dict) -> List[str]:
    facts = []

    role_label = ROLE_LABELS.get(a["role"], a["role"])
    pick_note = f" — pick #{a['pick_number']}" if a.get("pick_number") else ""
    facts.append(f"Role: {role_label}{pick_note}")

    facts.append(
        f"KDA {a['kills']}/{a['deaths']}/{a['assists']} "
        f"(kill participation {a['kill_participation']*100:.0f}%)"
    )

    if a["is_support"]:
        facts.append(
            f"Wards: {a['obs_placed']} obs / {a['sen_placed']} sen, "
            f"{a['stuns']:.0f}s of stuns, {a['healing']} healing, "
            f"{a['camps_stacked']} camps stacked"
        )
    else:
        facts.append(
            f"GPM {a['gpm']} ({a['gpm_share']*100:.0f}% of team), "
            f"hero dmg share {a['dmg_share']*100:.0f}%, "
            f"net worth share {a['networth_share']*100:.0f}%"
        )

    if a["early_deaths"] >= 0:
        facts.append(f"Died {a['early_deaths']}x before the 10 minute mark")

    facts.append(f"Lane ({a['lane_group']}): {describe_lane_outcome(a['lane_outcome_score'])}")

    if a["buyback_count"]:
        facts.append(f"Used buyback {a['buyback_count']}x")

    return facts


# --------------------------------------------------------------------------
# Entry point used by the bot
# --------------------------------------------------------------------------

def analyze_team(
    match: dict,
    team: List[dict],
    enemy_team: List[dict],
    hero_name_fn: Optional[Any] = None,
) -> List[dict]:
    """Returns a list of dicts, one per teammate, with computed features,
    a role, a 0+ 'badness' score, and a normalized guilt probability.
    Sorted by guilt_probability descending (most guilty first)."""

    duration_min = max(1.0, match.get("duration", 0) / 60.0)

    team_kills = sum(p.get("kills", 0) for p in team) or 1
    team_gpm = sum(p.get("gold_per_min", 0) for p in team) or 1
    team_dmg = sum(p.get("hero_damage", 0) for p in team) or 1
    team_networth = sum(p.get("net_worth", p.get("total_gold", 0)) for p in team) or 1

    my_roles = assign_roles(team)
    enemy_roles = assign_roles(enemy_team)
    lane_outcomes = compute_lane_outcomes(team, enemy_team, my_roles, enemy_roles)
    pick_map = compute_pick_order(match)

    analyzed = []
    for p in team:
        slot = p["player_slot"]
        role = my_roles.get(slot, ROLE_CARRY)

        kills = p.get("kills", 0)
        deaths = p.get("deaths", 0)
        assists = p.get("assists", 0)
        gpm = p.get("gold_per_min", 0)
        hero_damage = p.get("hero_damage", 0)
        net_worth = p.get("net_worth", p.get("total_gold", 0))
        obs = p.get("obs_placed", 0)
        sen = p.get("sen_placed", 0)
        stuns = p.get("stuns", 0) or 0
        healing = p.get("hero_healing", 0) or 0
        camps = p.get("camps_stacked", 0) or 0
        buyback_count = len(p.get("buyback_log", []) or [])
        early_deaths = early_death_count(p)

        kill_participation = safe_div(kills + assists, team_kills)
        gpm_share = safe_div(gpm, team_gpm)
        dmg_share = safe_div(hero_damage, team_dmg)
        networth_share = safe_div(net_worth, team_networth)
        death_rate = deaths / duration_min

        hero_id = p.get("hero_id", 0)
        pick_info = pick_map.get(hero_id)
        pick_number = pick_info["pick_number"] if pick_info else None
        pick_phase = pick_info["phase"] if pick_info else None
        pick_order_bonus = PICK_PHASE_BONUS.get(pick_phase, 0.0)

        group = lane_group(role)
        lane_outcome_score = lane_outcomes.get(group, 0.0)

        feats = dict(
            kills=kills, deaths=deaths, assists=assists,
            kill_participation=kill_participation,
            gpm_share=gpm_share, dmg_share=dmg_share, networth_share=networth_share,
            death_rate=death_rate, early_deaths=early_deaths, buyback_count=buyback_count,
            duration_min=duration_min,
            wards_placed=obs + sen, stuns=stuns, healing=healing, camps_stacked=camps,
            lane_outcome_score=lane_outcome_score, pick_order_bonus=pick_order_bonus,
        )

        badness = compute_badness(role, feats)

        hero_label = hero_name_fn(hero_id) if hero_name_fn else f"Hero {hero_id}"

        analyzed.append({
            "account_id": p.get("account_id"),
            "nickname": player_nickname(p),
            "hero": hero_label,
            "role": role,
            "is_support": role in SUPPORT_ROLES,
            "kills": kills, "deaths": deaths, "assists": assists,
            "gpm": gpm, "xpm": p.get("xp_per_min", 0),
            "hero_damage": hero_damage,
            "net_worth": net_worth,
            "healing": healing,
            "camps_stacked": camps,
            "kill_participation": kill_participation,
            "gpm_share": gpm_share,
            "dmg_share": dmg_share,
            "networth_share": networth_share,
            "obs_placed": obs, "sen_placed": sen,
            "stuns": round(stuns, 1),
            "early_deaths": early_deaths,
            "buyback_count": buyback_count,
            "pick_number": pick_number,
            "pick_phase": pick_phase,
            "lane_group": group,
            "lane_outcome_score": round(lane_outcome_score, 2),
            "badness": badness,
        })

    # Softmax the badness scores into "guilt probabilities" that sum to 1.
    scores = [a["badness"] for a in analyzed]
    m = max(scores) if scores else 0.0
    exps = [math.exp((s - m) * 0.12) for s in scores]  # lower coefficient = more even spread
    total = sum(exps) or 1.0
    for a, e in zip(analyzed, exps):
        a["guilt_probability"] = e / total

    analyzed.sort(key=lambda a: a["guilt_probability"], reverse=True)
    return analyzed

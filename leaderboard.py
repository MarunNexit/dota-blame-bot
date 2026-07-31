def build_players(state):
    players = []

    for p in state["players"].values():

        games = p["games_together"]
        guilty = p["guilty_count"]

        if games == 0:
            continue

        players.append({
            "name": p["nickname"],
            "games": games,
            "guilty": guilty,
            "percent": guilty / games * 100
        })

    return players



def top_ruiners(state, limit=10):

    players = build_players(state)

    return sorted(
        players,
        key=lambda p: (
            p["guilty"],     # total ruined games
            p["percent"]     # tie breaker
        ),
        reverse=True
    )[:limit]



def top_allies(state, limit=10):

    players = build_players(state)

    return sorted(
        players,
        key=lambda p: (
            p["games"],      # most games together
            p["guilty"]      # tie breaker
        ),
        reverse=True
    )[:limit]




def format_leaderboard(ruiners, allies):
    
    lines = []

    lines.append("🏆 **DOTA LEADERBOARDS**")
    lines.append("")


    lines.append("💀 **TOP RUINERS**")

    if ruiners:

        for i, p in enumerate(ruiners, 1):

            lines.append(
                f"`#{i}` **{p['name']}** "
                f"💀 {p['guilty']}/{p['games']} "
                f"({p['percent']:.1f}%)"
            )

    else:
        lines.append("No data")


    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")


    lines.append("")
    lines.append("🤝 **TOP ALLIES**")


    if allies:

        for i, p in enumerate(allies, 1):

            lines.append(
                f"`#{i}` **{p['name']}** "
                f"🎮 {p['games']} games "
                f"💀 {p['guilty']} ruined"
            )

    else:
        lines.append("No data")


    return "\n".join(lines)
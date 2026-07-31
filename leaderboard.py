def build_players(state):

    players = []

    for p in state["players"].values():

        games = p.get("games_together", 0)
        losses = p.get("losses_together", 0)
        guilty = p.get("guilty_count", 0)

        if games == 0:
            continue

        players.append({
            "name": p["nickname"],
            "games": games,
            "losses": losses,
            "guilty": guilty,
            "percent": guilty / losses * 100 if losses else 0
        })

    return players



def top_ruiners(state, limit=10):

    players = build_players(state)

    return sorted(
        players,
        key=lambda p: (
            p["percent"],
            p["guilty"],
            p["losses"]
        ),
        reverse=True
    )[:limit]



def top_allies(state, limit=10):

    players = build_players(state)

    return sorted(
        players,
        key=lambda p: (
            p["games"],
            p["losses"]
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
                f"💀 {p['guilty']} ruined "
                f"❌ {p['losses']} losses "
                f"🎮 {p['games']} games "
                f"({p['percent']:.1f}% blame)"
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
                f"❌ {p['losses']} losses "
                f"💀 {p['guilty']} ruined"
            )

    else:
        lines.append("No data")


    return "\n".join(lines)
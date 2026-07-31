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
            p["guilty"],
            p["percent"]
        ),
        reverse=True
    )[:limit]



def top_allies(state, limit=10):
    players = build_players(state)

    return sorted(
        players,
        key=lambda p: p["games"],
        reverse=True
    )[:limit]
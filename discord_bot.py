import os
import json
import discord
import subprocess

from dotenv import load_dotenv
from discord.ext import commands


load_dotenv()


TOKEN = os.environ["DISCORD_TOKEN"]


intents = discord.Intents.default()
intents.message_content = True


bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


@bot.event
async def on_ready():

    await bot.tree.sync()

    print(
        f"Logged in as {bot.user}"
    )

    print("Slash commands synced")



def update_state():
    try:
        result = subprocess.run(
            ["git", "pull"],
            capture_output=True,
            text=True,
            timeout=30
        )

        print(result.stdout)

        if result.stderr:
            print(result.stderr)

    except Exception as e:
        print(f"Git pull failed: {e}")

@bot.tree.command(
    name="top",
    description="Show top Dota ruiners"
)
async def top(interaction: discord.Interaction):

    update_state()

    with open("state.json") as f:
        state = json.load(f)


    players = []


    for p in state["players"].values():

        games = p["games_together"]

        if games:
            players.append(
                {
                    "name": p["nickname"],
                    "guilty": p["guilty_count"],
                    "games": games
                }
            )


    players.sort(
        key=lambda x: (
            x["guilty"],
            x["guilty"] / x["games"]
        ),
        reverse=True
    )


    msg = "🏆 **TOP RUINERS**\n\n"


    for i, p in enumerate(players[:10], 1):

        percent = (
            p["guilty"] / p["games"] * 100
        )

        msg += (
            f"**{i}. {p['name']}**\n"
            f"💀 {p['guilty']}/{p['games']} "
            f"({percent:.1f}%)\n\n"
        )


    await interaction.response.send_message(msg)



bot.run(TOKEN)
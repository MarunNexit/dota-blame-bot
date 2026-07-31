import os
import json
import subprocess
import discord

from dotenv import load_dotenv
from discord.ext import commands

from leaderboard import top_ruiners, top_allies


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

        print(
            f"Git pull failed: {e}"
        )



@bot.tree.command(
    name="top",
    description="Show Dota leaderboards"
)
async def top(interaction: discord.Interaction):

    update_state()


    with open(
        "state.json",
        encoding="utf-8"
    ) as f:

        state = json.load(f)



    ruiners = top_ruiners(
        state,
        limit=10
    )


    allies = top_allies(
        state,
        limit=10
    )



    msg = "🏆 **TOP RUINERS**\n\n"


    if ruiners:

        for i, p in enumerate(ruiners, 1):

            msg += (
                f"**{i}. {p['name']}**\n"
                f"💀 Ruined: **{p['guilty']}** "
                f"({p['percent']:.1f}% "
                f"of {p['games']} games)\n\n"
            )


    else:

        msg += "No data.\n"



    msg += (
        "\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )


    msg += "🤝 **MOST PLAYED ALLIES**\n\n"



    if allies:

        for i, p in enumerate(allies, 1):

            msg += (
                f"**{i}. {p['name']}**\n"
                f"🎮 Games together: **{p['games']}**\n"
                f"💀 Ruined: {p['guilty']} "
                f"({p['percent']:.1f}%)\n\n"
            )


    else:

        msg += "No data."



    await interaction.response.send_message(
        msg
    )



bot.run(TOKEN)
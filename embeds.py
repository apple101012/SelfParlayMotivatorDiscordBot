# embeds.py
import discord
from datetime import datetime
from storage import TZ, format_timeleft
from models import Parlay

def make_embed(parlay: Parlay, author: discord.User) -> discord.Embed:
    deadline = datetime.fromisoformat(parlay.deadline_ts)
    color = 0x2ecc71 if parlay.status == "ACTIVE" else (0xe74c3c if parlay.status == "LOST" else 0x3498db)
    e = discord.Embed(
        title=f"Parlay #{parlay.id.split('-')[0]}",
        color=color,
        timestamp=datetime.now(TZ)
    )
    e.set_author(name=f"{author.display_name}", icon_url=author.display_avatar.url)
    e.add_field(name="Stake", value=f"{parlay.stake} pts", inline=True)
    e.add_field(name="Legs", value=f"{parlay.legs_count}", inline=True)
    e.add_field(name="Mult", value=f"{parlay.multiplier:.2f}×", inline=True)

    lines = []
    for i, leg in enumerate(parlay.legs, start=1):
        mark = "⬜"
        if leg.status == "WIN":
            mark = "✅"
        elif leg.status == "FAIL":
            mark = "❌"
        lines.append(f"{mark} {i}. {leg.text}")
    e.add_field(name="Items", value="\n".join(lines) or "—", inline=False)

    e.add_field(name="Deadline", value=deadline.strftime("%b %d, %Y %I:%M %p ET"), inline=True)
    e.add_field(name="Time Left", value=format_timeleft(deadline), inline=True)
    e.add_field(name="Status", value=parlay.status.title(), inline=True)

    e.set_footer(text="All legs must be ✅ by the deadline to win")
    return e

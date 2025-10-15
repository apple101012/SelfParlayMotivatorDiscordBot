# discordbot.py
import os, logging, asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from discord.utils import setup_logging
from dotenv import load_dotenv, find_dotenv

from models import Parlay
from storage import (
    DB, DB_LOCK, save_data, load_data, ensure_user, parse_deadline, parse_legs,
    new_parlay_for, daily_weekly_ok, next_daily_reset_info, TZ,
    COOLDOWN_AFTER_LOSS_MIN, DAILY_STAKE_CAP, WEEKLY_STAKE_CAP, MAX_LEGS,
    resolve_parlay
)
from embeds import make_embed
from views import ManageParlayView

# ----- ENV / LOGGING -----
load_dotenv(find_dotenv(usecwd=True), override=True)
setup_logging(level=logging.INFO)
logging.getLogger("discord").setLevel(logging.INFO)

# ----- BOT -----
intents = discord.Intents.default()
intents.message_content = False

class SelfParlayBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        deadline_watcher.start()

bot = SelfParlayBot()

@bot.event
async def on_ready():
    app = await bot.application_info()
    print(f"[OK] Logged in as: {bot.user} (id={bot.user.id}) • App: {app.name}")

# ----- DM guard -----
async def ensure_dm(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return True
    try:
        await interaction.response.send_message("Use me in **DMs**. I DM’d you instructions.", ephemeral=True)
    except discord.InteractionResponded:
        pass
    try:
        await interaction.user.send("Hi! I work only in DMs. Try `/rules` here first, then `/bet`.")
    except Exception:
        pass
    return False

# ----- COMMANDS -----
@bot.tree.command(name="rules", description="How this DM self-parlay bot works.")
async def rules(interaction: discord.Interaction):
    if not await ensure_dm(interaction): return
    e = discord.Embed(title="Self-Parlay Rules (DM Bot)", color=0x43B581)
    e.add_field(name="How it works", value="Create a parlay (1–5 tasks), set a deadline, mark legs ✅/❌. If **all** are ✅ at the deadline, you win.", inline=False)
    e.add_field(name="Example", value="`/bet 50 (go gym) (study 40 mins) (finish 310 hw) 10/14/2025 11:59 PM`", inline=False)
    e.add_field(name="Caps/Cooldown", value=f"Daily cap {DAILY_STAKE_CAP}, weekly cap {WEEKLY_STAKE_CAP}. After a loss: {COOLDOWN_AFTER_LOSS_MIN} min cooldown.", inline=False)
    e.set_footer(text="All times ET • You vs. you.")
    await interaction.response.send_message(embed=e)

@bot.tree.command(name="faq", description="Quick FAQ / tips.")
async def faq(interaction: discord.Interaction):
    if not await ensure_dm(interaction): return
    e = discord.Embed(title="FAQ", color=0x7289DA)
    e.add_field(name="Where do I use commands?", value="**DMs only.**", inline=False)
    e.add_field(name="Editing", value="No edits. Use the **Modify a leg** button to mark ✅/❌.", inline=False)
    e.add_field(name="Resend my parlays", value="Use `/parlays` and I’ll resend your active parlay cards here.", inline=False)
    await interaction.response.send_message(embed=e)

@bot.tree.command(name="bet", description="Create a parlay in DM: /bet <stake> <legs> <deadline>")
@app_commands.describe(
    stake="Points to stake (integer)",
    legs_text="Legs in parentheses, e.g. (go gym) (study 40 mins) (finish 310 hw)",
    deadline="Deadline in ET, e.g. 10/14/2025 11:59 PM"
)
async def bet(interaction: discord.Interaction, stake: app_commands.Range[int, 1, 100000], legs_text: str, deadline: str):
    if not await ensure_dm(interaction): return
    uid = str(interaction.user.id)
    now = datetime.now(TZ)

    try:
        legs_list = parse_legs(legs_text)
        if not legs_list:
            return await interaction.response.send_message("Include at least one leg in `( ... )`.")
        if len(legs_list) > MAX_LEGS:
            return await interaction.response.send_message(f"Max {MAX_LEGS} legs allowed.")
        deadline_dt = parse_deadline(deadline)
        if deadline_dt <= now:
            return await interaction.response.send_message("Deadline must be in the future.")
    except ValueError as e:
        return await interaction.response.send_message(f"Error: {e}")

    async with DB_LOCK:
        ensure_user(uid)
        user = DB["users"][uid]
        if user["last_loss_ts"]:
            last_loss = datetime.fromisoformat(user["last_loss_ts"])
            if now - last_loss < timedelta(minutes=COOLDOWN_AFTER_LOSS_MIN):
                wait_m = int((timedelta(minutes=COOLDOWN_AFTER_LOSS_MIN) - (now - last_loss)).total_seconds() // 60) + 1
                return await interaction.response.send_message(f"Cooldown after loss. Try again in ~{wait_m} minutes.")
        ok, msg = daily_weekly_ok(user, stake, now)
        if not ok:
            return await interaction.response.send_message(msg)

        p = new_parlay_for(uid, stake, legs_list, deadline_dt)
        DB["parlays"][p.id] = p.to_dict()
        user["daily_spent"] += stake
        user["weekly_spent"] += stake
        save_data()

    embed = make_embed(p, interaction.user)
    view = ManageParlayView(p, interaction.user.id, bot)
    await interaction.response.send_message(embed=embed, view=view)

    # store the message/channel
    msg = await interaction.original_response()
    async with DB_LOCK:
        p.message_id = msg.id
        p.channel_id = msg.channel.id
        DB["parlays"][p.id] = p.to_dict()
        save_data()

@bot.tree.command(name="parlays", description="Re-send your active parlays here (fresh cards).")
async def parlays(interaction: discord.Interaction):
    if not await ensure_dm(interaction): return
    uid = str(interaction.user.id)
    async with DB_LOCK:
        ps = [Parlay.from_dict(p) for p in DB["parlays"].values() if p["user_id"] == uid and p["status"] == "ACTIVE"]

    if not ps:
        return await interaction.response.send_message("You have no active parlays.")

    ps.sort(key=lambda p: p.deadline_ts)
    await interaction.response.send_message(f"Re-sending **{len(ps)}** active parlay(s):")
    for p in ps:
        embed = make_embed(p, interaction.user)
        view = ManageParlayView(p, interaction.user.id, bot)
        msg = await interaction.channel.send(embed=embed, view=view)
        async with DB_LOCK:
            p.message_id = msg.id
            p.channel_id = msg.channel.id
            DB["parlays"][p.id] = p.to_dict()
            save_data()

@bot.tree.command(name="bank", description="Show your balance, caps, streak, last 5 results + next daily reset.")
async def bank(interaction: discord.Interaction):
    if not await ensure_dm(interaction): return
    uid = str(interaction.user.id)
    async with DB_LOCK:
        ensure_user(uid)
        user = DB["users"][uid]
        recent = [l for l in reversed(DB["ledger"]) if l["user_id"] == uid][:5]

    next_reset_dt, in_text = next_daily_reset_info()
    reset_at = next_reset_dt.strftime("%b %d, %Y %I:%M %p ET")

    e = discord.Embed(title="Your Bank", color=0x5865F2)
    e.add_field(name="Balance", value=f"**{user['balance']} pts**", inline=True)
    e.add_field(name="Win Streak", value=str(user.get("streak_days", 0)), inline=True)
    e.add_field(name="Daily Stake Used", value=f"{user['daily_spent']}/{DAILY_STAKE_CAP}", inline=True)
    e.add_field(name="Weekly Stake Used", value=f"{user['weekly_spent']}/{WEEKLY_STAKE_CAP}", inline=True)
    e.add_field(name="Daily cap resets", value=f"{reset_at} ({in_text})", inline=False)

    if recent:
        pretty = []
        from storage import TZ as _TZ
        for r in recent:
            sign = "🟢" if r["delta"] > 0 else "🔴"
            amt = f"+{r['delta']}" if r["delta"] > 0 else f"{r['delta']}"
            when = datetime.fromisoformat(r['ts']).astimezone(_TZ).strftime('%m/%d %I:%M %p')
            pretty.append(f"{sign} {amt} • #{r['parlay_id'].split('-')[0]} • {r['note']} • {when}")
        e.add_field(name="Recent", value="\n".join(pretty), inline=False)
    else:
        e.add_field(name="Recent", value="No results yet.", inline=False)

    e.set_footer(text="All times ET")
    await interaction.response.send_message(embed=e)

# ----- Background: auto-resolve -----
@tasks.loop(seconds=60)
async def deadline_watcher():
    async with DB_LOCK:
        active = [Parlay.from_dict(p) for p in DB["parlays"].values() if p["status"] == "ACTIVE"]
    if not active:
        return
    for p in active:
        from datetime import datetime as _dt
        deadline = _dt.fromisoformat(p.deadline_ts)
        if _dt.now(TZ) >= deadline:
            # best effort resolve + update
            try:
                user = bot.get_user(int(p.user_id)) or await bot.fetch_user(int(p.user_id))
            except Exception:
                user = None
            if not user:
                continue

            async with DB_LOCK:
                p_latest = Parlay.from_dict(DB["parlays"][p.id])

            await resolve_parlay(p_latest, user, bot)

            async with DB_LOCK:
                DB["parlays"][p_latest.id] = p_latest.to_dict()
                save_data()

@deadline_watcher.before_loop
async def before_deadline_watcher():
    await bot.wait_until_ready()

# ----- Run -----
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    print(f"[ENV] DISCORD_TOKEN present? {bool(token)} (len={len(token) if token else 0})")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in .env")
    bot.run(token)

# self_parlay_bot.py
# Minimal "Self-Parlay" Discord bot — JSON storage, no SQL.
# Python 3.10+ recommended (uses zoneinfo). Requires: pip install discord.py

import os
import json
import asyncio
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

# ========= CONFIG =========
DATA_FILE = "selfparlay_data.json"
TZ = ZoneInfo("America/New_York")

# Economy / rules
START_BALANCE = 1000
DAILY_STAKE_CAP = 150
WEEKLY_STAKE_CAP = 800
COOLDOWN_AFTER_LOSS_MIN = 60
MAX_LEGS = 5

# Fixed multipliers by leg count
PARLAY_MULT = {1: 1.20, 2: 1.50, 3: 1.80, 4: 2.00, 5: 2.20}

# ========= STORAGE =========

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}, "parlays": {}, "ledger": []}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, indent=2, ensure_ascii=False)

DB = load_data()
DB_LOCK = asyncio.Lock()

def today_str(dt: datetime | None = None):
    dt = dt or datetime.now(TZ)
    return dt.date().isoformat()

def iso_week_key(dt: datetime | None = None):
    dt = dt or datetime.now(TZ)
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"

def ensure_user(uid: str):
    if uid not in DB["users"]:
        DB["users"][uid] = {
            "balance": START_BALANCE,
            "daily_spent": 0,
            "daily_date": today_str(),
            "weekly_spent": 0,
            "weekly_key": iso_week_key(),
            "last_loss_ts": None,
            "streak_days": 0,
            "last_win_date": None,
        }

# ========= MODELS =========

@dataclass
class Leg:
    text: str
    status: str = "OPEN"  # OPEN | WIN | FAIL

@dataclass
class Parlay:
    id: str
    user_id: str
    stake: int
    legs: list[Leg]
    legs_count: int
    multiplier: float
    created_ts: str  # ISO
    deadline_ts: str  # ISO (ET)
    status: str = "ACTIVE"  # ACTIVE | WON | LOST
    message_id: int | None = None
    channel_id: int | None = None
    resolved_ts: str | None = None

    def to_dict(self):
        d = asdict(self)
        d["legs"] = [asdict(l) for l in self.legs]
        return d

    @staticmethod
    def from_dict(d: dict):
        legs = [Leg(**l) for l in d["legs"]]
        return Parlay(
            id=d["id"],
            user_id=d["user_id"],
            stake=d["stake"],
            legs=legs,
            legs_count=d["legs_count"],
            multiplier=d["multiplier"],
            created_ts=d["created_ts"],
            deadline_ts=d["deadline_ts"],
            status=d.get("status", "ACTIVE"),
            message_id=d.get("message_id"),
            channel_id=d.get("channel_id"),
            resolved_ts=d.get("resolved_ts"),
        )

# ========= UTIL =========

def parse_deadline(deadline_str: str) -> datetime:
    """
    Accepts format like "10/14/2025 11:59 PM" in America/New_York.
    """
    # strict, simple format — avoids external libs
    deadline_str = deadline_str.strip()
    try:
        dt_naive = datetime.strptime(deadline_str, "%m/%d/%Y %I:%M %p")
        return dt_naive.replace(tzinfo=TZ)
    except ValueError:
        raise ValueError("Use format MM/DD/YYYY HH:MM AM/PM, e.g., 10/14/2025 11:59 PM")

def parse_legs(legs_str: str) -> list[str]:
    """
    Extracts ( ... ) groups from a string: "(go to gym) (study 40 mins)"
    """
    legs = []
    buf, in_paren = [], False
    for ch in legs_str:
        if ch == "(":
            if in_paren:  # nested not allowed
                raise ValueError("Nested parentheses not allowed.")
            in_paren = True
            buf = []
        elif ch == ")":
            if not in_paren:
                raise ValueError("Unbalanced parentheses.")
            text = "".join(buf).strip()
            if text:
                legs.append(text)
            in_paren = False
        else:
            if in_paren:
                buf.append(ch)
    if in_paren:
        raise ValueError("Unbalanced parentheses — missing ')'.")
    return legs

def format_timeleft(deadline: datetime) -> str:
    now = datetime.now(TZ)
    delta = deadline - now
    if delta.total_seconds() <= 0:
        return "expired"
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m left"

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
    e.add_field(name="Multiplier", value=f"{parlay.multiplier:.2f}×", inline=True)

    lines = []
    for i, leg in enumerate(parlay.legs, start=1):
        mark = "⬜"
        if leg.status == "WIN":
            mark = "✅"
        elif leg.status == "FAIL":
            mark = "❌"
        lines.append(f"{mark} **{i}.** {leg.text}")
    e.add_field(name="Items", value="\n".join(lines) or "—", inline=False)

    e.add_field(name="Deadline (ET)", value=deadline.strftime("%b %d, %Y %I:%M %p"), inline=True)
    e.add_field(name="Time Left", value=format_timeleft(deadline), inline=True)
    e.add_field(name="Status", value=parlay.status.title(), inline=True)

    e.set_footer(text="Multipliers: 1→1.20, 2→1.50, 3→1.80, 4→2.00, 5→2.20 • All legs must be ✅ to win")
    return e

def daily_weekly_ok(user: dict, stake: int, now: datetime) -> tuple[bool, str]:
    # Reset daily
    if user["daily_date"] != now.date().isoformat():
        user["daily_date"] = now.date().isoformat()
        user["daily_spent"] = 0
    # Reset weekly
    wk = iso_week_key(now)
    if user["weekly_key"] != wk:
        user["weekly_key"] = wk
        user["weekly_spent"] = 0
    if user["daily_spent"] + stake > DAILY_STAKE_CAP:
        return False, f"Daily stake cap {DAILY_STAKE_CAP} pts reached."
    if user["weekly_spent"] + stake > WEEKLY_STAKE_CAP:
        return False, f"Weekly stake cap {WEEKLY_STAKE_CAP} pts reached."
    return True, ""

def add_ledger(user_id: str, delta: int, parlay_id: str, note: str):
    DB["ledger"].append({
        "user_id": user_id,
        "delta": delta,
        "parlay_id": parlay_id,
        "note": note,
        "ts": datetime.now(TZ).isoformat()
    })

async def resolve_parlay(parlay: Parlay, author: discord.User, guild: discord.Guild | None = None):
    if parlay.status != "ACTIVE":
        return  # already done
    user = DB["users"][parlay.user_id]
    # Determine outcome
    all_done = all(l.status == "WIN" for l in parlay.legs)
    any_fail = any(l.status == "FAIL" for l in parlay.legs)
    deadline = datetime.fromisoformat(parlay.deadline_ts)

    if not all_done or any_fail or datetime.now(TZ) > deadline:
        # LOSS
        user["balance"] -= parlay.stake
        user["last_loss_ts"] = datetime.now(TZ).isoformat()
        user["streak_days"] = 0  # reset streak
        outcome = f"LOSS −{parlay.stake} pts"
        add_ledger(parlay.user_id, -parlay.stake, parlay.id, "Parlay loss")
        parlay.status = "LOST"
    else:
        payout = round(parlay.stake * parlay.multiplier)
        user["balance"] += payout
        today = today_str()
        if user["last_win_date"] != today:
            user["streak_days"] = (user.get("streak_days", 0) or 0) + 1
            user["last_win_date"] = today
        outcome = f"WIN +{payout} pts"
        add_ledger(parlay.user_id, payout, parlay.id, "Parlay win")
        parlay.status = "WON"

    parlay.resolved_ts = datetime.now(TZ).isoformat()

    # Post/update the embed & a ledger note
    try:
        if parlay.channel_id and parlay.message_id and guild:
            ch = guild.get_channel(parlay.channel_id) or await guild.fetch_channel(parlay.channel_id)
            msg = await ch.fetch_message(parlay.message_id)
            await msg.edit(embed=make_embed(parlay, author), view=None)
            await ch.send(f"**Parlay #{parlay.id.split('-')[0]}** → {outcome} • New balance: **{user['balance']} pts**")
    except Exception:
        pass

# ========= DISCORD BOT =========

intents = discord.Intents.default()
intents.message_content = False

class SelfParlayBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Sync commands (optionally to a single guild for fast dev)
        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        deadline_watcher.start()

bot = SelfParlayBot()

# ========= VIEWS (Buttons) =========

class ManageParlayView(discord.ui.View):
    def __init__(self, parlay: Parlay, author_id: int, timeout: float | None = None):
        super().__init__(timeout=timeout)
        self.parlay_id = parlay.id
        self.author_id = author_id

        # Add leg selection dropdowns only if ACTIVE
        if parlay.status == "ACTIVE":
            self.add_item(CompleteLegSelect(parlay))
            self.add_item(FailLegSelect(parlay))
        # Resolve button: enabled only if all legs WIN
        all_done = all(l.status == "WIN" for l in parlay.legs)
        self.add_item(ResolveNowButton(enabled=all_done))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the bet creator can manage this parlay.", ephemeral=True)
            return False
        return True

class CompleteLegSelect(discord.ui.Select):
    def __init__(self, parlay: Parlay):
        options = []
        for idx, leg in enumerate(parlay.legs, start=1):
            if leg.status == "OPEN":
                options.append(discord.SelectOption(label=f"✅ Complete leg {idx}", description=leg.text, value=str(idx)))
        super().__init__(placeholder="Mark a leg as COMPLETE", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        async with DB_LOCK:
            parlay = Parlay.from_dict(DB["parlays"][self.view.parlay_id])
            if parlay.status != "ACTIVE":
                return await interaction.response.send_message("Parlay already resolved.", ephemeral=True)
            idx = int(self.values[0]) - 1
            if parlay.legs[idx].status != "OPEN":
                return await interaction.response.send_message("That leg is not open.", ephemeral=True)
            parlay.legs[idx].status = "WIN"
            DB["parlays"][parlay.id] = parlay.to_dict()
            save_data()

        embed = make_embed(parlay, interaction.user)
        # Rebuild view with updated state
        view = ManageParlayView(parlay, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)

class FailLegSelect(discord.ui.Select):
    def __init__(self, parlay: Parlay):
        options = []
        for idx, leg in enumerate(parlay.legs, start=1):
            if leg.status == "OPEN":
                options.append(discord.SelectOption(label=f"❌ Fail leg {idx}", description=leg.text, value=str(idx)))
        super().__init__(placeholder="Mark a leg as FAIL", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        async with DB_LOCK:
            parlay = Parlay.from_dict(DB["parlays"][self.view.parlay_id])
            if parlay.status != "ACTIVE":
                return await interaction.response.send_message("Parlay already resolved.", ephemeral=True)
            idx = int(self.values[0]) - 1
            if parlay.legs[idx].status != "OPEN":
                return await interaction.response.send_message("That leg is not open.", ephemeral=True)
            parlay.legs[idx].status = "FAIL"
            DB["parlays"][parlay.id] = parlay.to_dict()
            save_data()

        embed = make_embed(parlay, interaction.user)
        view = ManageParlayView(parlay, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)

class ResolveNowButton(discord.ui.Button):
    def __init__(self, enabled: bool):
        super().__init__(label="Resolve Now", style=discord.ButtonStyle.primary, disabled=not enabled, row=2)

    async def callback(self, interaction: discord.Interaction):
        async with DB_LOCK:
            parlay = Parlay.from_dict(DB["parlays"][self.view.parlay_id])
            if parlay.status != "ACTIVE":
                return await interaction.response.send_message("Parlay already resolved.", ephemeral=True)
            # Must be all WIN to resolve early
            if not all(l.status == "WIN" for l in parlay.legs):
                return await interaction.response.send_message("All legs must be ✅ to resolve early.", ephemeral=True)
            author = interaction.user
            guild = interaction.guild
            await resolve_parlay(parlay, author, guild)
            DB["parlays"][parlay.id] = parlay.to_dict()
            save_data()

        embed = make_embed(parlay, interaction.user)
        await interaction.response.edit_message(embed=embed, view=None)

# ========= COMMANDS =========

@bot.tree.command(name="bet", description="Create a parlay: /bet <stake> <legs> <deadline>")
@app_commands.describe(
    stake="Points to stake (integer)",
    legs_text="Legs in parentheses, e.g. (go gym) (study 40 mins) (finish 310 hw)",
    deadline="Deadline in ET, e.g. 10/14/2025 11:59 PM"
)
async def bet(interaction: discord.Interaction, stake: app_commands.Range[int, 1, 100000], legs_text: str, deadline: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    uid = str(interaction.user.id)
    now = datetime.now(TZ)

    try:
        legs_list = parse_legs(legs_text)
        if not legs_list:
            return await interaction.followup.send("Please include at least one leg in `( ... )`.", ephemeral=True)
        if len(legs_list) > MAX_LEGS:
            return await interaction.followup.send(f"Max {MAX_LEGS} legs allowed.", ephemeral=True)

        deadline_dt = parse_deadline(deadline)
        if deadline_dt <= now:
            return await interaction.followup.send("Deadline must be in the future.", ephemeral=True)
    except ValueError as e:
        return await interaction.followup.send(f"Error: {e}", ephemeral=True)

    async with DB_LOCK:
        ensure_user(uid)
        user = DB["users"][uid]

        # Cooldown after loss
        if user["last_loss_ts"]:
            last_loss = datetime.fromisoformat(user["last_loss_ts"])
            if now - last_loss < timedelta(minutes=COOLDOWN_AFTER_LOSS_MIN):
                wait_m = int((timedelta(minutes=COOLDOWN_AFTER_LOSS_MIN) - (now - last_loss)).total_seconds() // 60) + 1
                return await interaction.followup.send(f"Cooldown after loss. Try again in ~{wait_m} minutes.", ephemeral=True)

        ok, msg = daily_weekly_ok(user, stake, now)
        if not ok:
            return await interaction.followup.send(msg, ephemeral=True)

        # Create parlay
        legs = [Leg(text=t) for t in legs_list]
        legs_count = len(legs)
        multiplier = PARLAY_MULT.get(legs_count, PARLAY_MULT[MAX_LEGS])
        pid = str(uuid.uuid4())
        parlay = Parlay(
            id=pid,
            user_id=uid,
            stake=stake,
            legs=legs,
            legs_count=legs_count,
            multiplier=multiplier,
            created_ts=now.isoformat(),
            deadline_ts=deadline_dt.isoformat(),
        )
        DB["parlays"][pid] = parlay.to_dict()

        # consume caps immediately (stake reserved)
        # (we count against caps when creating; balance only changes on resolve)
        # reset daily/weekly counters if needed (handled in daily_weekly_ok)
        user["daily_spent"] += stake
        user["weekly_spent"] += stake

        save_data()

    # Post the parlay embed in current channel
    embed = make_embed(parlay, interaction.user)
    view = ManageParlayView(parlay, interaction.user.id)
    ch = interaction.channel
    msg = await ch.send(embed=embed, view=view)

    # Store message/channel for jump link & edits
    async with DB_LOCK:
        parlay.message_id = msg.id
        parlay.channel_id = msg.channel.id
        DB["parlays"][parlay.id] = parlay.to_dict()
        save_data()

    await interaction.followup.send(
        f"Parlay created: **{parlay.legs_count} legs @ {parlay.multiplier:.2f}×**. "
        f"[Jump to message]({msg.jump_url})",
        ephemeral=True
    )

@bot.tree.command(name="parlays", description="List your active parlays and jump to them.")
async def parlays(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    async with DB_LOCK:
        user_parlays = [Parlay.from_dict(p) for p in DB["parlays"].values() if p["user_id"] == uid and p["status"] == "ACTIVE"]

    if not user_parlays:
        return await interaction.response.send_message("You have no active parlays.", ephemeral=True)

    # Sort by deadline soonest
    user_parlays.sort(key=lambda p: p.deadline_ts)
    lines = []
    for p in user_parlays[:10]:
        jump = f"https://discord.com/channels/{interaction.guild_id}/{p.channel_id}/{p.message_id}" if p.channel_id and p.message_id else "(no link)"
        deadline = datetime.fromisoformat(p.deadline_ts).strftime("%b %d, %Y %I:%M %p")
        lines.append(f"• **#{p.id.split('-')[0]}** — {p.legs_count} legs @ {p.multiplier:.2f}× — Stake {p.stake} — Deadline {deadline} — [open]({jump})")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="bank", description="Show your balance, streak, and last 5 results.")
async def bank(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    async with DB_LOCK:
        ensure_user(uid)
        user = DB["users"][uid]
        # recent ledger
        recent = [l for l in reversed(DB["ledger"]) if l["user_id"] == uid][:5]

    e = discord.Embed(title="Your Bank", color=0x5865F2, timestamp=datetime.now(TZ))
    e.add_field(name="Balance", value=f"**{user['balance']} pts**", inline=True)
    e.add_field(name="Win Streak (days)", value=str(user.get("streak_days", 0)), inline=True)
    e.add_field(name="Daily Stake Used", value=f"{user['daily_spent']}/{DAILY_STAKE_CAP}", inline=True)
    e.add_field(name="Weekly Stake Used", value=f"{user['weekly_spent']}/{WEEKLY_STAKE_CAP}", inline=True)

    if recent:
        pretty = []
        for r in recent:
            sign = "🟢" if r["delta"] > 0 else "🔴"
            amt = f"+{r['delta']}" if r["delta"] > 0 else f"{r['delta']}"
            pretty.append(f"{sign} {amt} • #{r['parlay_id'].split('-')[0]} • {r['note']} • {datetime.fromisoformat(r['ts']).astimezone(TZ).strftime('%m/%d %I:%M %p')}")
        e.add_field(name="Recent", value="\n".join(pretty), inline=False)
    else:
        e.add_field(name="Recent", value="No results yet.", inline=False)

    e.set_footer(text="All times ET • Multipliers: 1→1.20, 2→1.50, 3→1.80, 4→2.00, 5→2.20")
    await interaction.response.send_message(embed=e, ephemeral=True)

# ========= BACKGROUND DEADLINE WATCHER =========

@tasks.loop(seconds=60)
async def deadline_watcher():
    # check all ACTIVE parlays whose deadline passed → auto-resolve as loss unless all legs WIN
    async with DB_LOCK:
        active = [Parlay.from_dict(p) for p in DB["parlays"].values() if p["status"] == "ACTIVE"]
    if not active:
        return
    for p in active:
        deadline = datetime.fromisoformat(p.deadline_ts)
        if datetime.now(TZ) >= deadline:
            # Need guild context to edit messages; loop through all guilds (single-user bot)
            author_user = None
            guild_for_msg = None
            try:
                # Find the guild that has the channel
                for g in bot.guilds:
                    if p.channel_id and g.get_channel(p.channel_id):
                        guild_for_msg = g
                        break
                if guild_for_msg:
                    author_user = guild_for_msg.get_member(int(p.user_id)) or await guild_for_msg.fetch_member(int(p.user_id))
            except Exception:
                pass

            async with DB_LOCK:
                # Reload latest state to avoid races
                p_latest = Parlay.from_dict(DB["parlays"][p.id])
            if author_user and guild_for_msg:
                await resolve_parlay(p_latest, author_user, guild_for_msg)
            else:
                # Resolve without messaging if we can't find guild/user (fallback)
                user = DB["users"].get(p_latest.user_id)
                if user:
                    all_done = all(l.status == "WIN" for l in p_latest.legs)
                    any_fail = any(l.status == "FAIL" for l in p_latest.legs)
                    if not all_done or any_fail:
                        user["balance"] -= p_latest.stake
                        user["last_loss_ts"] = datetime.now(TZ).isoformat()
                        user["streak_days"] = 0
                        add_ledger(p_latest.user_id, -p_latest.stake, p_latest.id, "Parlay loss (auto)")
                        p_latest.status = "LOST"
                    else:
                        payout = round(p_latest.stake * p_latest.multiplier)
                        user["balance"] += payout
                        today = today_str()
                        if user["last_win_date"] != today:
                            user["streak_days"] = (user.get("streak_days", 0) or 0) + 1
                            user["last_win_date"] = today
                        add_ledger(p_latest.user_id, payout, p_latest.id, "Parlay win (auto)")
                        p_latest.status = "WON"
                    p_latest.resolved_ts = datetime.now(TZ).isoformat()

            async with DB_LOCK:
                DB["parlays"][p_latest.id] = p_latest.to_dict()
                save_data()

@deadline_watcher.before_loop
async def before_deadline_watcher():
    await bot.wait_until_ready()

# ========= RUN =========

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("Set your bot token in the DISCORD_TOKEN environment variable.")
        raise SystemExit(1)
    bot.run(token)

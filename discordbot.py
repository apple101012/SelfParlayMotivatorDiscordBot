# self_parlay_dm_bot.py
# DM-only "Self-Parlay" Discord bot — JSON storage, no SQL.
# Python 3.10+ recommended. Requires: pip install discord.py python-dotenv

import os
import json
import asyncio
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

from dotenv import load_dotenv
load_dotenv()

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
    channel_id: int | None = None  # DM channel id
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
            if in_paren:
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

async def resolve_parlay(parlay: Parlay, author: discord.User):
    if parlay.status != "ACTIVE":
        return
    user = DB["users"][parlay.user_id]
    all_done = all(l.status == "WIN" for l in parlay.legs)
    any_fail = any(l.status == "FAIL" for l in parlay.legs)
    deadline = datetime.fromisoformat(parlay.deadline_ts)

    if not all_done or any_fail or datetime.now(TZ) > deadline:
        user["balance"] -= parlay.stake
        user["last_loss_ts"] = datetime.now(TZ).isoformat()
        user["streak_days"] = 0
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

    # Edit the DM embed + send a short ledger DM
    try:
        if parlay.channel_id and parlay.message_id:
            ch = bot.get_channel(parlay.channel_id) or await bot.fetch_channel(parlay.channel_id)
            msg = await ch.fetch_message(parlay.message_id)
            await msg.edit(embed=make_embed(parlay, author), view=None)
            await ch.send(f"**Parlay #{parlay.id.split('-')[0]}** → {outcome} • New balance: **{user['balance']} pts**")
    except Exception:
        pass

# ========= DISCORD BOT =========

intents = discord.Intents.default()
intents.message_content = False  # slash commands don't need message content

class SelfParlayBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Global sync so commands work in DMs
        await self.tree.sync()
        deadline_watcher.start()

bot = SelfParlayBot()

# ========= DM-ONLY GUARD =========

async def ensure_dm(interaction: discord.Interaction) -> bool:
    """True if in DM; if used in a server, nudge + DM instructions, then return False."""
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

# ========= VIEWS (Buttons) =========

class ManageParlayView(discord.ui.View):
    def __init__(self, parlay: Parlay, author_id: int, timeout: float | None = None):
        super().__init__(timeout=timeout)
        self.parlay_id = parlay.id
        self.author_id = author_id

        if parlay.status == "ACTIVE":
            self.add_item(CompleteLegSelect(parlay))
            self.add_item(FailLegSelect(parlay))
        all_done = all(l.status == "WIN" for l in parlay.legs)
        self.add_item(ResolveNowButton(enabled=all_done))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the bet creator can manage this parlay.", ephemeral=True)
            return False
        # DM only
        if interaction.guild is not None:
            await interaction.response.send_message("I only work in **DMs**. I DM’d you instructions.", ephemeral=True)
            try:
                await interaction.user.send("Open our DM and use `/bet`, `/bank`, `/parlays` here.")
            except Exception:
                pass
            return False
        return True

class CompleteLegSelect(discord.ui.Select):
    def __init__(self, parlay: Parlay):
        options = []
        for idx, leg in enumerate(parlay.legs, start=1):
            if leg.status == "OPEN":
                options.append(discord.SelectOption(label=f"✅ Complete leg {idx}", description=leg.text, value=str(idx)))
        placeholder = "Mark a leg as COMPLETE" if options else "No open legs"
        super().__init__(placeholder=placeholder, min_values=1 if options else 0, max_values=1 if options else 0, options=options, row=0)

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
        view = ManageParlayView(parlay, interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)

class FailLegSelect(discord.ui.Select):
    def __init__(self, parlay: Parlay):
        options = []
        for idx, leg in enumerate(parlay.legs, start=1):
            if leg.status == "OPEN":
                options.append(discord.SelectOption(label=f"❌ Fail leg {idx}", description=leg.text, value=str(idx)))
        placeholder = "Mark a leg as FAIL" if options else "No open legs"
        super().__init__(placeholder=placeholder, min_values=1 if options else 0, max_values=1 if options else 0, options=options, row=1)

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
            if not all(l.status == "WIN" for l in parlay.legs):
                return await interaction.response.send_message("All legs must be ✅ to resolve early.", ephemeral=True)
            author = interaction.user
            await resolve_parlay(parlay, author)
            DB["parlays"][parlay.id] = parlay.to_dict()
            save_data()

        embed = make_embed(parlay, interaction.user)
        await interaction.response.edit_message(embed=embed, view=None)

# ========= COMMANDS =========

@bot.tree.command(name="rules", description="How this DM self-parlay bot works.")
async def rules(interaction: discord.Interaction):
    if not await ensure_dm(interaction):
        return
    e = discord.Embed(title="Self-Parlay Rules (DM Bot)", color=0x43B581, timestamp=datetime.now(TZ))
    e.add_field(name="What is this?", value="A simple, manual **bet-on-yourself** game. Create a parlay (1–5 tasks), set a deadline, mark legs ✅/❌, then resolve.", inline=False)
    e.add_field(name="Create a parlay", value="`/bet 50 (go to gym) (study 40 mins) (finish 310 hw) 10/14/2025 11:59 PM`", inline=False)
    e.add_field(name="See your parlays", value="`/parlays` — lists active ones (jump links).", inline=False)
    e.add_field(name="Your bank", value="`/bank` — balance, streak, recent results.", inline=False)
    e.add_field(name="Win/Loss", value="Win only if **all legs** are ✅ by the deadline. Else it’s a loss.", inline=False)
    e.add_field(name="Payouts", value="Multiplier by leg count: 1→1.20, 2→1.50, 3→1.80, 4→2.00, 5→2.20.", inline=False)
    e.add_field(name="Caps & Cooldown", value=f"Daily cap {DAILY_STAKE_CAP} pts, weekly cap {WEEKLY_STAKE_CAP} pts. After a loss: {COOLDOWN_AFTER_LOSS_MIN} min cooldown.", inline=False)
    e.set_footer(text="All times are ET • Keep it healthy. This is about progress, not gambling.")
    await interaction.response.send_message(embed=e)

@bot.tree.command(name="faq", description="Quick FAQ / tips.")
async def faq(interaction: discord.Interaction):
    if not await ensure_dm(interaction):
        return
    e = discord.Embed(title="FAQ", color=0x7289DA, timestamp=datetime.now(TZ))
    e.add_field(name="Where do I use commands?", value="**DMs only.**", inline=False)
    e.add_field(name="How do I mark progress?", value="Use the buttons on your parlay embed in DM to mark legs ✅ or ❌.", inline=False)
    e.add_field(name="Can I edit a parlay?", value="No edits. Create carefully. You can create a new one.", inline=False)
    e.add_field(name="What happens at deadline?", value="If any leg isn’t ✅, it resolves as a loss automatically.", inline=False)
    e.add_field(name="What if I misclicked?", value="Keep it honest. If it was a genuine mistake, you can create a new parlay tomorrow.", inline=False)
    e.set_footer(text="Use /rules for full details • /bet to start")
    await interaction.response.send_message(embed=e)

@bot.tree.command(name="bet", description="Create a parlay in DM: /bet <stake> <legs> <deadline>")
@app_commands.describe(
    stake="Points to stake (integer)",
    legs_text="Legs in parentheses, e.g. (go gym) (study 40 mins) (finish 310 hw)",
    deadline="Deadline in ET, e.g. 10/14/2025 11:59 PM"
)
async def bet(interaction: discord.Interaction, stake: app_commands.Range[int, 1, 100000], legs_text: str, deadline: str):
    # DM-only
    if not await ensure_dm(interaction):
        return

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

        # cooldown / caps
        if user["last_loss_ts"]:
            last_loss = datetime.fromisoformat(user["last_loss_ts"])
            if now - last_loss < timedelta(minutes=COOLDOWN_AFTER_LOSS_MIN):
                wait_m = int((timedelta(minutes=COOLDOWN_AFTER_LOSS_MIN) - (now - last_loss)).total_seconds() // 60) + 1
                return await interaction.response.send_message(f"Cooldown after loss. Try again in ~{wait_m} minutes.")
        ok, msg = daily_weekly_ok(user, stake, now)
        if not ok:
            return await interaction.response.send_message(msg)

        # create parlay
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
        user["daily_spent"] += stake
        user["weekly_spent"] += stake
        save_data()

    # reply in this DM with the embed + buttons
    embed = make_embed(parlay, interaction.user)
    view = ManageParlayView(parlay, interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view)

    # store the message we just sent to enable later edits
    msg = await interaction.original_response()
    async with DB_LOCK:
        parlay.message_id = msg.id
        parlay.channel_id = msg.channel.id
        DB["parlays"][parlay.id] = parlay.to_dict()
        save_data()

@bot.tree.command(name="parlays", description="List your active parlays (DM).")
async def parlays(interaction: discord.Interaction):
    if not await ensure_dm(interaction):
        return
    uid = str(interaction.user.id)
    async with DB_LOCK:
        user_parlays = [Parlay.from_dict(p) for p in DB["parlays"].values() if p["user_id"] == uid and p["status"] == "ACTIVE"]

    if not user_parlays:
        return await interaction.response.send_message("You have no active parlays.")

    user_parlays.sort(key=lambda p: p.deadline_ts)
    lines = []
    for p in user_parlays[:10]:
        jump = f"https://discord.com/channels/@me/{p.channel_id}/{p.message_id}" if p.channel_id and p.message_id else "(no link)"
        deadline = datetime.fromisoformat(p.deadline_ts).strftime("%b %d, %Y %I:%M %p")
        lines.append(f"• **#{p.id.split('-')[0]}** — {p.legs_count} legs @ {p.multiplier:.2f}× — Stake {p.stake} — Deadline {deadline} — [open]({jump})")

    await interaction.response.send_message("\n".join(lines))

@bot.tree.command(name="bank", description="Show your balance, streak, and last 5 results (DM).")
async def bank(interaction: discord.Interaction):
    if not await ensure_dm(interaction):
        return
    uid = str(interaction.user.id)
    async with DB_LOCK:
        ensure_user(uid)
        user = DB["users"][uid]
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
            when = datetime.fromisoformat(r['ts']).astimezone(TZ).strftime('%m/%d %I:%M %p')
            pretty.append(f"{sign} {amt} • #{r['parlay_id'].split('-')[0]} • {r['note']} • {when}")
        e.add_field(name="Recent", value="\n".join(pretty), inline=False)
    else:
        e.add_field(name="Recent", value="No results yet.", inline=False)

    e.set_footer(text="All times ET • Multipliers: 1→1.20, 2→1.50, 3→1.80, 4→2.00, 5→2.20")
    await interaction.response.send_message(embed=e)

# ========= BACKGROUND DEADLINE WATCHER =========

@tasks.loop(seconds=60)
async def deadline_watcher():
    # Auto-resolve expired ACTIVE parlays in DMs
    async with DB_LOCK:
        active = [Parlay.from_dict(p) for p in DB["parlays"].values() if p["status"] == "ACTIVE"]
    if not active:
        return
    for p in active:
        deadline = datetime.fromisoformat(p.deadline_ts)
        if datetime.now(TZ) >= deadline:
            # Resolve for the user in DMs
            try:
                user = bot.get_user(int(p.user_id)) or await bot.fetch_user(int(p.user_id))
            except Exception:
                user = None
            if not user:
                continue

            async with DB_LOCK:
                p_latest = Parlay.from_dict(DB["parlays"][p.id])

            await resolve_parlay(p_latest, user)

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
        print("Set your bot token in the DISCORD_TOKEN environment variable (use python-dotenv or export it).")
        raise SystemExit(1)
    bot.run(token)

# storage.py
import os, json, asyncio, uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Tuple, List

from models import Parlay, Leg

# ====== CONFIG / ECONOMY ======
DATA_FILE = "selfparlay_data.json"
TZ = ZoneInfo("America/New_York")

START_BALANCE = 1000
DAILY_STAKE_CAP = 150
WEEKLY_STAKE_CAP = 800
COOLDOWN_AFTER_LOSS_MIN = 60
MAX_LEGS = 5

PARLAY_MULT = {1: 1.20, 2: 1.50, 3: 1.80, 4: 2.00, 5: 2.20}

# ====== DB ======
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

# ====== UTIL ======
def today_str(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(TZ)
    return dt.date().isoformat()

def iso_week_key(dt: datetime | None = None) -> str:
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

def parse_deadline(deadline_str: str) -> datetime:
    from datetime import datetime as dt
    s = deadline_str.strip()
    try:
        naive = dt.strptime(s, "%m/%d/%Y %I:%M %p")
        return naive.replace(tzinfo=TZ)
    except ValueError:
        raise ValueError("Use MM/DD/YYYY HH:MM AM/PM, e.g., 10/14/2025 11:59 PM")

def parse_legs(legs_str: str) -> List[str]:
    legs, buf, in_p = [], [], False
    for ch in legs_str:
        if ch == "(":
            if in_p:
                raise ValueError("Nested parentheses not allowed.")
            in_p, buf = True, []
        elif ch == ")":
            if not in_p:
                raise ValueError("Unbalanced parentheses.")
            txt = "".join(buf).strip()
            if txt:
                legs.append(txt)
            in_p = False
        else:
            if in_p:
                buf.append(ch)
    if in_p:
        raise ValueError("Unbalanced parentheses — missing ')'.")
    return legs

def format_timeleft(deadline: datetime) -> str:
    now = datetime.now(TZ)
    delta = deadline - now
    if delta.total_seconds() <= 0:
        return "expired"
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m"

def next_daily_reset_info(now: datetime | None = None) -> Tuple[datetime, str]:
    now = now or datetime.now(TZ)
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    delta = next_midnight - now
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, _ = divmod(rem, 60)
    return next_midnight, f"in {h}h {m}m"

def daily_weekly_ok(user: dict, stake: int, now: datetime) -> Tuple[bool, str]:
    if user["daily_date"] != now.date().isoformat():
        user["daily_date"] = now.date().isoformat()
        user["daily_spent"] = 0
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

async def resolve_parlay(parlay: Parlay, author_user, bot=None):
    """Resolve and, if possible, edit the original parlay message in DM."""
    if parlay.status != "ACTIVE":
        return
    user = DB["users"][parlay.user_id]
    from embeds import make_embed  # local import to avoid cycles

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

    try:
        if parlay.channel_id and parlay.message_id and bot is not None:
            ch = bot.get_channel(parlay.channel_id) or await bot.fetch_channel(parlay.channel_id)
            msg = await ch.fetch_message(parlay.message_id)
            await msg.edit(embed=make_embed(parlay, author_user), view=None)
            await ch.send(f"**Parlay #{parlay.id.split('-')[0]}** → {outcome} • New balance: **{user['balance']} pts**")
    except Exception:
        pass

def new_parlay_for(uid: str, stake: int, legs_texts: List[str], deadline_dt: datetime) -> Parlay:
    legs = [Leg(text=t) for t in legs_texts]
    legs_count = len(legs)
    mult = PARLAY_MULT.get(legs_count, PARLAY_MULT[MAX_LEGS])
    pid = str(uuid.uuid4())
    p = Parlay(
        id=pid, user_id=uid, stake=stake, legs=legs, legs_count=legs_count,
        multiplier=mult, created_ts=datetime.now(TZ).isoformat(), deadline_ts=deadline_dt.isoformat()
    )
    return p

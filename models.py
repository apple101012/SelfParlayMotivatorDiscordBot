# models.py
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional

@dataclass
class Leg:
    text: str
    status: str = "OPEN"  # OPEN | WIN | FAIL

@dataclass
class Parlay:
    id: str
    user_id: str
    stake: int
    legs: List[Leg]
    legs_count: int
    multiplier: float
    created_ts: str  # ISO
    deadline_ts: str  # ISO (ET)
    status: str = "ACTIVE"  # ACTIVE | WON | LOST
    message_id: Optional[int] = None
    channel_id: Optional[int] = None  # DM channel id
    resolved_ts: Optional[str] = None

    def to_dict(self):
        d = asdict(self)
        d["legs"] = [asdict(l) for l in self.legs]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Parlay":
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

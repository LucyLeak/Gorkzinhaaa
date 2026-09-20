from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from youtube_bot.db.pool import Database

logger = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")
BRT = ZoneInfo("America/Sao_Paulo")
DAILY_QUOTA = 10_000


class QuotaGuardTriggered(RuntimeError):
    """Raised before a non-essential call would consume the safety margin."""


def quota_reset_at(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(timezone.utc)).astimezone(PACIFIC)
    next_day = current.date() + timedelta(days=1)
    return datetime.combine(next_day, datetime.min.time(), tzinfo=PACIFIC).astimezone(
        timezone.utc
    )


class QuotaTracker:
    COSTS = {"search": 100, "insert": 50, "list": 5, "channels": 1, "videos": 1}

    def __init__(self, db: "Database", safety_margin: int = 500, daily_quota: int = DAILY_QUOTA):
        self.db = db
        self.safety_margin = max(0, safety_margin)
        self.daily_quota = daily_quota
        self._memory: dict[date, int] = {}

    @staticmethod
    def date_key(now: datetime | None = None) -> date:
        return (now or datetime.now(timezone.utc)).astimezone(PACIFIC).date()

    def cost_for(self, operation: str) -> int:
        return self.COSTS.get(operation, 1)

    async def _used(self, day: date) -> int:
        if day in self._memory:
            return self._memory[day]
        try:
            value = await self.db.fetchval(
                "SELECT units_used FROM youtube_quota_usage WHERE usage_date = $1",
                day,
            )
        except Exception:
            logger.warning("Quota tracker DB read failed; using in-memory usage.", exc_info=True)
            value = None
        self._memory[day] = int(value or 0)
        return self._memory[day]

    async def reserve(self, operation: str, *, essential: bool = False) -> int:
        cost = self.cost_for(operation)
        day = self.date_key()
        used = await self._used(day)
        remaining = self.daily_quota - used
        if not essential and remaining - cost < self.safety_margin:
            logger.warning(
                "YouTube quota guard triggered: operation=%s cost=%s remaining=%s",
                operation, cost, max(0, remaining),
            )
            raise QuotaGuardTriggered(operation)
        try:
            updated = await self.db.fetchval(
                """INSERT INTO youtube_quota_usage (usage_date, units_used)
                   VALUES ($1, $2)
                   ON CONFLICT (usage_date) DO UPDATE
                   SET units_used = youtube_quota_usage.units_used + EXCLUDED.units_used,
                       updated_at = now()
                   RETURNING units_used""",
                day, cost,
            )
            self._memory[day] = int(updated)
        except Exception:
            logger.warning("Quota tracker DB write failed; retaining in-memory usage.", exc_info=True)
            self._memory[day] = used + cost
        if remaining <= self.safety_margin + cost:
            logger.warning("YouTube quota low: operation=%s remaining_after=%s", operation, remaining - cost)
        return cost

    async def status(self) -> dict:
        day = self.date_key()
        used = await self._used(day)
        reset = quota_reset_at()
        return {
            "date": day.isoformat(),
            "limit": self.daily_quota,
            "used": used,
            "remaining": max(0, self.daily_quota - used),
            "safety_margin": self.safety_margin,
            "reset_at": reset.isoformat().replace("+00:00", "Z"),
            "reset_brt": reset.astimezone(BRT).isoformat(),
            "reset_pt": reset.astimezone(PACIFIC).isoformat(),
        }


def quota_exceeded_payload() -> dict:
    reset = quota_reset_at()
    reset_at = reset.isoformat().replace("+00:00", "Z")
    hours = max(1, math.ceil((reset - datetime.now(timezone.utc)).total_seconds() / 3600))
    return {
        "ok": False,
        "error": "quota_exceeded",
        "reset_at": reset_at,
        "message": f"Cota da YouTube API esgotada. Reinicia em {hours} horas.",
    }

"""Approximate net AC energy from successive socket-power samples."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo


@dataclass
class EnergyEstimate:
    """Persistent counters; the sampling baseline intentionally is not persisted."""

    charged_total: float = 0
    discharged_total: float = 0
    charged_daily: float = 0
    discharged_daily: float = 0
    day: str = ""
    _previous_time: datetime | None = None
    _previous_power: float | None = None

    def update(self, now: datetime, power: float | None, timezone: tzinfo) -> None:
        """Integrate the previous sample, splitting local days and ignoring outages."""
        start = self._previous_time
        previous_power = self._previous_power
        self._previous_time = now
        self._previous_power = power
        if (
            start is not None
            and previous_power is not None
            and power is not None
            and 0 < (now - start).total_seconds() <= 120
        ):
            while start < now:
                local_start = start.astimezone(timezone)
                self._set_day(local_start.date().isoformat())
                midnight = datetime.combine(
                    local_start.date() + timedelta(days=1), time.min, timezone
                ).astimezone(now.tzinfo)
                end = min(now, midnight)
                energy = abs(previous_power) * (end - start).total_seconds() / 3_600_000
                if previous_power < 0:
                    self.charged_total += energy
                    self.charged_daily += energy
                else:
                    self.discharged_total += energy
                    self.discharged_daily += energy
                start = end
        self._set_day(now.astimezone(timezone).date().isoformat())

    def _set_day(self, day: str) -> None:
        if self.day != day:
            self.day = day
            self.charged_daily = self.discharged_daily = 0

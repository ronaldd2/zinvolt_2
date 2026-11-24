"""Expose raw API modes without rejecting values unknown to the upstream enum."""

from dataclasses import dataclass, field

from mashumaro import field_options
from mashumaro.mixins.orjson import DataClassORJSONMixin
from zinvolt import ZinvoltClient
from zinvolt.models import CurrentPower, GlobalSettings, OnlineStatus


@dataclass
class ObservedBatteryState(DataClassORJSONMixin):
    """Battery telemetry with the unmodified smart-mode string."""

    serial_number: str = field(metadata=field_options(alias="sn"))
    name: str
    online_status: OnlineStatus = field(metadata=field_options(alias="onlineStatus"))
    current_power: CurrentPower = field(metadata=field_options(alias="currentPower"))
    smart_mode: str = field(metadata=field_options(alias="smartMode"))
    global_settings: GlobalSettings = field(metadata=field_options(alias="globalSettings"))


class ObservedZinvoltClient(ZinvoltClient):
    """Keep unknown smart modes observable while retaining the upstream transport."""

    async def get_observed_battery_status(self, battery_id: str) -> ObservedBatteryState:
        """Read the same status endpoint without enum coercion."""
        result = await self._get(f"system/{battery_id}/basic/current-state")
        return ObservedBatteryState.from_json(result)

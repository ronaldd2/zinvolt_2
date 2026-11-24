"""Persistent local caps and synthetic modes for a Zinvolt battery."""

from dataclasses import asdict
from datetime import datetime
import logging
import math
from typing import Any

from zinvolt.exceptions import ZinvoltError
from zinvolt.models import OnlineStatus, SmartMode

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .client import ObservedBatteryState, ObservedZinvoltClient
from .const import DOMAIN
from .energy import EnergyEstimate

_LOGGER = logging.getLogger(__name__)
LOCAL_MODES = ("charge_only", "discharge_only", "inactive")


class BatteryControl:
    """State belonging to one battery, accessed under the coordinator's lock."""

    def __init__(self, hass: HomeAssistant, entry_id: str, battery_id: str) -> None:
        self.store = Store[dict[str, Any]](
            hass, 1, f"{DOMAIN}.{entry_id}.{battery_id}", atomic_writes=True
        )
        self.energy = EnergyEstimate()
        self.charge_limit = 2000
        self.discharge_limit = 2000
        self.requested_output: int | None = None
        self.local_mode: str | None = None
        self.pause_until = 0.0
        self._last_api_mode: str | None = None
        self._save_scheduled = False

    async def async_load(self) -> None:
        """Restore caps, synthetic mode and counters without integrating downtime."""
        if (data := await self.store.async_load()) is not None:
            self.charge_limit = data["charge_limit"]
            self.discharge_limit = data["discharge_limit"]
            self.requested_output = data["requested_output"]
            self.local_mode = data["local_mode"]
            self.pause_until = data["pause_until"]
            self.energy = EnergyEstimate(**data["energy"])

    def _data(self) -> dict[str, Any]:
        energy = asdict(self.energy)
        energy.pop("_previous_time")
        energy.pop("_previous_power")
        return {
            "charge_limit": self.charge_limit,
            "discharge_limit": self.discharge_limit,
            "requested_output": self.requested_output,
            "local_mode": self.local_mode,
            "pause_until": self.pause_until,
            "energy": energy,
        }

    def schedule_save(self) -> None:
        """Coalesce periodic counter writes and flush them at HA shutdown."""
        if not self._save_scheduled:
            self._save_scheduled = True
            self.store.async_delay_save(self._delayed_data, 60)

    def _delayed_data(self) -> dict[str, Any]:
        self._save_scheduled = False
        return self._data()

    async def async_save(self) -> None:
        """Flush state and cancel any pending delayed write."""
        self._save_scheduled = False
        await self.store.async_save(self._data())

    @staticmethod
    def output_max(state: ObservedBatteryState) -> int:
        """Respect the hardware's advertised discharge ceiling."""
        settings = state.global_settings
        return 2000 if settings.max_output_unlocked else settings.max_output_limit

    @property
    def charge_max(self) -> int:
        """Keep the live charge control at zero while inactive or paused."""
        if self.local_mode == "inactive" or (
            self.local_mode == "discharge_only" and self.pause_until
        ):
            return 0
        return self.charge_limit

    async def async_observe(
        self,
        client: ObservedZinvoltClient,
        battery_id: str,
        state: ObservedBatteryState,
        now: datetime,
    ) -> None:
        """Observe mode and energy, then enforce the user's local policy."""
        if self._last_api_mode != state.smart_mode:
            _LOGGER.info("Zinvolt API smart mode changed to %s", state.smart_mode)
            self._last_api_mode = state.smart_mode
        online = state.current_power.online_status is OnlineStatus.ONLINE
        power = state.current_power.power_socket_output if online else None
        self.energy.update(now, power, dt_util.get_default_time_zone())
        self.schedule_save()
        settings = state.global_settings
        self.discharge_limit = min(self.discharge_limit, self.output_max(state))
        if not online:
            return
        output_limit = self.discharge_limit
        if self.local_mode is None or self.requested_output is None:
            self.requested_output = min(settings.max_output, output_limit)
        target_output = min(settings.max_output, output_limit)
        if self.local_mode == "charge_only":
            target_output = 0
        elif self.local_mode == "discharge_only":
            target_output = min(self.requested_output, output_limit)
        if settings.max_output != target_output:
            await client.set_max_output(battery_id, target_output)
        if settings.maximum_charge_power > self.charge_max:
            await client.set_max_charge_power(battery_id, self.charge_max)
        if self.local_mode == "charge_only" and state.smart_mode != SmartMode.SELF_USE:
            await self._async_apply_mode(
                client, battery_id, state, SmartMode.SELF_USE, output_blocked=True
            )
        elif self.local_mode == "inactive" and state.smart_mode != SmartMode.CHARGED:
            await self._async_apply_mode(
                client, battery_id, state, SmartMode.CHARGED, charge_blocked=True
            )
        elif self.local_mode == "discharge_only":
            if self.pause_until:
                if now.timestamp() >= self.pause_until:
                    await self._async_apply_mode(
                        client, battery_id, state, SmartMode.SELF_USE
                    )
                    self.pause_until = 0
                elif state.smart_mode != SmartMode.CHARGED:
                    await self._async_apply_mode(
                        client,
                        battery_id,
                        state,
                        SmartMode.CHARGED,
                        charge_blocked=True,
                    )
            elif power is not None and power < 0:
                await self._async_apply_mode(
                    client, battery_id, state, SmartMode.CHARGED, charge_blocked=True
                )
                self.pause_until = now.timestamp() + 180
            elif state.smart_mode != SmartMode.SELF_USE:
                await self._async_apply_mode(
                    client, battery_id, state, SmartMode.SELF_USE
                )
        self.schedule_save()

    async def async_set_limit(
        self,
        client: ObservedZinvoltClient,
        battery_id: str,
        state: ObservedBatteryState,
        key: str,
        value: float,
    ) -> None:
        """Apply lower limits immediately, persisting only a successful change."""
        if key not in ("charge_limit", "discharge_limit"):
            raise ServiceValidationError(f"Unknown power limit: {key}")
        maximum = 2000 if key == "charge_limit" else self.output_max(state)
        self._validate_power(value, maximum)
        limit = int(value)
        try:
            if key == "charge_limit":
                target = 0 if self.charge_max == 0 else limit
                if state.global_settings.maximum_charge_power > target:
                    await client.set_max_charge_power(battery_id, target)
                self.charge_limit = limit
            else:
                target = 0 if self.local_mode == "charge_only" else limit
                if state.global_settings.max_output > target:
                    await client.set_max_output(battery_id, target)
                self.discharge_limit = limit
                if self.requested_output is not None:
                    self.requested_output = min(self.requested_output, limit)
        except ZinvoltError as err:
            raise HomeAssistantError(f"Unable to apply power limit: {err}") from err
        await self.async_save()

    async def async_set_power(
        self,
        client: ObservedZinvoltClient,
        battery_id: str,
        state: ObservedBatteryState,
        key: str,
        value: float,
    ) -> None:
        """Reject commands above the cap rather than silently changing the value."""
        if key not in ("max_charge_power", "max_output"):
            raise ServiceValidationError(f"Unknown power control: {key}")
        maximum = (
            self.charge_max
            if key == "max_charge_power"
            else min(self.discharge_limit, self.output_max(state))
        )
        if key == "max_output" and self.local_mode == "charge_only":
            maximum = 0
        self._validate_power(value, maximum)
        try:
            if key == "max_charge_power":
                await client.set_max_charge_power(battery_id, int(value))
            else:
                await client.set_max_output(battery_id, int(value))
                self.requested_output = int(value)
        except ZinvoltError as err:
            raise HomeAssistantError(f"Unable to set battery power: {err}") from err
        await self.async_save()

    async def async_set_mode(
        self,
        client: ObservedZinvoltClient,
        battery_id: str,
        state: ObservedBatteryState,
        mode: SmartMode | str,
    ) -> None:
        """Switch policy and copy configured limits to both live power controls."""
        local_mode = mode if mode in LOCAL_MODES else None
        if local_mode is None and not isinstance(mode, SmartMode):
            raise ServiceValidationError(f"Unsupported mode: {mode}")
        if local_mode == "inactive":
            api_mode = SmartMode.CHARGED
        elif local_mode:
            api_mode = SmartMode.SELF_USE
        else:
            api_mode = SmartMode(mode)
        try:
            await self._async_apply_mode(
                client,
                battery_id,
                state,
                api_mode,
                charge_blocked=local_mode == "inactive",
                output_blocked=local_mode == "charge_only",
            )
        except ZinvoltError as err:
            raise HomeAssistantError(f"Unable to switch battery mode: {err}") from err
        self.local_mode = local_mode
        self.pause_until = 0
        await self.async_save()

    async def _async_apply_mode(
        self,
        client: ObservedZinvoltClient,
        battery_id: str,
        state: ObservedBatteryState,
        mode: SmartMode,
        *,
        charge_blocked: bool = False,
        output_blocked: bool = False,
    ) -> None:
        """Apply zero overrides before switching; restore other powers afterwards."""
        charge = 0 if charge_blocked else self.charge_limit
        output = (
            0 if output_blocked else min(self.discharge_limit, self.output_max(state))
        )
        if charge == 0:
            await client.set_max_charge_power(battery_id, 0)
        if output == 0:
            await client.set_max_output(battery_id, 0)
        await client.set_smart_mode(battery_id, mode)
        if output:
            await client.set_max_output(battery_id, output)
        if charge:
            await client.set_max_charge_power(battery_id, charge)
        self.requested_output = min(self.discharge_limit, self.output_max(state))

    @staticmethod
    def _validate_power(value: float, maximum: int) -> None:
        if not math.isfinite(value) or value != int(value):
            raise ServiceValidationError("Power must be a finite whole number of watts")
        if not 0 <= value <= maximum:
            raise ServiceValidationError(f"Power must be between 0 and {maximum} W")

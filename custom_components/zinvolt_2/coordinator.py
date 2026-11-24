"""Coordinator for Zinvolt, modified for partial unit failures."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import override

from zinvolt.exceptions import ZinvoltAuthenticationError, ZinvoltError
from zinvolt.models import Battery, SmartMode, Unit, UnitType

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import ObservedBatteryState, ObservedZinvoltClient
from .const import DOMAIN
from .control import BatteryControl

_LOGGER = logging.getLogger(__name__)

type ZinvoltConfigEntry = ConfigEntry[dict[str, ZinvoltDeviceCoordinator]]


@dataclass
class BatteryData:
    """Data per battery unit."""

    sw_version: str
    model: str
    points: dict[str, bool]


@dataclass
class ZinvoltData:
    """Data for the Zinvolt integration."""

    battery: ObservedBatteryState
    batteries: dict[str, BatteryData]


class ZinvoltDeviceCoordinator(DataUpdateCoordinator[ZinvoltData]):
    """Class for Zinvolt devices."""

    config_entry: ZinvoltConfigEntry
    battery_units: dict[str, Unit]

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ZinvoltConfigEntry,
        client: ObservedZinvoltClient,
        battery: Battery,
    ) -> None:
        """Initialize the Zinvolt device."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"Zinvolt {battery.identifier}",
            update_interval=timedelta(seconds=30),
        )
        self.battery = battery
        self.client = client
        self.control = BatteryControl(hass, config_entry.entry_id, battery.identifier)
        self.control_lock = asyncio.Lock()

    @override
    async def _async_setup(self) -> None:
        """Set up the Zinvolt integration."""
        await self.control.async_load()
        try:
            units = await self.client.get_units(self.battery.identifier)
        except ZinvoltAuthenticationError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="authentication_failed"
            ) from err
        except ZinvoltError as err:
            raise UpdateFailed(
                translation_key="update_failed", translation_domain=DOMAIN
            ) from err
        self.battery_units = {
            unit.serial_number: unit for unit in units if unit.type is UnitType.BATTERY
        }

    @override
    async def _async_update_data(self) -> ZinvoltData:
        """Update data from Zinvolt."""
        async with self.control_lock:
            return await self._async_fetch_data()

    async def _async_fetch_data(self) -> ZinvoltData:
        """Fetch and enforce policy without racing entity commands."""
        try:
            battery_state = await self.client.get_observed_battery_status(
                self.battery.identifier
            )
            await self.control.async_observe(
                self.client, self.battery.identifier, battery_state, dt_util.utcnow()
            )
        except ZinvoltAuthenticationError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="authentication_failed"
            ) from err
        except ZinvoltError as err:
            self.control.energy.update(
                dt_util.utcnow(), None, dt_util.get_default_time_zone()
            )
            _LOGGER.warning(
                "Unable to refresh battery status or apply local controls: %s",
                err,
                exc_info=True,
            )
            raise UpdateFailed(
                translation_key="update_failed",
                translation_domain=DOMAIN,
            ) from err

        battery_units = {}
        for unit_serial_number in self.battery_units:
            try:
                battery_units[unit_serial_number] = await self.client.get_battery_unit(
                    self.battery.identifier, unit_serial_number
                )
            except ZinvoltAuthenticationError as err:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="authentication_failed"
                ) from err
            except ZinvoltError as err:
                _LOGGER.warning(
                    "Unable to fetch battery unit %s; other data remains available: %s",
                    unit_serial_number,
                    err,
                    exc_info=True,
                )
        return ZinvoltData(
            battery_state,
            {
                serial_number: BatteryData(
                    battery_unit.version.current_version,
                    battery_unit.battery_model,
                    {
                        point.point.lower(): point.normal
                        for point in battery_unit.points
                    },
                )
                for serial_number, battery_unit in battery_units.items()
            },
        )

    async def async_set_retry_delay(self, value: float) -> None:
        """Set the local retry delay and notify entities without a cloud request."""
        async with self.control_lock:
            await self.control.async_set_retry_delay(value)
        self.async_update_listeners()

    async def async_set_limit(self, key: str, value: float) -> None:
        """Set a persistent local cap."""
        async with self.control_lock:
            await self.control.async_set_limit(
                self.client, self.battery.identifier, self.data.battery, key, value
            )
        await self.async_request_refresh()

    async def async_set_power(self, key: str, value: float) -> None:
        """Set power through the local cap."""
        async with self.control_lock:
            await self.control.async_set_power(
                self.client, self.battery.identifier, self.data.battery, key, value
            )
        await self.async_request_refresh()

    async def async_set_mode(self, mode: SmartMode | str) -> None:
        """Select a native or synthetic mode."""
        async with self.control_lock:
            await self.control.async_set_mode(
                self.client, self.battery.identifier, self.data.battery, mode
            )
        await self.async_request_refresh()

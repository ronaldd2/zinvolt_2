"""Zinvolt select platform, modified to expose synthetic local modes."""

from typing import override

from zinvolt.models import SmartMode

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .control import LOCAL_MODES
from .coordinator import ZinvoltConfigEntry, ZinvoltDeviceCoordinator
from .entity import ZinvoltEntity

MODE_MAP: dict[str, str] = {
    SmartMode.DYNAMIC: "dynamic",
    SmartMode.SELF_USE: "self_use",
    SmartMode.PERFORMANCE: "fast_discharge",
    SmartMode.CHARGED: "fast_charge",
    SmartMode.FEED: "connected_solar_panels",
}

HA_TO_MODE = {v: k for k, v in MODE_MAP.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZinvoltConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Initialize the entries."""

    async_add_entities(
        ZinvoltBatteryMode(coordinator) for coordinator in entry.runtime_data.values()
    )


class ZinvoltBatteryMode(ZinvoltEntity, SelectEntity):
    """Zinvolt select."""

    _attr_options = [*HA_TO_MODE, *LOCAL_MODES]
    _attr_translation_key = "battery_mode"

    def __init__(self, coordinator: ZinvoltDeviceCoordinator) -> None:
        """Initialize the select."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.battery.serial_number}.mode"

    @property
    @override
    def current_option(self) -> str | None:
        """Return the current battery mode."""
        return self.coordinator.control.local_mode or MODE_MAP.get(
            self.coordinator.data.battery.smart_mode
        )

    @override
    async def async_select_option(self, option: str) -> None:
        """Set battery mode."""
        await self.coordinator.async_set_mode(
            HA_TO_MODE[option] if option in HA_TO_MODE else option
        )

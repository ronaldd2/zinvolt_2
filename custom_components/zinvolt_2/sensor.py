"""Zinvolt sensors, modified to estimate energy and expose raw API modes."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import override

from zinvolt.models import OnlineStatus

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .coordinator import ZinvoltConfigEntry, ZinvoltData, ZinvoltDeviceCoordinator
from .entity import ZinvoltEntity


@dataclass(kw_only=True, frozen=True)
class ZinvoltBatteryStateDescription(SensorEntityDescription):
    """Sensor description for Zinvolt battery state."""

    value_fn: Callable[[ZinvoltData], float | None]


SENSORS: tuple[ZinvoltBatteryStateDescription, ...] = (
    ZinvoltBatteryStateDescription(
        key="state_of_charge",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda state: state.battery.current_power.state_of_charge,
    ),
    ZinvoltBatteryStateDescription(
        key="power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=(
            lambda state: (
                None
                if state.battery.current_power.power_socket_output is None
                else 0 - state.battery.current_power.power_socket_output
            )
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZinvoltConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Initialize the entries."""

    async_add_entities(
        ZinvoltBatteryStateSensor(coordinator, description)
        for description in SENSORS
        for coordinator in entry.runtime_data.values()
    )
    async_add_entities(
        ZinvoltEnergySensor(coordinator, direction, daily)
        for coordinator in entry.runtime_data.values()
        for direction in ("charged", "discharged")
        for daily in (False, True)
    )
    async_add_entities(
        ZinvoltRawModeSensor(coordinator)
        for coordinator in entry.runtime_data.values()
    )
    for coordinator in entry.runtime_data.values():
        _track_midnight(hass, entry, coordinator)


@callback
def _track_midnight(
    hass: HomeAssistant,
    entry: ZinvoltConfigEntry,
    coordinator: ZinvoltDeviceCoordinator,
) -> None:
    @callback
    def reset_daily(_now: datetime) -> None:
        power = coordinator.data.battery.current_power
        coordinator.control.energy.update(
            dt_util.utcnow(),
            power.power_socket_output
            if (
                coordinator.last_update_success
                and power.online_status is OnlineStatus.ONLINE
            )
            else None,
            dt_util.get_default_time_zone(),
        )
        coordinator.control.schedule_save()
        coordinator.async_update_listeners()

    entry.async_on_unload(
        async_track_time_change(hass, reset_daily, hour=0, minute=0, second=0)
    )


class ZinvoltBatteryStateSensor(ZinvoltEntity, SensorEntity):
    """Zinvolt battery state sensor."""

    entity_description: ZinvoltBatteryStateDescription

    def __init__(
        self,
        coordinator: ZinvoltDeviceCoordinator,
        description: ZinvoltBatteryStateDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"{coordinator.data.battery.serial_number}.{description.key}"
        )

    @property
    @override
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator.data)


class ZinvoltEnergySensor(ZinvoltEntity, SensorEntity):
    """Approximate charged or discharged net AC energy since installation."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(
        self, coordinator: ZinvoltDeviceCoordinator, direction: str, daily: bool
    ) -> None:
        super().__init__(coordinator)
        self.direction = direction
        self.daily = daily
        self.key = f"{direction}_{'daily' if daily else 'total'}"
        self._attr_translation_key = f"energy_{self.key}"
        self._attr_unique_id = (
            f"{coordinator.data.battery.serial_number}.energy_{self.key}"
        )

    @property
    def native_value(self) -> float:
        energy = self.coordinator.control.energy
        if self.direction == "charged":
            return energy.charged_daily if self.daily else energy.charged_total
        return energy.discharged_daily if self.daily else energy.discharged_total


class ZinvoltRawModeSensor(ZinvoltEntity, SensorEntity):
    """Show the exact API value, including modes unknown to the upstream client."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "api_smart_mode"

    def __init__(self, coordinator: ZinvoltDeviceCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.battery.serial_number}.api_smart_mode"

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> str:
        return self.coordinator.data.battery.smart_mode

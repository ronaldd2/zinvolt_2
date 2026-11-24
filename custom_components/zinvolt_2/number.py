"""Zinvolt number platform, modified to enforce persistent power ceilings."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import override

from zinvolt import ZinvoltClient

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ZinvoltConfigEntry, ZinvoltData, ZinvoltDeviceCoordinator
from .entity import ZinvoltEntity


@dataclass(kw_only=True, frozen=True)
class ZinvoltBatteryStateDescription(NumberEntityDescription):
    """Number description for Zinvolt battery state."""

    max_fn: Callable[[ZinvoltData], int] | None = None
    value_fn: Callable[[ZinvoltData], int]
    set_value_fn: Callable[[ZinvoltClient, str, int], Awaitable[None]]


NUMBERS: tuple[ZinvoltBatteryStateDescription, ...] = (
    ZinvoltBatteryStateDescription(
        key="max_output",
        translation_key="max_output",
        entity_category=EntityCategory.CONFIG,
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda state: state.battery.global_settings.max_output,
        set_value_fn=lambda client, battery_id, value: client.set_max_output(
            battery_id, value
        ),
        native_min_value=0,
        max_fn=lambda state: (
            2000
            if state.battery.global_settings.max_output_unlocked
            else state.battery.global_settings.max_output_limit
        ),
    ),
    ZinvoltBatteryStateDescription(
        key="upper_threshold",
        translation_key="upper_threshold",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda state: state.battery.global_settings.battery_upper_threshold,
        set_value_fn=lambda client, battery_id, value: client.set_upper_threshold(
            battery_id, value
        ),
        native_min_value=0,
        native_max_value=100,
    ),
    ZinvoltBatteryStateDescription(
        key="lower_threshold",
        translation_key="lower_threshold",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda state: state.battery.global_settings.battery_lower_threshold,
        set_value_fn=lambda client, battery_id, value: client.set_lower_threshold(
            battery_id, value
        ),
        native_min_value=9,
        native_max_value=100,
    ),
    ZinvoltBatteryStateDescription(
        key="max_charge_power",
        translation_key="max_charge_power",
        entity_category=EntityCategory.CONFIG,
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda state: state.battery.global_settings.maximum_charge_power,
        set_value_fn=lambda client, battery_id, value: client.set_max_charge_power(
            battery_id, value
        ),
        native_min_value=0,
        native_max_value=2000,
    ),
    ZinvoltBatteryStateDescription(
        key="standby_time",
        translation_key="standby_time",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=NumberDeviceClass.DURATION,
        value_fn=lambda state: state.battery.global_settings.standby_time,
        set_value_fn=lambda client, battery_id, value: client.set_standby_time(
            battery_id, value
        ),
        native_min_value=5,
        native_max_value=60,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZinvoltConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Initialize the entries."""

    async_add_entities(
        ZinvoltBatteryStateNumber(coordinator, description)
        for description in NUMBERS
        for coordinator in entry.runtime_data.values()
    )
    async_add_entities(
        ZinvoltPowerLimit(coordinator, key)
        for coordinator in entry.runtime_data.values()
        for key in ("charge_limit", "discharge_limit")
    )
    async_add_entities(
        ZinvoltRetryDelay(coordinator)
        for coordinator in entry.runtime_data.values()
    )


class ZinvoltBatteryStateNumber(ZinvoltEntity, NumberEntity):
    """Zinvolt number."""

    entity_description: ZinvoltBatteryStateDescription

    def __init__(
        self,
        coordinator: ZinvoltDeviceCoordinator,
        description: ZinvoltBatteryStateDescription,
    ) -> None:
        """Initialize the number."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"{coordinator.data.battery.serial_number}.{description.key}"
        )

    @property
    @override
    def native_max_value(self) -> float:
        """Return the native maximum value."""
        if self.entity_description.key == "max_output":
            if self.coordinator.control.local_mode == "charge_only":
                return 0
            return min(
                self.coordinator.control.discharge_limit,
                self.coordinator.control.output_max(self.coordinator.data.battery),
            )
        if self.entity_description.key == "max_charge_power":
            return self.coordinator.control.charge_max
        if self.entity_description.max_fn is None:
            return super().native_max_value
        return self.entity_description.max_fn(self.coordinator.data)

    @property
    @override
    def native_value(self) -> float:
        """Return the state of the sensor."""
        return self.entity_description.value_fn(self.coordinator.data)

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Set the state of the sensor."""
        if self.entity_description.key in ("max_output", "max_charge_power"):
            await self.coordinator.async_set_power(self.entity_description.key, value)
            return
        await self.entity_description.set_value_fn(
            self.coordinator.client, self.coordinator.battery.identifier, int(value)
        )
        await self.coordinator.async_request_refresh()


class ZinvoltPowerLimit(ZinvoltEntity, NumberEntity):
    """A persistent ceiling separate from the API's live power setpoint."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 0
    _attr_native_step = 1

    def __init__(self, coordinator: ZinvoltDeviceCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self.key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.data.battery.serial_number}.{key}"

    @property
    def native_value(self) -> float:
        return (
            self.coordinator.control.charge_limit
            if self.key == "charge_limit"
            else self.coordinator.control.discharge_limit
        )

    @property
    def native_max_value(self) -> float:
        return (
            2000
            if self.key == "charge_limit"
            else self.coordinator.control.output_max(self.coordinator.data.battery)
        )

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_limit(self.key, value)


class ZinvoltRetryDelay(ZinvoltEntity, NumberEntity):
    """Persistent duration of a discharge-only retry countdown."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 1
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_translation_key = "retry_delay"

    def __init__(self, coordinator: ZinvoltDeviceCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.data.battery.serial_number}.retry_delay"

    @property
    def native_value(self) -> float:
        return self.coordinator.control.retry_delay_minutes

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_retry_delay(value)

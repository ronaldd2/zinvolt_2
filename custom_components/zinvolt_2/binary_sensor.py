"""Binary sensors for Zinvolt, modified to discover units after a failed fetch."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ZinvoltConfigEntry, ZinvoltData, ZinvoltDeviceCoordinator
from .entity import ZinvoltEntity, ZinvoltUnitEntity

POINT_ENTITIES = {
    "communication": BinarySensorDeviceClass.PROBLEM,
    "voltage": BinarySensorDeviceClass.PROBLEM,
    "current": BinarySensorDeviceClass.PROBLEM,
    "temperature": BinarySensorDeviceClass.HEAT,
    "charge": BinarySensorDeviceClass.PROBLEM,
    "discharge": BinarySensorDeviceClass.PROBLEM,
    "other": BinarySensorDeviceClass.PROBLEM,
}


@dataclass(kw_only=True, frozen=True)
class ZinvoltBatteryStateDescription(BinarySensorEntityDescription):
    """Binary sensor description for Zinvolt battery state."""

    is_on_fn: Callable[[ZinvoltData], bool | None]


SENSORS: tuple[ZinvoltBatteryStateDescription, ...] = (
    ZinvoltBatteryStateDescription(
        key="on_grid",
        translation_key="on_grid",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=lambda state: state.battery.current_power.on_grid,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZinvoltConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Initialize the entries."""

    entities: list[BinarySensorEntity] = [
        ZinvoltBatteryStateBinarySensor(coordinator, description)
        for description in SENSORS
        for coordinator in entry.runtime_data.values()
    ]
    async_add_entities(entities)

    def add_unit_sensors(coordinator: ZinvoltDeviceCoordinator) -> None:
        known_points: set[tuple[str, str]] = set()

        @callback
        def update_unit_sensors() -> None:
            new_entities = []
            for serial_number, battery in coordinator.data.batteries.items():
                for point in battery.points:
                    key = (serial_number, point)
                    if point in POINT_ENTITIES and key not in known_points:
                        new_entities.append(
                            ZinvoltPointBinarySensor(coordinator, serial_number, point)
                        )
                        known_points.add(key)
            if new_entities:
                async_add_entities(new_entities)

        update_unit_sensors()
        entry.async_on_unload(coordinator.async_add_listener(update_unit_sensors))

    for coordinator in entry.runtime_data.values():
        add_unit_sensors(coordinator)


class ZinvoltBatteryStateBinarySensor(ZinvoltEntity, BinarySensorEntity):
    """Zinvolt battery state binary sensor."""

    entity_description: ZinvoltBatteryStateDescription

    def __init__(
        self,
        coordinator: ZinvoltDeviceCoordinator,
        description: ZinvoltBatteryStateDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = (
            f"{coordinator.data.battery.serial_number}.{description.key}"
        )

    @property
    @override
    def is_on(self) -> bool | None:
        """Return the state of the binary sensor."""
        return self.entity_description.is_on_fn(self.coordinator.data)


class ZinvoltPointBinarySensor(ZinvoltUnitEntity, BinarySensorEntity):
    """Zinvolt battery state binary sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ZinvoltDeviceCoordinator, unit_serial_number: str, point: str
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, unit_serial_number)
        self.point = point
        self._attr_translation_key = point
        self._attr_device_class = POINT_ENTITIES[point]
        self._attr_unique_id = f"{self.serial_number}.{point}"

    @property
    @override
    def available(self) -> bool:
        """Return the availability of the binary sensor."""
        return super().available and self.point in self.battery.points

    @property
    @override
    def is_on(self) -> bool:
        """Return the state of the binary sensor."""
        return not self.battery.points[self.point]

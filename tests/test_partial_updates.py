"""Focused tests for partial battery-unit failures without a Home Assistant install."""

from dataclasses import dataclass
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class ZinvoltError(Exception):
    pass


class ZinvoltAuthenticationError(ZinvoltError):
    pass


class Base:
    def __init_subclass__(cls, **kwargs):
        pass

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, *args, **kwargs):
        if args:
            self.coordinator = args[0]


class UpdateFailed(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)


class ConfigEntryAuthFailed(UpdateFailed):
    pass


@dataclass(kw_only=True, frozen=True)
class Description:
    key: str
    translation_key: str = ""
    entity_category: object = None
    device_class: object = None
    state_class: object = None
    native_unit_of_measurement: object = None
    native_min_value: float = 0
    native_max_value: float = 100


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class PartialUpdatesTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        package = module("custom_components.zinvolt_2")
        package.__path__ = [str(root / "custom_components" / "zinvolt_2")]
        modules = {
            "custom_components.zinvolt_2": package,
            "custom_components.zinvolt_2.client": module(
                "custom_components.zinvolt_2.client",
                ObservedBatteryState=object,
                ObservedZinvoltClient=object,
            ),
            "custom_components.zinvolt_2.control": module(
                "custom_components.zinvolt_2.control",
                BatteryControl=lambda *args: SimpleNamespace(
                    async_observe=AsyncMock(), energy=SimpleNamespace(update=Mock())
                ),
            ),
            "zinvolt": module("zinvolt", ZinvoltClient=object),
            "zinvolt.exceptions": module(
                "zinvolt.exceptions",
                ZinvoltError=ZinvoltError,
                ZinvoltAuthenticationError=ZinvoltAuthenticationError,
            ),
            "zinvolt.models": module(
                "zinvolt.models",
                Battery=object,
                BatteryState=object,
                Unit=object,
                UnitType=SimpleNamespace(BATTERY=object()),
                OnlineStatus=SimpleNamespace(ONLINE=object()),
                SmartMode=str,
            ),
            "homeassistant": module("homeassistant"),
            "homeassistant.config_entries": module(
                "homeassistant.config_entries", ConfigEntry=Base
            ),
            "homeassistant.core": module(
                "homeassistant.core", HomeAssistant=object, callback=lambda fn: fn
            ),
            "homeassistant.const": module(
                "homeassistant.const",
                EntityCategory=SimpleNamespace(DIAGNOSTIC=object(), CONFIG=object()),
                PERCENTAGE="%",
                UnitOfPower=SimpleNamespace(WATT="W"),
                UnitOfTime=SimpleNamespace(MINUTES="min"),
                UnitOfEnergy=SimpleNamespace(KILO_WATT_HOUR="kWh"),
            ),
            "homeassistant.components": module("homeassistant.components"),
            "homeassistant.components.binary_sensor": module(
                "homeassistant.components.binary_sensor",
                BinarySensorDeviceClass=SimpleNamespace(
                    PROBLEM=object(), HEAT=object(), CONNECTIVITY=object()
                ),
                BinarySensorEntity=Base,
                BinarySensorEntityDescription=Description,
            ),
            "homeassistant.components.sensor": module(
                "homeassistant.components.sensor",
                SensorDeviceClass=SimpleNamespace(
                    BATTERY="battery", POWER="power", ENERGY="energy"
                ),
                SensorStateClass=SimpleNamespace(
                    MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing"
                ),
                SensorEntity=Base,
                SensorEntityDescription=Description,
            ),
            "homeassistant.components.number": module(
                "homeassistant.components.number",
                NumberDeviceClass=SimpleNamespace(POWER="power", DURATION="duration"),
                NumberEntity=Base,
                NumberEntityDescription=Description,
            ),
            "homeassistant.exceptions": module(
                "homeassistant.exceptions", ConfigEntryAuthFailed=ConfigEntryAuthFailed
            ),
            "homeassistant.helpers": module("homeassistant.helpers"),
            "homeassistant.util": module("homeassistant.util"),
            "homeassistant.util.dt": module(
                "homeassistant.util.dt",
                utcnow=Mock(),
                get_default_time_zone=Mock(),
            ),
            "homeassistant.helpers.device_registry": module(
                "homeassistant.helpers.device_registry", DeviceInfo=dict
            ),
            "homeassistant.helpers.entity_platform": module(
                "homeassistant.helpers.entity_platform",
                AddConfigEntryEntitiesCallback=object,
            ),
            "homeassistant.helpers.event": module(
                "homeassistant.helpers.event", async_track_time_change=Mock()
            ),
            "homeassistant.helpers.update_coordinator": module(
                "homeassistant.helpers.update_coordinator",
                DataUpdateCoordinator=Base,
                CoordinatorEntity=Base,
                UpdateFailed=UpdateFailed,
            ),
        }
        cls.module_patch = patch.dict(sys.modules, modules)
        cls.module_patch.start()
        cls.coordinator_module = importlib.import_module(
            "custom_components.zinvolt_2.coordinator"
        )
        cls.binary_sensor_module = importlib.import_module(
            "custom_components.zinvolt_2.binary_sensor"
        )
        cls.sensor_module = importlib.import_module("custom_components.zinvolt_2.sensor")
        cls.number_module = importlib.import_module("custom_components.zinvolt_2.number")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("custom_components.zinvolt_2.coordinator", None)
        sys.modules.pop("custom_components.zinvolt_2.binary_sensor", None)
        sys.modules.pop("custom_components.zinvolt_2.entity", None)
        sys.modules.pop("custom_components.zinvolt_2.const", None)
        sys.modules.pop("custom_components.zinvolt_2.sensor", None)
        sys.modules.pop("custom_components.zinvolt_2.number", None)
        cls.module_patch.stop()

    def make_coordinator(self, unit_fetch):
        client = SimpleNamespace(
            get_observed_battery_status=AsyncMock(return_value="main status"),
            get_battery_unit=AsyncMock(side_effect=unit_fetch),
        )
        coordinator = self.coordinator_module.ZinvoltDeviceCoordinator(
            None, SimpleNamespace(entry_id="entry"), client,
            SimpleNamespace(identifier="battery")
        )
        coordinator.battery_units = {"unit-1": object(), "unit-2": object()}
        return coordinator, client

    async def test_unit_failure_preserves_status_and_other_unit_then_recovers(self):
        unit = SimpleNamespace(
            version=SimpleNamespace(current_version="1"),
            battery_model="model",
            points=[SimpleNamespace(point="Voltage", normal=True)],
        )

        async def fetch(battery_id, serial):
            if serial == "unit-1":
                raise ZinvoltError("unit unavailable")
            return unit

        coordinator, client = self.make_coordinator(fetch)
        with self.assertLogs(level="WARNING"):
            result = await coordinator._async_update_data()
        self.assertEqual(result.battery, "main status")
        self.assertEqual(list(result.batteries), ["unit-2"])
        self.assertEqual(result.batteries["unit-2"].points, {"voltage": True})

        client.get_battery_unit.side_effect = None
        client.get_battery_unit.return_value = unit
        result = await coordinator._async_update_data()
        self.assertEqual(set(result.batteries), {"unit-1", "unit-2"})

    async def test_authentication_failure_does_not_get_ignored(self):
        coordinator, _ = self.make_coordinator(
            ZinvoltAuthenticationError("expired")
        )
        with self.assertRaises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()

    async def test_status_failure_is_not_reported_as_success(self):
        coordinator, client = self.make_coordinator(None)
        client.get_observed_battery_status.side_effect = ZinvoltError("status unavailable")
        with self.assertLogs(level="WARNING"), self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()
        client.get_battery_unit.assert_not_awaited()

    async def test_recovered_unit_points_are_added_once(self):
        listeners = []
        added = []
        coordinator = SimpleNamespace(
            data=SimpleNamespace(batteries={}),
            async_add_listener=lambda listener: listeners.append(listener)
            or (lambda: None),
        )
        entry = SimpleNamespace(
            runtime_data={"battery": coordinator},
            async_on_unload=lambda unsubscribe: None,
        )
        with (
            patch.object(
                self.binary_sensor_module,
                "ZinvoltBatteryStateBinarySensor",
                side_effect=lambda *args: "main",
            ),
            patch.object(
                self.binary_sensor_module,
                "ZinvoltPointBinarySensor",
                side_effect=lambda coordinator, serial, point: (serial, point),
            ),
        ):
            await self.binary_sensor_module.async_setup_entry(None, entry, added.extend)
            self.assertEqual(added, ["main"])
            coordinator.data.batteries["unit-1"] = SimpleNamespace(
                points={"voltage": True, "unknown": True}
            )
            listeners[0]()
            listeners[0]()
        self.assertEqual(added, ["main", ("unit-1", "voltage")])

    async def test_power_entities_use_caps_and_actual_unlocked_output(self):
        state = SimpleNamespace(
            serial_number="test",
            global_settings=SimpleNamespace(max_output=700, max_output_unlocked=True),
        )
        control = SimpleNamespace(
            charge_limit=600, discharge_limit=400, local_mode=None,
            output_max=lambda state: 2000,
        )
        coordinator = SimpleNamespace(
            data=SimpleNamespace(battery=state),
            battery=SimpleNamespace(name="test"), control=control,
            async_set_power=AsyncMock(), async_set_limit=AsyncMock(),
        )
        entity = self.number_module.ZinvoltBatteryStateNumber(
            coordinator, self.number_module.NUMBERS[0]
        )
        self.assertEqual(entity.native_value, 700)
        self.assertEqual(entity.native_max_value, 400)
        await entity.async_set_native_value(300)
        coordinator.async_set_power.assert_awaited_once_with("max_output", 300)
        control.local_mode = "charge_only"
        self.assertEqual(entity.native_max_value, 0)
        limit = self.number_module.ZinvoltPowerLimit(coordinator, "charge_limit")
        await limit.async_set_native_value(500)
        coordinator.async_set_limit.assert_awaited_once_with("charge_limit", 500)

    async def test_energy_and_raw_mode_entities_expose_independent_values(self):
        coordinator = SimpleNamespace(
            data=SimpleNamespace(battery=SimpleNamespace(
                serial_number="test", smart_mode="IDLE"
            )),
            battery=SimpleNamespace(name="test"),
            control=SimpleNamespace(energy=SimpleNamespace(
                charged_total=1.2, charged_daily=.2,
                discharged_total=2.3, discharged_daily=.3,
            )),
            last_update_success=True,
        )
        values = [
            self.sensor_module.ZinvoltEnergySensor(coordinator, direction, daily).native_value
            for direction in ("charged", "discharged")
            for daily in (False, True)
        ]
        self.assertEqual(values, [1.2, .2, 2.3, .3])
        sensor = self.sensor_module.ZinvoltRawModeSensor(coordinator)
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, "IDLE")
        coordinator.last_update_success = False
        self.assertFalse(sensor.available)


if __name__ == "__main__":
    unittest.main()

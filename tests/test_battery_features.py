"""Exercise energy, persistent limits and local modes with the pinned API client."""

import copy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from zinvolt.models import CurrentPower, GlobalSettings, OnlineStatus, SmartMode


class ServiceValidationError(Exception):
    pass


class HomeAssistantError(Exception):
    pass


class MemoryStore:
    data = {}

    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, version, key, *, atomic_writes=False):
        self.key = key
        self.delay_calls = 0

    async def async_load(self):
        return copy.deepcopy(self.data.get(self.key))

    async def async_save(self, data):
        self.data[self.key] = copy.deepcopy(data)

    def async_delay_save(self, data, delay):
        self.pending = data
        self.delay_calls += 1


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


class BatteryFeaturesTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        package = module("custom_components.zinvolt_2")
        package.__path__ = [str(root / "custom_components" / "zinvolt_2")]
        cls.modules_patch = patch.dict(sys.modules, {
            "custom_components.zinvolt_2": package,
            "homeassistant": module("homeassistant"),
            "homeassistant.core": module("homeassistant.core", HomeAssistant=object),
            "homeassistant.exceptions": module(
                "homeassistant.exceptions",
                ServiceValidationError=ServiceValidationError,
                HomeAssistantError=HomeAssistantError,
            ),
            "homeassistant.helpers": module("homeassistant.helpers"),
            "homeassistant.helpers.storage": module(
                "homeassistant.helpers.storage", Store=MemoryStore
            ),
            "homeassistant.util": module("homeassistant.util"),
            "homeassistant.util.dt": module(
                "homeassistant.util.dt", get_default_time_zone=lambda: UTC
            ),
        })
        cls.modules_patch.start()
        cls.client_module = importlib.import_module("custom_components.zinvolt_2.client")
        cls.control_module = importlib.import_module("custom_components.zinvolt_2.control")
        cls.energy_module = importlib.import_module("custom_components.zinvolt_2.energy")

    @classmethod
    def tearDownClass(cls):
        for name in ("client", "control", "const", "energy"):
            sys.modules.pop(f"custom_components.zinvolt_2.{name}", None)
        cls.modules_patch.stop()

    def setUp(self):
        MemoryStore.data.clear()
        self.control = self.control_module.BatteryControl(None, "entry", "battery")
        self.client = SimpleNamespace(
            set_max_output=AsyncMock(),
            set_max_charge_power=AsyncMock(),
            set_smart_mode=AsyncMock(),
        )
        self.now = datetime(2026, 11, 24, 12, tzinfo=UTC)
        self.state = self.client_module.ObservedBatteryState(
            serial_number="test", name="battery", online_status=OnlineStatus.ONLINE,
            current_power=CurrentPower(
                online_status=OnlineStatus.ONLINE, power_socket_output=500
            ),
            smart_mode="SELF_USE",
            global_settings=GlobalSettings(
                max_output=800, max_output_limit=800, max_output_unlocked=False,
                battery_upper_threshold=100, battery_lower_threshold=10,
                maximum_charge_power=1200, standby_time=10,
            ),
        )

    async def observe(self, seconds=0):
        await self.control.async_observe(
            self.client, "battery", self.state, self.now + timedelta(seconds=seconds)
        )

    async def test_unrecognized_api_mode_remains_visible(self):
        payload = {
            "sn": "test", "name": "battery", "onlineStatus": "ONLINE",
            "currentPower": {"onlineStatus": "ONLINE", "pso": 500},
            "smartMode": "IDLE",
            "globalSettings": {
                "maxOutput": 800, "maxOutputLimit": 800,
                "maxOutputUnlocked": False, "batHighCap": 100, "batUseCap": 10,
                "maxChargePower": 1200, "standbyTime": 10,
            },
        }
        client = self.client_module.ObservedZinvoltClient()
        with patch.object(client, "_get", AsyncMock(return_value=json.dumps(payload))):
            state = await client.get_observed_battery_status("battery")
        self.assertEqual(state.smart_mode, "IDLE")
        self.assertEqual(state.current_power.power_socket_output, 500)

    async def test_inactive_restores_updated_limits_when_leaving(self):
        self.control.charge_limit = 600
        self.control.discharge_limit = 400
        await self.control.async_set_mode(
            self.client, "battery", self.state, "inactive"
        )
        self.client.set_smart_mode.assert_awaited_with("battery", SmartMode.CHARGED)
        self.client.set_max_charge_power.assert_awaited_with("battery", 0)
        self.assertEqual(self.control.local_mode, "inactive")
        restored = self.control_module.BatteryControl(None, "entry", "battery")
        await restored.async_load()
        self.assertEqual(restored.local_mode, "inactive")
        self.assertEqual(restored.charge_max, 0)
        with self.assertRaises(ServiceValidationError):
            await self.control.async_set_power(
                self.client, "battery", self.state, "max_charge_power", 100
            )
        self.state.smart_mode = "CHARGED"
        self.state.global_settings.maximum_charge_power = 0
        self.state.global_settings.max_output = 0
        await self.control.async_set_limit(
            self.client, "battery", self.state, "charge_limit", 700
        )
        await self.control.async_set_limit(
            self.client, "battery", self.state, "discharge_limit", 500
        )
        self.client.set_max_charge_power.assert_awaited_with("battery", 0)
        await self.control.async_set_mode(
            self.client, "battery", self.state, SmartMode.SELF_USE
        )
        self.client.set_max_charge_power.assert_awaited_with("battery", 700)
        self.client.set_max_output.assert_awaited_with("battery", 500)

    async def test_native_mode_switch_copies_both_limits_even_without_local_mode(self):
        self.control.charge_limit = 600
        self.control.discharge_limit = 400
        self.control.requested_output = 0
        self.state.global_settings.maximum_charge_power = 0
        self.state.global_settings.max_output = 0
        await self.control.async_set_mode(
            self.client, "battery", self.state, SmartMode.CHARGED
        )
        self.client.set_max_charge_power.assert_awaited_once_with("battery", 600)
        self.client.set_max_output.assert_awaited_once_with("battery", 400)
        self.client.set_smart_mode.assert_awaited_once_with("battery", SmartMode.CHARGED)
        self.assertIsNone(self.control.local_mode)

    async def test_inactive_sets_zero_before_selecting_charged(self):
        requests = Mock()
        requests.attach_mock(self.client.set_max_charge_power, "charge")
        requests.attach_mock(self.client.set_max_output, "output")
        requests.attach_mock(self.client.set_smart_mode, "mode")
        await self.control.async_set_mode(
            self.client, "battery", self.state, "inactive"
        )
        self.assertEqual(requests.mock_calls, [
            call.charge("battery", 0),
            call.mode("battery", SmartMode.CHARGED),
            call.output("battery", 800),
        ])

    async def test_selector_distinguishes_app_inactive_from_fast_charge(self):
        class Entity:
            def __init__(self, coordinator):
                self.coordinator = coordinator

        modules = {
            "homeassistant.components": module("homeassistant.components"),
            "homeassistant.components.select": module(
                "homeassistant.components.select", SelectEntity=object
            ),
            "homeassistant.helpers.entity_platform": module(
                "homeassistant.helpers.entity_platform",
                AddConfigEntryEntitiesCallback=object,
            ),
            "custom_components.zinvolt_2.coordinator": module(
                "custom_components.zinvolt_2.coordinator",
                ZinvoltConfigEntry=object, ZinvoltDeviceCoordinator=object,
            ),
            "custom_components.zinvolt_2.entity": module(
                "custom_components.zinvolt_2.entity", ZinvoltEntity=Entity
            ),
        }
        with patch.dict(sys.modules, modules):
            select = importlib.import_module("custom_components.zinvolt_2.select")
            coordinator = SimpleNamespace(
                control=self.control, data=SimpleNamespace(battery=self.state)
            )
            entity = select.ZinvoltBatteryMode(coordinator)
            self.state.smart_mode = "CHARGED"
            self.state.global_settings.maximum_charge_power = 0
            self.assertEqual(entity.current_option, "inactive")
            self.state.global_settings.maximum_charge_power = 600
            self.assertEqual(entity.current_option, "fast_charge")
            self.control.local_mode = "discharge_only"
            self.assertEqual(entity.current_option, "discharge_only")

    async def test_energy_direction_midnight_and_restart(self):
        energy = self.energy_module.EnergyEstimate()
        zone = timezone(timedelta(hours=1))
        start = datetime(2026, 11, 24, 22, 59, 30, tzinfo=UTC)
        energy.update(start, -1200, zone)
        energy.update(start + timedelta(seconds=60), 600, zone)
        self.assertAlmostEqual(energy.charged_total, .02)
        self.assertAlmostEqual(energy.charged_daily, .01)
        energy.update(start + timedelta(seconds=120), 600, zone)
        self.assertAlmostEqual(energy.discharged_total, .01)
        self.control.energy = energy
        await self.control.store.async_save(self.control._data())
        restored = self.control_module.BatteryControl(None, "entry", "battery")
        await restored.async_load()
        restored.energy.update(start + timedelta(hours=2), 2000, zone)
        self.assertEqual(asdict(restored.energy)["charged_total"], energy.charged_total)
        self.assertEqual(restored.energy.discharged_total, energy.discharged_total)

    async def test_energy_skips_missing_samples_outages_and_resets_day(self):
        energy = self.energy_module.EnergyEstimate()
        energy.update(self.now, 1200, UTC)
        energy.update(self.now + timedelta(seconds=30), None, UTC)
        energy.update(self.now + timedelta(seconds=60), 1200, UTC)
        energy.update(self.now + timedelta(minutes=10), 1200, UTC)
        self.assertEqual(energy.discharged_total, 0)
        energy.update(self.now + timedelta(minutes=11), 1200, UTC)
        self.assertAlmostEqual(energy.discharged_total, .02)
        energy.update(self.now + timedelta(days=1), None, UTC)
        self.assertEqual(energy.discharged_daily, 0)
        self.assertAlmostEqual(energy.discharged_total, .02)

    async def test_limits_apply_immediately_persist_and_reject_above_cap(self):
        await self.control.async_set_limit(self.client, "battery", self.state, "charge_limit", 600)
        await self.control.async_set_limit(self.client, "battery", self.state, "discharge_limit", 400)
        self.client.set_max_charge_power.assert_awaited_once_with("battery", 600)
        self.client.set_max_output.assert_awaited_once_with("battery", 400)
        with self.assertRaises(ServiceValidationError):
            await self.control.async_set_power(self.client, "battery", self.state, "max_output", 401)
        restored = self.control_module.BatteryControl(None, "entry", "battery")
        await restored.async_load()
        self.assertEqual((restored.charge_limit, restored.discharge_limit), (600, 400))
        for invalid in (float("nan"), float("inf"), -1, 2001, 1.5):
            with self.assertRaises(ServiceValidationError):
                await self.control.async_set_limit(self.client, "battery", self.state, "charge_limit", invalid)

    async def test_charge_only_blocks_output_then_restores_capped_output(self):
        await self.observe()
        await self.control.async_set_mode(self.client, "battery", self.state, "charge_only")
        self.client.set_max_output.assert_awaited_with("battery", 0)
        self.client.set_smart_mode.assert_awaited_with("battery", SmartMode.SELF_USE)
        with self.assertRaises(ServiceValidationError):
            await self.control.async_set_power(self.client, "battery", self.state, "max_output", 100)
        await self.control.async_set_limit(self.client, "battery", self.state, "discharge_limit", 400)
        self.client.set_max_output.assert_awaited_with("battery", 0)
        await self.control.async_set_mode(self.client, "battery", self.state, SmartMode.DYNAMIC)
        self.client.set_max_output.assert_awaited_with("battery", 400)
        self.assertIsNone(self.control.local_mode)

    async def test_discharge_only_pauses_and_retries_after_three_minutes(self):
        self.control.charge_limit = 600
        self.control.discharge_limit = 400
        await self.observe()
        await self.control.async_set_mode(
            self.client, "battery", self.state, "discharge_only"
        )
        self.state.current_power.power_socket_output = -500
        await self.observe()
        self.client.set_smart_mode.assert_awaited_with("battery", SmartMode.CHARGED)
        self.client.set_max_charge_power.assert_awaited_with("battery", 0)
        self.state.smart_mode = "CHARGED"
        self.state.global_settings.maximum_charge_power = 0
        self.state.global_settings.max_output = 400
        with self.assertRaises(ServiceValidationError):
            await self.control.async_set_power(
                self.client, "battery", self.state, "max_charge_power", 100
            )
        self.client.set_smart_mode.reset_mock()
        await self.observe(180)
        self.client.set_smart_mode.assert_not_awaited()
        self.assertIsNone(self.control.pause_until)
        self.assertEqual(self.control.charge_max, 0)
        self.state.current_power.power_socket_output = None
        await self.observe(210)
        self.assertIsNone(self.control.pause_until)
        self.state.current_power.power_socket_output = 25
        await self.observe(240)
        self.assertIsNone(self.control.pause_until)
        self.state.current_power.power_socket_output = 0
        await self.observe(270)
        self.assertEqual(self.control.pause_until, self.now.timestamp() + 450)
        await self.observe(449)
        self.client.set_smart_mode.assert_not_awaited()
        await self.control.async_set_limit(
            self.client, "battery", self.state, "charge_limit", 700
        )
        await self.control.async_set_limit(
            self.client, "battery", self.state, "discharge_limit", 500
        )
        self.client.set_max_charge_power.assert_awaited_with("battery", 0)
        await self.observe(450)
        self.client.set_smart_mode.assert_awaited_once_with("battery", SmartMode.SELF_USE)
        self.client.set_max_charge_power.assert_awaited_with("battery", 700)
        self.client.set_max_output.assert_awaited_with("battery", 500)
        self.state.smart_mode = "SELF_USE"
        self.state.current_power.power_socket_output = -500
        await self.observe(480)
        self.client.set_smart_mode.assert_awaited_with("battery", SmartMode.CHARGED)
        self.client.set_max_charge_power.assert_awaited_with("battery", 0)
        self.assertIsNone(self.control.pause_until)

    async def test_api_error_does_not_persist_a_successful_mode_change(self):
        from zinvolt.exceptions import ZinvoltError
        self.client.set_smart_mode.side_effect = ZinvoltError("unavailable")
        with self.assertRaises(HomeAssistantError):
            await self.control.async_set_mode(self.client, "battery", self.state, "charge_only")
        self.assertIsNone(self.control.local_mode)
        self.assertEqual(MemoryStore.data, {})

    async def test_poll_enforces_caps_and_offline_does_not_change_api(self):
        self.control.charge_limit = 600
        self.control.discharge_limit = 400
        await self.observe()
        self.client.set_max_charge_power.assert_awaited_once_with("battery", 600)
        self.client.set_max_output.assert_awaited_once_with("battery", 400)
        self.client.set_max_output.reset_mock()
        self.client.set_max_charge_power.reset_mock()
        self.control.local_mode = "discharge_only"
        self.state.current_power.online_status = OnlineStatus.OFFLINE
        self.state.current_power.power_socket_output = -500
        await self.observe(30)
        self.client.set_smart_mode.assert_not_awaited()
        self.client.set_max_output.assert_not_awaited()
        self.client.set_max_charge_power.assert_not_awaited()

    async def test_raising_limit_does_not_raise_discharge_only_output(self):
        await self.observe()
        await self.control.async_set_mode(
            self.client, "battery", self.state, "discharge_only"
        )
        await self.control.async_set_limit(self.client, "battery", self.state, "discharge_limit", 400)
        self.state.global_settings.max_output = 400
        self.client.set_max_output.reset_mock()
        await self.control.async_set_limit(self.client, "battery", self.state, "discharge_limit", 800)
        await self.observe(30)
        self.client.set_max_output.assert_not_awaited()

    async def test_mode_pause_survives_reload_and_periodic_saves_are_not_starved(self):
        self.control.local_mode = "discharge_only"
        self.control.requested_output = 800
        self.state.current_power.power_socket_output = -500
        await self.observe()
        await self.observe(30)
        self.assertEqual(self.control.store.delay_calls, 1)
        await self.control.store.async_save(self.control.store.pending())
        await self.observe(60)
        self.assertEqual(self.control.store.delay_calls, 2)
        restored = self.control_module.BatteryControl(None, "entry", "battery")
        await restored.async_load()
        self.assertEqual(restored.local_mode, "discharge_only")
        self.assertIsNone(restored.pause_until)
        self.assertEqual(restored.charge_max, 0)
        self.client.set_smart_mode.reset_mock()
        self.state.smart_mode = "CHARGED"
        await restored.async_observe(
            self.client, "battery", self.state, self.now + timedelta(seconds=300)
        )
        self.client.set_smart_mode.assert_not_awaited()
        self.state.current_power.power_socket_output = 0
        await restored.async_observe(
            self.client, "battery", self.state, self.now + timedelta(seconds=330)
        )
        self.assertEqual(restored.pause_until, self.now.timestamp() + 510)
        await restored.async_save()
        countdown = self.control_module.BatteryControl(None, "entry", "battery")
        await countdown.async_load()
        await countdown.async_observe(
            self.client, "battery", self.state, self.now + timedelta(seconds=509)
        )
        self.client.set_smart_mode.assert_not_awaited()
        await countdown.async_observe(
            self.client, "battery", self.state, self.now + timedelta(seconds=510)
        )
        self.client.set_smart_mode.assert_awaited_once_with("battery", SmartMode.SELF_USE)

    async def test_legacy_pause_waits_for_measured_zero_after_upgrade(self):
        data = self.control._data()
        data.pop("zero_power_pause")
        data.pop("retry_delay_minutes", None)
        data["local_mode"] = "discharge_only"
        data["pause_until"] = self.now.timestamp() - 1
        await self.control.store.async_save(data)
        await self.control.async_load()
        self.assertIsNone(self.control.pause_until)
        self.assertEqual(self.control.retry_delay_minutes, 3)
        self.state.smart_mode = "CHARGED"
        self.state.current_power.power_socket_output = -500
        await self.observe()
        self.client.set_smart_mode.assert_not_awaited()
        self.assertEqual(self.control.charge_max, 0)

    async def test_retry_delay_persists_and_only_changes_future_countdowns(self):
        self.control.local_mode = "discharge_only"
        self.state.current_power.power_socket_output = -500
        await self.observe()
        await self.control.async_set_retry_delay(2)
        self.state.smart_mode = "CHARGED"
        self.state.current_power.power_socket_output = 0
        await self.observe(30)
        self.assertEqual(self.control.pause_until, self.now.timestamp() + 150)
        await self.control.async_set_retry_delay(5)
        restored = self.control_module.BatteryControl(None, "entry", "battery")
        await restored.async_load()
        self.assertEqual(restored.retry_delay_minutes, 5)
        self.assertEqual(restored.pause_until, self.now.timestamp() + 150)
        self.control = restored
        self.client.set_smart_mode.reset_mock()
        await self.observe(149)
        self.client.set_smart_mode.assert_not_awaited()
        await self.observe(150)
        self.client.set_smart_mode.assert_awaited_once_with("battery", SmartMode.SELF_USE)
        self.state.smart_mode = "SELF_USE"
        self.state.current_power.power_socket_output = -500
        await self.observe(180)
        self.state.smart_mode = "CHARGED"
        self.state.current_power.power_socket_output = 0
        await self.observe(210)
        self.assertEqual(self.control.pause_until, self.now.timestamp() + 510)

    async def test_retry_delay_validates_whole_minute_bounds(self):
        for invalid in (0, 61, -1, 1.5, float("nan"), float("inf")):
            with self.assertRaises(ServiceValidationError):
                await self.control.async_set_retry_delay(invalid)
        self.assertEqual(self.control.retry_delay_minutes, 3)
        self.assertEqual(MemoryStore.data, {})
        for valid in (1, 60):
            await self.control.async_set_retry_delay(valid)
            self.assertEqual(self.control.retry_delay_minutes, valid)
        self.client.set_smart_mode.assert_not_awaited()
        self.client.set_max_charge_power.assert_not_awaited()
        self.client.set_max_output.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

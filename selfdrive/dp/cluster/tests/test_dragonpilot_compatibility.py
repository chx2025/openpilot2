from __future__ import annotations

import ast
import contextlib
import io
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import Mock, patch


CLUSTER_DIR = Path(__file__).resolve().parents[1]
ROOT = CLUSTER_DIR.parents[2]
sys.path.insert(0, str(CLUSTER_DIR))

from cluster_live import LIVE_SERVICES_BASE, resolve_live_services
from cluster_paths import CEREAL_DIR, OPENPILOT_PACKAGE_ROOT
from cluster_route_replay import RouteLogParser, frame_to_state, load_openpilot_log_schema


class DragonpilotCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_openpilot_log_schema()

    def test_flat_checkout_resource_paths(self):
        self.assertEqual(OPENPILOT_PACKAGE_ROOT, ROOT)
        self.assertEqual(CEREAL_DIR, ROOT / "cereal")

    def test_live_services_resolve_against_destination_registry(self):
        registry = runpy.run_path(str(CEREAL_DIR / "services.py"))["SERVICE_LIST"]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            services = resolve_live_services(LIVE_SERVICES_BASE, registry)
        for service in ("carState", "modelV2", "radarState", "liveTracks", "liveDelay",
                        "liveParameters", "liveTorqueParameters", "carControl"):
            self.assertIn(service, services)
        self.assertNotIn("longitudinalPlanSP", services)
        self.assertIn("longitudinalPlanSP not available; skipping", output.getvalue())

    def test_destination_vehicle_and_control_messages(self):
        parser = RouteLogParser()
        car_state = self.schema.Event.new_message(carState={
            "vEgo": 10.0, "vEgoCluster": 10.0, "gearShifter": "drive",
            "leftBlinker": True, "cruiseState": {"enabled": True, "available": True, "speed": 20.0},
        }).carState
        parser._update_car_control(self.schema.Event.new_message(carControl={"latActive": True}).carControl)
        parser._update_selfdrive_state(self.schema.Event.new_message(selfdriveState={
            "experimentalMode": True, "alertHudVisual": "steerRequired", "alertSize": "mid",
        }).selfdriveState)
        state = frame_to_state(parser._frame_from_car_state(car_state, 100.0))
        self.assertAlmostEqual(state.speed_kph, 36.0)
        self.assertAlmostEqual(state.cruise_kph, 72.0)
        self.assertEqual(state.gear_text, "D")
        self.assertTrue(state.left_signal)
        self.assertTrue(state.lfa_active)
        self.assertTrue(state.experimental_mode)
        self.assertTrue(state.wheel_critical)

    def test_destination_radar_lead_status(self):
        parser = RouteLogParser()
        radar = self.schema.Event.new_message(radarState={
            "leadOne": {"status": True, "dRel": 25.0, "yRel": 1.0, "vRel": -2.0, "vLead": 8.0},
        }).radarState
        parser._update_radar_state(radar, 100.0)
        car_state = self.schema.Event.new_message(carState={"vEgo": 10.0}).carState
        state = frame_to_state(parser._frame_from_car_state(car_state, 100.0))
        target = next(vehicle for vehicle in state.detected_vehicles if vehicle.label == "TARGET")
        self.assertEqual(target.longitudinal_m, 25.0)
        self.assertEqual(target.relative_speed_mps, -2.0)
        self.assertAlmostEqual(target.absolute_speed_kph, 28.8)

    def test_destination_accelerator_schema(self):
        event = self.schema.Event.new_message(modelExt={"bigModel": True, "acceleratorState": "running"})
        self.assertTrue(event.modelExt.bigModel)
        self.assertEqual(str(event.modelExt.acceleratorState), "running")

    def test_usb_pacing_uses_destination_runtime_params(self):
        tree = ast.parse((CLUSTER_DIR / "cluster_usb_display.py").read_text())
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_usbgpu_transfer_active")
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "cluster_usb_display.py", "exec"), namespace)
        params_module = Mock()
        params = params_module.Params.return_value
        for loading, running, expected in ((False, False, False), (True, False, True), (False, True, True)):
            with self.subTest(loading=loading, running=running):
                flags = {"dp_egpu_loading": loading, "dp_egpu_running": running}
                params.get_bool.side_effect = flags.__getitem__
                with patch.dict(sys.modules, {"openpilot.common.params": params_module}):
                    self.assertEqual(namespace["_usbgpu_transfer_active"](), expected)


if __name__ == "__main__":
    unittest.main()

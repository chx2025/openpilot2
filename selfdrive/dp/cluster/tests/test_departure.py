from __future__ import annotations

import ast
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


CLUSTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLUSTER_DIR))

from cluster_departure import DepartureReminder


def load_method(file_name, method_name, namespace):
    tree = ast.parse((CLUSTER_DIR / file_name).read_text(encoding="utf-8"))
    method = next(
        node for cls in tree.body if isinstance(cls, ast.ClassDef) for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == method_name
    )
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(CLUSTER_DIR / file_name), "exec"), namespace)
    return namespace[method_name]


class DepartureTests(unittest.TestCase):
    def setUp(self):
        self.reminder = DepartureReminder()

    def update(self, t, **overrides):
        args = {"allowed": True, "valid": True, "sample_time": t, "lead_present": True,
                "lead_distance": 5.0, "lead_relative_speed": 0.0}
        args.update(overrides)
        return self.reminder.update(t, **args)

    def arm(self, start=10.0):
        for i in range(6):
            self.assertFalse(self.update(start + i * 0.2))

    def trigger(self, start=10.0):
        self.arm(start)
        self.assertFalse(self.update(start + 1.2, lead_distance=6.2, lead_relative_speed=1.0))
        self.assertFalse(self.update(start + 1.4, lead_distance=6.4, lead_relative_speed=1.0))
        self.assertTrue(self.update(start + 1.6, lead_distance=6.6, lead_relative_speed=1.0))

    def test_confirmation_and_exact_distance_threshold(self):
        self.arm()
        self.assertFalse(self.update(11.2, lead_distance=6.0, lead_relative_speed=1.0))
        self.assertFalse(self.update(11.4, lead_distance=6.1, lead_relative_speed=1.0))
        self.assertFalse(self.update(11.6, lead_distance=6.3, lead_relative_speed=1.0))
        self.assertTrue(self.update(11.8, lead_distance=6.5, lead_relative_speed=1.0))

    def test_repeated_sample_cannot_confirm(self):
        self.arm()
        self.assertFalse(self.update(11.2, lead_distance=6.2, lead_relative_speed=1.0))
        self.assertFalse(self.update(11.6, sample_time=11.2, lead_distance=6.2, lead_relative_speed=1.0))
        self.assertFalse(self.update(11.8, sample_time=11.2, lead_distance=6.2, lead_relative_speed=1.0))

    def test_sample_gap_and_backward_time_restart_confirmation(self):
        self.arm()
        self.assertFalse(self.update(12.0, lead_distance=6.2, lead_relative_speed=1.0))
        self.assertFalse(self.update(11.8, lead_distance=6.4, lead_relative_speed=1.0))
        self.assertFalse(self.update(12.0, lead_distance=6.6, lead_relative_speed=1.0))
        self.assertFalse(self.update(12.2, lead_distance=6.8, lead_relative_speed=1.0))

    def test_invalid_inputs_never_trigger(self):
        cases = [
            {"valid": False},
            {"sample_time": 9.0},
            {"sample_time": 11.0},
            {"lead_present": False},
            {"lead_distance": None},
            {"lead_distance": 0.0},
            {"lead_distance": float("nan")},
            {"lead_distance": float("inf")},
            {"lead_relative_speed": None},
            {"lead_relative_speed": float("nan")},
        ]
        for args in cases:
            with self.subTest(args=args):
                self.reminder = DepartureReminder()
                self.assertFalse(self.update(10.0, **args))
                self.assertFalse(self.update(10.4, **args))

    def test_single_trigger_per_stop_and_display_expiry(self):
        self.trigger()
        for i in range(1, 16):
            self.update(11.6 + i * 0.2)
        self.assertFalse(self.update(14.8))
        self.assertFalse(self.update(15.0))
        self.assertFalse(self.update(15.2, allowed=False))
        self.trigger(16.0)

    def test_stationary_or_approaching_lead_cannot_trigger(self):
        self.arm()
        for i in range(1, 8):
            self.assertFalse(self.update(11.0 + i * 0.2, lead_distance=7.0))
        self.assertFalse(self.update(12.6, lead_distance=7.0, lead_relative_speed=-1.0))

    def test_no_close_lead_or_already_departing_lead_cannot_arm(self):
        for overrides in ({"lead_distance": 8.0}, {"lead_relative_speed": 1.0}, {"lead_present": False}):
            self.setUp()
            for i in range(10):
                self.assertFalse(self.update(10.0 + i * 0.2, **overrides))

    def test_disappearing_lead_requires_rearming(self):
        self.arm()
        self.assertFalse(self.update(11.2, lead_present=False))
        self.assertFalse(self.update(11.4, lead_distance=7.0, lead_relative_speed=1.0))
        self.assertFalse(self.update(11.8, lead_distance=8.0, lead_relative_speed=1.0))

    def test_interrupted_departure_restarts_confirmation(self):
        self.arm()
        self.assertFalse(self.update(11.2, lead_distance=6.5, lead_relative_speed=1.0))
        self.assertFalse(self.update(11.4, lead_distance=6.5, lead_relative_speed=0.0))
        self.assertFalse(self.update(11.6, lead_distance=6.7, lead_relative_speed=1.0))
        self.assertFalse(self.update(11.8, lead_distance=6.9, lead_relative_speed=1.0))
        self.assertTrue(self.update(12.0, lead_distance=7.1, lead_relative_speed=1.0))

    def test_invalid_data_hides_without_repeated_alert(self):
        self.trigger()
        self.assertFalse(self.update(11.8, valid=False))
        self.assertFalse(self.update(12.0))
        self.assertFalse(self.update(12.4))


class LiveDepartureTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.method = load_method(
            "cluster_live.py",
            "_update_departure_reminder",
            {
                "time": SimpleNamespace(monotonic=lambda: self.now),
                "safe_optional_float": lambda obj, name: getattr(obj, name, None),
                "DEPARTURE_MAX_SAMPLE_GAP_SECONDS": 0.5,
            },
        )
        self.sm = {
            "carState": SimpleNamespace(vEgo=0.0, gasPressed=False, cruiseState=SimpleNamespace(enabled=False)),
            "selfdriveState": SimpleNamespace(enabled=False),
            "radarState": SimpleNamespace(leadOne=SimpleNamespace(present=True, dRel=5.0, vRel=0.0), radarErrors=[]),
        }

        class SubMaster(dict):
            pass

        self.sm = SubMaster(self.sm)
        self.sm.valid = dict.fromkeys(self.sm, True)
        self.sm.logMonoTime = dict.fromkeys(self.sm, int(self.now * 1e9))
        self.source = SimpleNamespace(
            sm=self.sm,
            _service_alive=Mock(return_value=True),
            _service_valid=Mock(return_value=True),
            _departure_reminder=DepartureReminder(),
            vehicle_started=Mock(return_value=True),
        )
        self.state = SimpleNamespace(gear_text="D")

    def sample(self, t):
        self.now = t
        self.sm.logMonoTime = dict.fromkeys(self.sm, int(t * 1e9))
        return self.method(self.source, self.state)

    def trigger(self, start=10.0):
        lead = self.sm["radarState"].leadOne
        lead.dRel, lead.vRel = 5.0, 0.0
        for i in range(6):
            self.assertFalse(self.sample(start + i * 0.2))
        lead.dRel, lead.vRel = 6.5, 1.0
        self.assertFalse(self.sample(start + 1.2))
        self.assertTrue(self.sample(start + 1.6))

    def test_live_trigger_without_model_or_driver_monitoring(self):
        self.trigger()

    def test_disallowed_vehicle_states(self):
        for attribute, value in (("gear_text", "R"), ("gear_text", "P"), ("gear_text", "N")):
            setattr(self.state, attribute, value)
            self.assertFalse(self.sample(10.0))
            self.assertFalse(self.sample(10.4))
        self.state.gear_text = "D"
        car = self.sm["carState"]
        for speed in (0.1, -0.1, 1.0, float("nan")):
            car.vEgo = speed
            self.assertFalse(self.sample(10.0))
            self.assertFalse(self.sample(10.4))
        car.vEgo = 0.0
        for obj, name in ((car, "gasPressed"), (car.cruiseState, "enabled"), (self.sm["selfdriveState"], "enabled")):
            setattr(obj, name, True)
            self.assertFalse(self.sample(10.0))
            self.assertFalse(self.sample(10.4))
            setattr(obj, name, False)
        self.source.vehicle_started.return_value = False
        self.assertFalse(self.sample(10.0))
        self.assertFalse(self.sample(10.4))

    def test_stale_invalid_and_missing_timestamps_clear_banner(self):
        self.trigger()
        self.sm.valid["radarState"] = False
        self.assertFalse(self.method(self.source, self.state))
        self.sm.valid["radarState"] = True
        self.sm.logMonoTime.pop("carState")
        self.assertFalse(self.method(self.source, self.state))

    def test_offroad_resets_even_with_stale_radar(self):
        self.trigger()
        self.now = 12.0
        self.source.vehicle_started.return_value = False
        self.assertFalse(self.method(self.source, self.state))
        self.source.vehicle_started.return_value = True
        self.trigger(12.2)

    def test_radar_errors_clear_banner(self):
        self.trigger()
        self.sm["radarState"].radarErrors = ["fault"]
        self.assertFalse(self.method(self.source, self.state))

    def test_each_required_service_must_be_fresh_alive_and_valid(self):
        for service in self.sm:
            for failure in ("stale", "invalid", "dead"):
                with self.subTest(service=service, failure=failure):
                    self.setUp()
                    self.sample(10.0)
                    self.now = 10.4
                    self.sm.logMonoTime = dict.fromkeys(self.sm, int(self.now * 1e9))
                    if failure == "stale":
                        self.sm.logMonoTime[service] = int(9.0 * 1e9)
                    elif failure == "invalid":
                        self.sm.valid[service] = False
                    else:
                        self.source._service_alive.side_effect = lambda name, service=service: name != service
                    self.assertFalse(self.method(self.source, self.state))


class DepartureRenderingTests(unittest.TestCase):
    def test_banner_and_critical_alert_precedence(self):
        draw = load_method(
            "cluster_renderer.py",
            "_draw_departure_reminder",
            {
                "DESIGN_WIDTH": 1920,
                "DESIGN_HEIGHT": 720,
                "TOP_STATUS_DETAIL_CENTER_Y": 162.0,
                "TOP_STATUS_DETAIL_FONT_SIZE": 34.0,
                "GREEN": (0, 255, 0),
                "WHITE": (255, 255, 255),
            },
        )
        hud = SimpleNamespace(_rounded_rect=Mock(), _draw_text=Mock())
        for show, critical in ((False, False), (True, True)):
            draw(hud, SimpleNamespace(departure_reminder=show, wheel_critical=critical))
            hud._draw_text.assert_not_called()
        draw(hud, SimpleNamespace(departure_reminder=True, wheel_critical=False))
        self.assertEqual(hud._draw_text.call_args.args[0], "READY TO GO")
        self.assertEqual(hud._rounded_rect.call_args.args[:4], (750.0, 195.0, 420.0, 64.0))
        self.assertEqual(hud._draw_text.call_args.args[1:3], (960.0, 227.0))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import ast
import math
from pathlib import Path
import sys
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


CLUSTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLUSTER_DIR))

from cluster_config import AMBER, DESIGN_HEIGHT, DESIGN_WIDTH, WHITE
from cluster_system_monitor import CHESTNUT_ACTIVE, CHESTNUT_FAILED, CHESTNUT_LOADING, SystemStatsSampler


def renderer_methods():
    # Exercise the production HUD methods without requiring a GPU/raylib context.
    tree = ast.parse((CLUSTER_DIR / "cluster_renderer.py").read_text(encoding="utf-8"))
    names = {"_draw_model_label", "_draw_acc_status_icon", "_draw_lfa_status_icon"}
    methods = [node for cls in tree.body if isinstance(cls, ast.ClassDef) for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assignments = {node.targets[0].id: node for node in tree.body if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    namespace = {
        "math": math,
        "time": time,
        "rl": Mock(),
        "DESIGN_WIDTH": DESIGN_WIDTH,
        "DESIGN_HEIGHT": DESIGN_HEIGHT,
        "AMBER": AMBER,
        "WHITE": WHITE,
    }

    def load_constant(name):
        if name in namespace or name not in assignments:
            return
        node = assignments[name]
        for child in ast.walk(node.value):
            if isinstance(child, ast.Name):
                load_constant(child.id)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(CLUSTER_DIR / "cluster_renderer.py"), "exec"), namespace)

    for method in methods:
        for node in ast.walk(method):
            if isinstance(node, ast.Name) and node.id.isupper():
                load_constant(node.id)
    cls = ast.ClassDef(name="Hud", bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(CLUSTER_DIR / "cluster_renderer.py"), "exec"), namespace)
    return namespace


class ModelNameTests(unittest.TestCase):
    def setUp(self):
        self.sampler = SystemStatsSampler()
        self.params = Mock()
        self.params.get.return_value = {"short_name": "TT", "display_name": "Tee Time"}
        self.sampler._params = self.params

    def test_inactive_states_do_not_claim_small_model(self):
        for state, label in ((None, "model idle"), (CHESTNUT_LOADING, "loading"), (CHESTNUT_FAILED, "model failed")):
            with self.subTest(state=state):
                self.assertEqual(self.sampler._read_model_name(state), label)
        self.params.get.assert_not_called()

    def test_confirmed_small_output_overrides_failed_icon(self):
        self.sampler._small_model_active = True
        self.assertEqual(self.sampler._read_model_name(CHESTNUT_FAILED), "stock")

    def test_active_uses_dragonpilot_bundle(self):
        self.assertEqual(self.sampler._read_model_name(CHESTNUT_ACTIVE), "TT")
        self.params.get.assert_called_once_with("dp_egpu_bundle")

    def test_active_display_name(self):
        self.params.get.return_value = {"display_name": "Tee Time"}
        self.assertEqual(self.sampler._read_model_name(CHESTNUT_ACTIVE), "Tee Time")

    def test_missing_bundle_is_explicit(self):
        self.params.get.return_value = None
        with self.assertLogs("cluster_system_monitor", level="ERROR"):
            self.assertEqual(self.sampler._read_model_name(CHESTNUT_ACTIVE), "model unknown")

    def test_disconnected_with_stale_active_param(self):
        self.assertEqual(self.sampler._read_model_name(CHESTNUT_FAILED), "model failed")
        self.params.get.assert_not_called()

    def test_read_error_is_explicit(self):
        self.params.get.side_effect = RuntimeError("params read failed")
        with self.assertLogs("cluster_system_monitor", level="ERROR"):
            self.assertEqual(self.sampler._read_model_name(CHESTNUT_ACTIVE), "model unknown")

    def test_sampler_publishes_name_with_matching_state(self):
        self.sampler._read_linux_memory = Mock(return_value=(None, None, None))
        self.sampler._read_linux_temperature = Mock(return_value=None)
        self.sampler._read_linux_cpu_times = Mock(return_value=None)
        self.sampler._read_chestnut_state = Mock(return_value=CHESTNUT_ACTIVE)
        with patch("cluster_system_monitor.PROC_STAT_PATH", Mock(exists=Mock(return_value=True))):
            stats = self.sampler._sample_linux()
        self.assertEqual(stats.chestnut_state, CHESTNUT_ACTIVE)
        self.assertEqual(stats.model_name, "TT")


class ChestnutHealthTests(unittest.TestCase):
    def setUp(self):
        self.sampler = SystemStatsSampler()
        self.flags = {"dp_egpu_enabled": True, "dp_egpu_present": True,
                      "dp_egpu_loading": False, "dp_egpu_running": True, "dp_egpu_failed": False}
        self.params = Mock()
        def read_bool(key):
            if key not in self.flags:
                raise ValueError(f"Unexpected param key: {key}")
            return self.flags[key]
        self.params.get_bool.side_effect = read_bool
        self.sampler._params = self.params
        self.device = SimpleNamespace(started=True)
        self.model = SimpleNamespace(bigModel=True)
        self.sm = SimpleNamespace(
            alive={"deviceState": True, "modelV2": True, "modelExt": True},
            valid={"deviceState": True, "modelV2": True, "modelExt": True},
            updated={"deviceState": True, "modelV2": True, "modelExt": True},
            recv_frame={"deviceState": 10, "modelV2": 10, "modelExt": 10},
            update=Mock(),
        )

        class Messages:
            def __init__(messages, owner):
                messages.__dict__.update(owner.sm.__dict__)
                messages.owner = owner

            def __getitem__(messages, key):
                return messages.owner.device if key == "deviceState" else messages.owner.model

        self.sm = Messages(self)
        self.sampler._chestnut_sm = self.sm

    def test_healthy_big_model_is_active(self):
        self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_ACTIVE)

    def test_small_model_requires_valid_live_publications(self):
        self.model.bigModel = False
        self.flags["dp_egpu_running"] = False
        self.sampler._read_chestnut_state()
        self.assertEqual(self.sampler._read_model_name(None), "stock")
        for service in ("modelV2", "modelExt", "deviceState"):
            for field in ("alive", "valid"):
                with self.subTest(service=service, field=field):
                    getattr(self.sm, field)[service] = False
                    state = self.sampler._read_chestnut_state()
                    self.assertNotEqual(self.sampler._read_model_name(state), "stock")
                    getattr(self.sm, field)[service] = True

    def test_stale_active_cannot_hide_runtime_failures(self):
        for failure in ("disconnected", "device_stale", "model_stale", "extension_stale", "small_model"):
            with self.subTest(failure=failure):
                self.setUp()
                self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_ACTIVE)
                if failure == "disconnected":
                    self.flags["dp_egpu_present"] = False
                elif failure == "device_stale":
                    self.sm.alive["deviceState"] = False
                elif failure == "model_stale":
                    self.sm.alive["modelV2"] = False
                elif failure == "extension_stale":
                    self.sm.alive["modelExt"] = False
                else:
                    self.model.bigModel = False
                self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_FAILED)

    def test_failure_is_latched_until_offroad(self):
        self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_ACTIVE)
        self.model.bigModel = False
        self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_FAILED)
        self.model.bigModel = True
        self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_FAILED)
        self.device.started = False
        self.assertIsNone(self.sampler._read_chestnut_state())
        self.device.started = True
        self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_ACTIVE)

    def test_startup_waits_for_device_and_model(self):
        self.sm.recv_frame["deviceState"] = 0
        self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_LOADING)
        self.sm.recv_frame["deviceState"] = 10
        self.sm.updated["modelExt"] = False
        self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_LOADING)
        self.sm.updated["modelExt"] = True
        self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_ACTIVE)

    def test_param_failure_overrides_loading(self):
        self.flags["dp_egpu_loading"] = True
        self.flags["dp_egpu_failed"] = True
        self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_FAILED)

    def test_offroad_disconnected_is_inactive(self):
        self.device.started = False
        self.flags["dp_egpu_present"] = False
        self.assertIsNone(self.sampler._read_chestnut_state())

    def test_read_failure_is_logged_and_not_active(self):
        self.sm.update.side_effect = RuntimeError("messaging failed")
        with self.assertLogs("cluster_system_monitor", level="ERROR"):
            self.assertIsNone(self.sampler._read_chestnut_state())

    def test_model_validity_does_not_latch_gpu_failure(self):
        self.sm.valid["modelV2"] = False
        self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_ACTIVE)
        self.sm.valid["modelV2"] = True
        self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_ACTIVE)

    def test_present_but_disabled_or_stock_route_is_inactive(self):
        for flags in ({"dp_egpu_enabled": False, "dp_egpu_running": False},
                      {"dp_egpu_running": False}):
            with self.subTest(flags=flags):
                self.setUp()
                self.flags.update(flags)
                self.assertIsNone(self.sampler._read_chestnut_state())

    def test_initializes_messaging_from_cereal_package(self):
        self.sampler._chestnut_sm = None
        cereal = ModuleType("cereal")
        cereal.messaging = SimpleNamespace(SubMaster=Mock(return_value=self.sm))
        with patch.dict(sys.modules, {"cereal": cereal}):
            self.assertEqual(self.sampler._read_chestnut_state(), CHESTNUT_ACTIVE)
        cereal.messaging.SubMaster.assert_called_once_with(["deviceState", "modelV2", "modelExt"])


class StatusLabelTests(unittest.TestCase):
    def setUp(self):
        self.ns = renderer_methods()
        self.hud = self.ns["Hud"]()
        self.hud.width, self.hud.height = 960, 360
        self.hud._compact_panel = False
        self.hud._model_label_text = ""
        self.hud._model_label_started_at = 0.0
        self.hud._measure_text = Mock(return_value=(50.0, 30.0))
        self.hud._draw_text = Mock()
        self.hud._current_theme = Mock(return_value=SimpleNamespace(text=(255, 255, 255), muted=(100, 100, 100)))
        self.rl = self.ns["rl"]

    def draw_name(self, text, now):
        with patch.object(time, "monotonic", return_value=now):
            self.hud._draw_model_label(text)
        return self.hud._draw_text.call_args

    def test_short_name_centered_without_scissor(self):
        for width in (50.0, self.ns["MODEL_LABEL_MAX_WIDTH"]):
            self.hud._measure_text.return_value = (width, 30.0)
            call = self.draw_name("TEE Time", 10.0)
            self.assertEqual(call.kwargs["anchor"], "center")
            self.assertEqual(call.args[2], self.ns["TOP_STATUS_DETAIL_CENTER_Y"])
            self.assertEqual(call.args[3], self.ns["TOP_STATUS_LABEL_FONT_SIZE"])
        self.rl.begin_scissor_mode.assert_not_called()

    def test_long_name_pauses_scrolls_and_returns(self):
        width = self.ns["MODEL_LABEL_MAX_WIDTH"]
        speed = self.ns["MODEL_LABEL_SCROLL_SPEED"]
        pause = self.ns["MODEL_LABEL_SCROLL_PAUSE_S"]
        self.hud._measure_text.return_value = (width + 96.0, 30.0)
        start = self.draw_name("Long model name", 10.0).args[1]
        travel = 96.0 / speed
        for elapsed, offset in (
            (pause / 2, 0),
            (pause + travel / 2, 48),
            (pause + travel + pause / 2, 96),
            (2 * pause + 1.5 * travel, 48),
            (2 * (pause + travel), 0),
        ):
            with self.subTest(elapsed=elapsed):
                self.assertAlmostEqual(self.draw_name("Long model name", 10.0 + elapsed).args[1], start - offset)
        self.assertEqual(self.rl.begin_scissor_mode.call_count, self.rl.end_scissor_mode.call_count)

    def test_name_change_resets_scroll(self):
        self.hud._measure_text.return_value = (300.0, 30.0)
        start = self.draw_name("First long name", 10.0).args[1]
        self.assertLess(self.draw_name("First long name", 13.0).args[1], start)
        self.assertEqual(self.draw_name("Second long name", 13.0).args[1], start)

    def test_scroll_is_independent_of_frame_count(self):
        self.hud._measure_text.return_value = (300.0, 30.0)
        self.draw_name("Long model name", 10.0)
        x_without_frames = self.draw_name("Long model name", 13.0).args[1]
        for now in (10.5, 11.0, 11.5, 12.0, 12.5):
            self.draw_name("Long model name", now)
        self.assertEqual(self.draw_name("Long model name", 13.0).args[1], x_without_frames)

    def test_scissor_uses_output_pixels_and_releases_on_error(self):
        self.hud._measure_text.return_value = (300.0, 30.0)
        self.hud._draw_text.side_effect = RuntimeError("draw failed")
        with self.assertRaises(RuntimeError):
            self.draw_name("Long model name", 10.0)
        x, y, width, height = self.rl.begin_scissor_mode.call_args.args
        left = (self.ns["CHESTNUT_ICON_CENTER_X"] - self.ns["MODEL_LABEL_MAX_WIDTH"] / 2) / 2
        right = (self.ns["CHESTNUT_ICON_CENTER_X"] + self.ns["MODEL_LABEL_MAX_WIDTH"] / 2) / 2
        top = (self.ns["TOP_STATUS_DETAIL_CENTER_Y"] - 15) / 2
        bottom = (self.ns["TOP_STATUS_DETAIL_CENTER_Y"] + 15 + self.ns["TOP_STATUS_LABEL_FONT_SIZE"] * self.ns["TEXT_VERTICAL_CENTER_OFFSET_RATIO"]) / 2
        self.assertEqual(x, math.floor(left))
        self.assertEqual(y, math.floor(top))
        self.assertEqual(width, math.ceil(right) - math.floor(left))
        self.assertEqual(height, math.ceil(bottom) - math.floor(top))
        self.rl.end_scissor_mode.assert_called_once()

    def test_compact_model_clip_tracks_top_row_at_native_and_scaled_resolution(self):
        self.hud._compact_panel = True
        self.hud._measure_text.return_value = (300.0, 30.0)
        for width, height in ((1920, 462), (960, 231)):
            with self.subTest(width=width, height=height):
                self.hud.width, self.hud.height = width, height
                self.draw_name("Long model name", 10.0)
                x, y, w, h = self.rl.begin_scissor_mode.call_args.args
                scale = self.ns["COMPACT_TOP_STATUS_SCALE"]
                center = 960 + (self.ns["CHESTNUT_ICON_CENTER_X"] - 960) * scale
                half_width = self.ns["MODEL_LABEL_MAX_WIDTH"] * scale / 2
                top = self.ns["COMPACT_TOP_STATUS_Y"] + (self.ns["TOP_STATUS_DETAIL_CENTER_Y"] - 15) * scale
                bottom = top + (30 + self.ns["TOP_STATUS_LABEL_FONT_SIZE"] * self.ns["TEXT_VERTICAL_CENTER_OFFSET_RATIO"]) * scale
                self.assertEqual(x, math.floor((center - half_width) * width / 1920))
                self.assertEqual(w, math.ceil((center + half_width) * width / 1920) - x)
                self.assertEqual(y, math.floor(top * height / 462))
                self.assertEqual(h, math.ceil(bottom * height / 462) - y)

    def test_compact_short_name_remains_centered_beneath_gpu(self):
        self.hud._compact_panel = True
        for name in ("stock", "TT", "loading"):
            call = self.draw_name(name, 10.)
            self.assertEqual(call.args[1], self.ns["CHESTNUT_ICON_CENTER_X"])
            self.assertEqual(call.kwargs["anchor"], "center")
        self.rl.begin_scissor_mode.assert_not_called()

    def test_acc_speed_and_placeholder_share_size_and_height(self):
        self.hud._b_gear_standby = Mock(return_value=False)
        self.hud._top_row_tint = Mock(return_value=(0, 255, 0))
        self.hud._cruise_set_color = Mock(return_value=(0, 255, 0))
        self.hud._acc_status_texture = SimpleNamespace(width=146, height=128)
        self.hud._draw_bottom_aligned_texture_icon = Mock(return_value=True)
        for speed in ("--", "100"):
            self.hud._cruise_set_speed_text = Mock(return_value=speed)
            self.hud._draw_acc_status_icon(SimpleNamespace())
            draw_icon_args = self.hud._draw_bottom_aligned_texture_icon.call_args.args
            expected_height = self.ns["LFA_STATUS_ICON_SIZE"]
            expected_width = expected_height * 146 / 128
            self.assertEqual(draw_icon_args[4], expected_height)
            self.assertAlmostEqual(draw_icon_args[3], expected_width)
            args = self.hud._draw_text.call_args.args
            self.assertEqual(args[2], self.ns["TOP_STATUS_DETAIL_CENTER_Y"])
            self.assertEqual(args[3], self.ns["TOP_STATUS_LABEL_FONT_SIZE"])

    def test_lfa_alignment_and_transition_clearance(self):
        self.hud._lfa_texture = object()
        self.hud._top_row_tint = Mock(return_value=(0, 255, 0))
        self.hud._draw_bottom_aligned_texture_icon = Mock(return_value=True)
        self.hud._draw_steering_angle_text = Mock()
        state = SimpleNamespace(wheel_critical=False, steering_angle_deg=10.0)
        for settle in (1.0, 1.05):
            self.hud._acc_layout_settle_scale = settle
            for progress in (0.0, 0.25, 0.5, 0.75, 1.0):
                with self.subTest(settle=settle, progress=progress):
                    self.hud._draw_lfa_status_icon(state, progress)
                    bottom = self.hud._draw_bottom_aligned_texture_icon.call_args.args[2]
                    _, _, _, center_y, size = self.hud._draw_steering_angle_text.call_args.args
                    self.assertGreater(center_y - size * 0.5, bottom)
                    if progress == 1.0:
                        self.assertAlmostEqual(center_y, self.ns["TOP_STATUS_DETAIL_CENTER_Y"])
                        self.assertEqual(size, self.ns["TOP_STATUS_LABEL_FONT_SIZE"])


if __name__ == "__main__":
    unittest.main()

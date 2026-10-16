from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


CLUSTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLUSTER_DIR))

from cluster_config import CLUSTER_FIXED_FPS, normalize_cluster_live_fps
from cluster_fps import AdaptiveFpsController


def main_namespace():
    tree = ast.parse((CLUSTER_DIR / "main.py").read_text(encoding="utf-8"))
    names = {
        "DEFAULT_FPS",
        "OFFROAD_RENDER_FPS",
        "H264_AUTO_BITRATE_BITS_PER_FPS",
        "H264_AUTO_BITRATE_MIN_BPS",
        "H264_AUTO_BITRATE_MAX_BPS",
        "resolved_usb_display_fps",
        "resolved_h264_encoder_fps",
        "resolved_usb_h264_bitrate",
    }
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names:
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
    namespace = {"CLUSTER_FIXED_FPS": CLUSTER_FIXED_FPS}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "main.py", "exec"), namespace)
    return namespace


class FpsTests(unittest.TestCase):
    def test_adaptive_fps_reduces_on_drops_and_recovers_after_stable_period(self):
        controller = AdaptiveFpsController(30.0)

        self.assertTrue(controller.update(dropped=True, now=0.0))
        self.assertEqual(controller.current_fps, 25.0)
        self.assertFalse(controller.update(dropped=True, now=1.0))
        self.assertTrue(controller.update(dropped=True, now=2.0))
        self.assertEqual(controller.current_fps, 20.0)
        self.assertFalse(controller.update(dropped=False, now=11.9))
        self.assertTrue(controller.update(dropped=False, now=12.0))
        self.assertEqual(controller.current_fps, 21.0)
        self.assertFalse(controller.update(dropped=False, now=16.9))
        self.assertTrue(controller.update(dropped=False, now=17.0))
        self.assertEqual(controller.current_fps, 22.0)

    def test_adaptive_fps_obeys_minimum_and_configured_maximum(self):
        controller = AdaptiveFpsController(12.0)
        for now in range(10):
            controller.update(dropped=True, now=float(now * 2))
        self.assertEqual(controller.current_fps, 5.0)

        controller.set_maximum(4.0)
        self.assertEqual(controller.current_fps, 4.0)
        controller.set_maximum(30.0)
        self.assertEqual(controller.current_fps, 4.0)
        self.assertTrue(controller.update(dropped=False, now=40.0))
        self.assertEqual(controller.current_fps, 5.0)

    def test_adaptive_fps_keeps_uncapped_mode_unchanged(self):
        controller = AdaptiveFpsController(0.0)
        self.assertFalse(controller.update(dropped=True, now=0.0))
        self.assertEqual(controller.current_fps, 0.0)

    def test_onroad_twenty_offroad_one(self):
        ns = main_namespace()
        self.assertEqual(CLUSTER_FIXED_FPS, 20.0)
        self.assertEqual(ns["DEFAULT_FPS"], 20.0)
        self.assertEqual(ns["OFFROAD_RENDER_FPS"], 1.0)
        for mode in (*range(7), "invalid", None):
            self.assertEqual(normalize_cluster_live_fps(mode), 20.0)

    def test_encoder_display_and_auto_bitrate_follow_twenty(self):
        ns = main_namespace()
        self.assertEqual(ns["resolved_h264_encoder_fps"](20.0, 15), 20)
        self.assertEqual(ns["resolved_usb_display_fps"](None, "h264", target_fps=20.0, h264_fps=15), 20)
        self.assertEqual(ns["resolved_usb_h264_bitrate"]("auto", 20.0, 15), "4680k")
        self.assertEqual(ns["resolved_usb_display_fps"](5, "h264", target_fps=30.0, h264_fps=15), 5)
        self.assertEqual(ns["resolved_usb_h264_bitrate"]("2M", 30.0, 15), "2M")

    def test_offroad_transition_and_chestnut_pacing(self):
        tree = ast.parse((CLUSTER_DIR / "main.py").read_text(encoding="utf-8"))
        transitions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "render_fps" for target in node.targets)
            and isinstance(node.value, ast.IfExp)
        ]
        self.assertEqual(len(transitions), 2)
        interval = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "effective_frame_interval" for target in node.targets)
        )
        for transition in transitions:
            for offroad in (True, False):
                for chestnut in (True, False):
                    ns = main_namespace()
                    ns.update(
                        is_offroad=offroad,
                        chestnut_active=chestnut,
                        target_fps=20.0,
                        adaptive_fps=AdaptiveFpsController(20.0),
                    )
                    exec(compile(ast.Module(body=[transition], type_ignores=[]), "main.py", "exec"), ns)
                    ns["frame_interval"] = 1.0 / ns["render_fps"]
                    exec(compile(ast.Module(body=[interval], type_ignores=[]), "main.py", "exec"), ns)
                    self.assertEqual(ns["effective_frame_interval"], 1.0 if offroad else 1.0 / 20.0)

        pacing_names = {node.id for node in ast.walk(interval) if isinstance(node, ast.Name)}
        self.assertEqual(pacing_names, {"effective_frame_interval", "frame_interval"})
        self.assertFalse(any(isinstance(node, ast.Name) and node.id == "chestnut_active" for node in ast.walk(tree)))

    def test_autorun_default_and_environment_override(self):
        path = CLUSTER_DIR.parent / "cluster_autorun.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [
            node
            for node in tree.body
            if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "DEFAULT_FPS")
            or (isinstance(node, ast.FunctionDef) and node.name == "cluster_fps")
        ]
        namespace = {"os": os}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), namespace)
        for override, expected in (("", "20"), ("  ", "20"), ("10", "10")):
            with patch.dict(os.environ, {"CLUSTER_FPS": override}):
                self.assertEqual(namespace["cluster_fps"](), expected)


if __name__ == "__main__":
    unittest.main()

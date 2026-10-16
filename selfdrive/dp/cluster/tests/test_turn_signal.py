from __future__ import annotations

import ast
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest


CLUSTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLUSTER_DIR))

from cluster_config import TURN_SIGNAL_BLINK_ON_SECONDS, TURN_SIGNAL_BLINK_PERIOD_SECONDS
from cluster_utils import blink_visible


def load_turn_signal_method():
    tree = ast.parse((CLUSTER_DIR / "cluster_renderer.py").read_text(encoding="utf-8"))
    method = next(
        node
        for cls in tree.body if isinstance(cls, ast.ClassDef)
        for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_turn_signal_lit"
    )
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method],
        type_ignores=[],
    )
    namespace = {"time": time, "blink_visible": blink_visible}
    exec(compile(ast.fix_missing_locations(module), "cluster_renderer.py", "exec"), namespace)
    return namespace["_turn_signal_lit"]


class TurnSignalTests(unittest.TestCase):
    def setUp(self):
        self.draw = load_turn_signal_method()
        self.hud = SimpleNamespace(
            _left_turn_signal_started_at=None,
            _right_turn_signal_started_at=None,
            blink_fps=30.0,
        )

    def test_reference_period_and_duty_cycle(self):
        self.assertEqual(TURN_SIGNAL_BLINK_PERIOD_SECONDS, 0.70)
        self.assertEqual(TURN_SIGNAL_BLINK_ON_SECONDS, 0.37)
        self.assertAlmostEqual(TURN_SIGNAL_BLINK_PERIOD_SECONDS - TURN_SIGNAL_BLINK_ON_SECONDS, 0.33)
        for cycle in range(5):
            for offset, expected in ((0.001, True), (0.369, True), (0.371, False), (0.699, False)):
                self.assertEqual(blink_visible(cycle * 0.7 + offset, 0.0, float("inf")), expected)

    def test_phase_survives_fps_changes_and_skipped_frames(self):
        self.assertTrue(self.draw(self.hud, "left", True, now=0.0, advance=True))
        for fps, now, expected in ((30.0, 0.38, False), (20.0, 0.71, True), (5.0, 1.1, False), (10.0, 2.81, True)):
            self.hud.blink_fps = fps
            self.assertEqual(self.draw(self.hud, "left", True, now=now, advance=True), expected)
            self.assertEqual(self.draw(self.hud, "left", True, now=now), expected)

    def test_signals_have_independent_start_times_and_reset(self):
        self.assertTrue(self.draw(self.hud, "left", True, now=0.0))
        self.assertTrue(self.draw(self.hud, "right", True, now=0.4))
        self.assertFalse(self.draw(self.hud, "left", True, now=0.4))
        self.assertFalse(self.draw(self.hud, "left", False, now=0.5))
        self.assertIsNone(self.hud._left_turn_signal_started_at)
        self.assertEqual(self.hud._right_turn_signal_started_at, 0.4)
        self.assertTrue(self.draw(self.hud, "left", True, now=0.6))


if __name__ == "__main__":
    unittest.main()

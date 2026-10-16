from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock


CLUSTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLUSTER_DIR))

from cluster_config import normalize_cluster_brightness_percent


def brightness_namespace():
    tree = ast.parse((CLUSTER_DIR / "main.py").read_text(encoding="utf-8"))
    constants = {
        "MIN_USB_BRIGHTNESS",
        "MAX_USB_BRIGHTNESS",
        "DEFAULT_USB_BRIGHTNESS",
        "AUTO_USB_BRIGHTNESS_SCALE",
        "OFFROAD_USB_BRIGHTNESS",
    }
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in constants:
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "resolved_usb_brightness":
            nodes.append(node)
    namespace = {"normalize_cluster_brightness_percent": normalize_cluster_brightness_percent}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(CLUSTER_DIR / "main.py"), "exec"), namespace)
    return namespace


class BrightnessTests(unittest.TestCase):
    def setUp(self):
        self.ns = brightness_namespace()
        self.resolve = self.ns["resolved_usb_brightness"]

    def test_manual_limits(self):
        for setting, expected in ((0, 3), (1, 3), (3, 3), (10, 10), (13, 13), (15, 13), (30, 13), (100, 13)):
            with self.subTest(setting=setting):
                self.assertEqual(self.resolve(setting, None, auto_enabled=False), expected)

    def test_auto_limits_and_scaling(self):
        source = Mock()
        for ambient, expected in ((0, 3), (3, 3), (10, 7), (13, 9), (15, 9), (30, 9), (100, 9)):
            with self.subTest(ambient=ambient):
                source.ambient_brightness_percent.return_value = ambient
                self.assertEqual(self.resolve(0, source, auto_enabled=True), expected)

    def test_missing_ambient_uses_thirteen(self):
        self.assertEqual(self.resolve(0, None, auto_enabled=True), 13)
        source = Mock()
        source.ambient_brightness_percent.return_value = None
        self.assertEqual(self.resolve(0, source, auto_enabled=True), 13)

    def test_manual_setting_overrides_auto(self):
        source = Mock()
        self.assertEqual(self.resolve(100, source, auto_enabled=True), 13)
        source.ambient_brightness_percent.assert_not_called()

    def test_offroad_dim_unchanged(self):
        self.assertEqual(self.ns["OFFROAD_USB_BRIGHTNESS"], 2)


if __name__ == "__main__":
    unittest.main()

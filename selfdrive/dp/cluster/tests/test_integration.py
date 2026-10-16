from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[4]
CLUSTER_DIR = ROOT / "selfdrive" / "dp" / "cluster"


class ClusterIntegrationTests(unittest.TestCase):
    def test_direct_openpilot_imports_exist(self):
        for source in CLUSTER_DIR.glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("openpilot."):
                    module = ROOT.joinpath(*node.module.split(".")[1:])
                    with self.subTest(source=source.name, module=node.module):
                        self.assertTrue(module.with_suffix(".py").is_file() or (module / "__init__.py").is_file())

    def test_manager_starts_cluster_supervisor(self):
        tree = ast.parse((ROOT / "system/manager/process_config.py").read_text())
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "PythonProcess" and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "cluster_d"
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[1].value, "selfdrive.dp.cluster_autorun")
        self.assertEqual(calls[0].args[2].id, "always_run")
        self.assertTrue((ROOT / "selfdrive/dp/__init__.py").is_file())

    def test_all_cluster_params_are_registered_and_persistent(self):
        tree = ast.parse((CLUSTER_DIR / "cluster_config.py").read_text())
        params = {
            node.value.value for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str) and node.value.value.startswith("ClusterHud")
        }
        self.assertEqual(len(params), 13)
        keys = (ROOT / "common/params_keys.h").read_text()
        for param in params:
            with self.subTest(param=param):
                self.assertRegex(keys, rf'\{{"{re.escape(param)}", \{{PERSISTENT, (INT|STRING)\}}\}}')

    def test_native_encoder_build_target_and_assets_exist(self):
        build = (ROOT / "system/loggerd/SConscript").read_text()
        tree = ast.parse(build)
        target = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.If) and ast.unparse(node.test) == "arch == 'larch64'"
            and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "SharedLibrary" for call in ast.walk(node))
        )
        self.assertIn("'cluster_h264_encoder_bridge'", ast.unparse(target))
        for name in ("cluster_h264_encoder.cc", "cluster_h264_encoder_bridge.cc"):
            self.assertIn(name, ast.unparse(target))
            self.assertTrue((ROOT / "system/loggerd/encoder" / name).is_file())
        for asset in ("bg.png", "chestnut.png", "fonts/JetBrainsMono-Medium.ttf"):
            with self.subTest(asset=asset):
                data = (CLUSTER_DIR / "assets" / asset).read_bytes()
                self.assertGreater(len(data), 1024)
                self.assertFalse(data.startswith(b"version https://git-lfs.github.com"))


if __name__ == "__main__":
    unittest.main()

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
  "onroad,present,loading,running,failed,expected",
  [
    (True, False, False, True, False, True),
    (True, False, True, False, False, True),
    (True, True, False, True, False, False),
    (True, False, False, False, False, False),
    (True, False, False, True, True, False),
    (False, False, False, True, False, False),
  ],
)
def test_disconnect_failure(onroad, present, loading, running, failed, expected):
  tree = ast.parse((ROOT / "dragonpilot/egpu/manager.py").read_text(encoding="utf-8"))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EgpuModelManager")
  tick = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "tick")
  values = {"IsOnroad": onroad, "present": present, "loading": loading, "running": running, "failed": failed, "seen": True}
  writes = []
  errors = []
  refreshed = []
  progress_checks = []
  params = SimpleNamespace(get_bool=lambda key: values.get(key, False), put_bool=lambda *args, **kwargs: writes.append((args, kwargs)), get=lambda key: None)
  manager = SimpleNamespace(
    params=params,
    _rom_logged=False,
    refresh_catalog=lambda: refreshed.append(True),
    validate_active=lambda: None,
    auto_default=lambda: None,
    process_download_request=lambda: None,
    check_model_progress=lambda: progress_checks.append(True),
  )
  logger = SimpleNamespace(error=errors.append)
  namespace = {
    "egpu_present": lambda: present,
    "egpu_rom_present": lambda: False,
    "cloudlog": logger,
    **{f"P_{key.upper()}": key for key in ["present", "loading", "running", "failed", "seen", "delete_ref", "clear_cache"]},
  }
  exec(compile(ast.Module(body=[tick], type_ignores=[]), "<egpu manager tick>", "exec"), namespace)
  namespace["tick"](manager)
  if expected:
    assert writes == [(("failed", True), {"block": True})]
    assert len(errors) == 1
  else:
    assert writes == []
    assert errors == []
  assert bool(refreshed) == (not onroad)
  assert bool(progress_checks) == onroad


def test_failure_latches_stock_until_next_drive():
  tree = ast.parse((ROOT / "dragonpilot/egpu/bundles.py").read_text(encoding="utf-8"))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EgpuRoute")
  namespace = {"Params": object, "P_FAILED": "failed", "egpu_should_drive": lambda params: not params.get_bool("failed")}
  exec(compile(ast.Module(body=[cls], type_ignores=[]), "<egpu routing>", "exec"), namespace)
  route = namespace["EgpuRoute"]()
  flags = {"failed": False}
  params = SimpleNamespace(get_bool=lambda key: flags[key])
  assert route.update(True, params)
  flags["failed"] = True
  assert not route.update(True, params)
  flags["failed"] = False
  assert not route.update(True, params)
  assert not route.update(False, params)
  assert route.update(True, params)


@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("compiled", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_failed_drive_never_selects_builtin_gpu(present, compiled, failed):
  tree = ast.parse((ROOT / "selfdrive/modeld/helpers.py").read_text(encoding="utf-8"))
  fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "use_builtin_usbgpu")
  namespace = {}
  exec(compile(ast.Module(body=[fn], type_ignores=[]), "<model backend selection>", "exec"), namespace)
  assert namespace["use_builtin_usbgpu"](present, compiled, failed) == (present and compiled and not failed)

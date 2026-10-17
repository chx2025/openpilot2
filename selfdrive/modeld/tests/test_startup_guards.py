import ast
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]


def test_generated_files_do_not_dirty_build_cache(tmp_path):
  subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
  (tmp_path / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
  generated = ["jetlink", "prebuild", "selfdrive/assets/fonts/events_chars.cache", "system/manager/log.log"]
  for name in [*generated, "new_source.py"]:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("test\n", encoding="utf-8")
  ignored = subprocess.run(
    ["git", "-C", str(tmp_path), "check-ignore", *generated, "new_source.py"], check=True, capture_output=True, text=True
  ).stdout.splitlines()
  assert ignored == generated


def test_probe_only_initializes_host_backends():
  tree = ast.parse((ROOT / "selfdrive/modeld/SConscript").read_text(encoding="utf-8"))
  probe = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "probe_devices")
  calls = []

  def run(args, **kwargs):
    calls.append((args, kwargs))
    return SimpleNamespace(stdout="QCOM\nCPU\n")

  namespace = {"subprocess": SimpleNamespace(run=run), "sys": SimpleNamespace(executable="python")}
  exec(compile(ast.Module(body=[probe], type_ignores=[]), "<device probe>", "exec"), namespace)
  assert namespace["probe_devices"]() == {"QCOM", "CPU"}
  args, options = calls[0]
  assert options["timeout"] == 30
  assert options["check"]
  requested = []

  class Device:
    def __getitem__(self, name):
      requested.append(name)
      return SimpleNamespace(device=name)

  script = args[2].replace("from tinygrad import Device", "")
  exec(script, {"Device": Device()})
  assert requested == ["CUDA", "QCOM", "CPU"]

  def timeout(*args, **kwargs):
    raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

  namespace["subprocess"] = SimpleNamespace(run=timeout)
  with pytest.raises(subprocess.TimeoutExpired):
    namespace["probe_devices"]()


def test_startup_diagnostics_include_ignition_and_force_offroad():
  tree = ast.parse((ROOT / "system/hardware/hardwared.py").read_text(encoding="utf-8"))
  block = next(
    n
    for n in ast.walk(tree)
    if isinstance(n, ast.If) and isinstance(n.test, ast.UnaryOp) and isinstance(n.test.operand, ast.Name) and n.test.operand.id == "should_start"
  )
  events = []
  namespace = {
    "should_start": False,
    "startup_conditions": {"device_booted": True},
    "onroad_conditions": {"ignition": False},
    "dp_dev_go_off_road": True,
    "sm": SimpleNamespace(alive={"pandaStates": True}, valid={"pandaStates": True}),
    "pandaStates": [SimpleNamespace(ignitionLine=False, ignitionCan=False)],
    "in_car": False,
    "startup_diagnostic_prev": {},
    "startup_diagnostic_ts": 0.0,
    "time": SimpleNamespace(monotonic=lambda: 10.0),
    "cloudlog": SimpleNamespace(event=lambda *args, **kwargs: events.append((args, kwargs))),
  }
  code = compile(ast.Module(body=[block], type_ignores=[]), "<startup diagnostic>", "exec")
  exec(code, namespace)
  assert events[0][1]["blockers"] == ["ignition", "not_forced_offroad"]
  exec(code, namespace)
  assert len(events) == 1
  namespace["time"] = SimpleNamespace(monotonic=lambda: 40.0)
  exec(code, namespace)
  assert len(events) == 2
  namespace["onroad_conditions"]["ignition"] = True
  exec(code, namespace)
  assert len(events) == 3
  assert events[-1][1]["blockers"] == ["not_forced_offroad"]

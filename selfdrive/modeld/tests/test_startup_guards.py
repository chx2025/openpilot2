import ast
import io
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


@pytest.mark.parametrize("first_offroad,now,expected", [
  (None, 100.0, False),
  (100.0, 100.0, False),
  (100.0, 102.999, False),
  (100.0, 103.0, True),
  (100.0, 110.0, True),
])
def test_initial_offroad_requires_three_seconds_since_first_publication(first_offroad, now, expected):
  tree = ast.parse((ROOT / "system/hardware/hardwared.py").read_text(encoding="utf-8"))
  constant = next(n for n in tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "STARTUP_OFFROAD_TIME" for t in n.targets))
  assert ast.literal_eval(constant.value) == 3.0
  guard = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                       and t.slice.value == "initial_offroad_complete" for t in n.targets))
  namespace = {"initial_offroad_ts": first_offroad, "time": SimpleNamespace(monotonic=lambda: now),
               "STARTUP_OFFROAD_TIME": 3.0, "startup_conditions": {}}
  exec(compile(ast.Module(body=[guard], type_ignores=[]), "<initial offroad guard>", "exec"), namespace)
  assert namespace["startup_conditions"]["initial_offroad_complete"] == expected


def test_initial_offroad_clock_starts_once_after_device_state_publication():
  tree = ast.parse((ROOT / "system/hardware/hardwared.py").read_text(encoding="utf-8"))
  thread = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "hardware_thread")
  loop = next(n for n in thread.body if isinstance(n, ast.While))
  index = next(i for i, n in enumerate(loop.body) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
               and ast.unparse(n.value.func) == "pm.send")
  clock_start = loop.body[index + 1]
  assert ast.unparse(clock_start.test) == "initial_offroad_ts is None"
  namespace = {"initial_offroad_ts": None, "time": SimpleNamespace(monotonic=lambda: 100.0)}
  code = compile(ast.Module(body=[clock_start], type_ignores=[]), "<initial offroad clock>", "exec")
  exec(code, namespace)
  assert namespace["initial_offroad_ts"] == 100.0
  namespace["time"] = SimpleNamespace(monotonic=lambda: 200.0)
  exec(code, namespace)
  assert namespace["initial_offroad_ts"] == 100.0


@pytest.mark.parametrize("delay_complete,ignition,other_ready,forced_offroad,expected", [
  (False, True, True, False, False),
  (True, True, True, False, True),
  (True, False, True, False, False),
  (True, True, False, False, False),
  (True, True, True, True, False),
])
def test_boot_delay_preserves_real_ignition_and_other_startup_gates(delay_complete, ignition, other_ready, forced_offroad, expected):
  tree = ast.parse((ROOT / "system/hardware/hardwared.py").read_text(encoding="utf-8"))
  thread = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "hardware_thread")
  loop = next(n for n in thread.body if isinstance(n, ast.While))
  index = next(i for i, n in enumerate(loop.body) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "should_start" for t in n.targets))
  namespace = {
    "dp_dev_go_off_road": forced_offroad, "onroad_conditions": {"ignition": ignition},
    "started_ts": None, "startup_conditions": {"initial_offroad_complete": delay_complete, "other_ready": other_ready},
  }
  exec(compile(ast.Module(body=loop.body[index:index + 2], type_ignores=[]), "<startup decision>", "exec"), namespace)
  assert namespace["should_start"] == expected


@pytest.mark.parametrize("device_type,brightness,expected", [
  ("tici", 0, 0),
  ("tici", 204, 49),
  ("tici", 409, 99),
  ("tici", 664, 100),
  ("tici", 1023, 100),
  ("mici", 664, 64),
  ("mici", 1023, 100),
])
def test_boot_brightness_stays_within_device_state_percentage(device_type, brightness, expected):
  tree = ast.parse((ROOT / "system/hardware/tici/hardware.py").read_text(encoding="utf-8"))
  getter = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "get_screen_brightness")

  def open_backlight(path):
    values = {
      "/sys/class/backlight/panel0-backlight/max_brightness": 1023,
      "/sys/class/backlight/panel0-backlight/brightness": brightness,
    }
    return io.StringIO(str(values[path]))

  namespace = {"open": open_backlight}
  exec(compile(ast.Module(body=[getter], type_ignores=[]), "<screen brightness>", "exec"), namespace)
  actual = namespace["get_screen_brightness"](SimpleNamespace(get_device_type=lambda: device_type))
  assert actual == expected
  assert 0 <= actual <= 100


def test_hardwared_worker_exit_fails_process_and_joins_threads():
  tree = ast.parse((ROOT / "system/hardware/hardwared.py").read_text(encoding="utf-8"))
  main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
  threads = []
  ended = []
  joined = []

  def thread(target, args):
    result = SimpleNamespace(
      name=target.__name__, start=lambda: None,
      is_alive=lambda: target.__name__ != "hardware_thread",
      join=lambda: joined.append(target.__name__),
    )
    threads.append(result)
    return result

  def hw_state_thread(*args):
    pass

  def hardware_thread(*args):
    pass

  def touch_thread(*args):
    pass

  namespace = {
    "queue": SimpleNamespace(Queue=lambda **kwargs: None),
    "threading": SimpleNamespace(Event=lambda: SimpleNamespace(set=lambda: ended.append(True)), Thread=thread),
    "hw_state_thread": hw_state_thread, "hardware_thread": hardware_thread, "touch_thread": touch_thread,
    "TICI": True, "time": SimpleNamespace(sleep=lambda seconds: None),
  }
  exec(compile(ast.Module(body=[main], type_ignores=[]), "<hardwared main>", "exec"), namespace)
  with pytest.raises(RuntimeError, match="hardwared threads stopped unexpectedly: hardware_thread"):
    namespace["main"]()
  assert ended == [True]
  assert joined == [t.name for t in threads]


def test_manager_restarts_dead_hardwared():
  tree = ast.parse((ROOT / "system/manager/process_config.py").read_text(encoding="utf-8"))
  process = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "PythonProcess" and n.args and isinstance(n.args[0], ast.Constant)
                 and n.args[0].value == "hardwared")
  assert any(k.arg == "restart_if_crash" and ast.literal_eval(k.value) is True for k in process.keywords)

  process_tree = ast.parse((ROOT / "system/manager/process.py").read_text(encoding="utf-8"))
  ensure = next(n for n in process_tree.body if isinstance(n, ast.FunctionDef) and n.name == "ensure_running")
  calls = []
  dead = SimpleNamespace(
    name="hardwared", enabled=True, restart_if_crash=True,
    should_run=lambda *args: True, proc=SimpleNamespace(is_alive=lambda: False, exitcode=0),
    restart=lambda: calls.append("restart"), start=lambda: calls.append("start"),
  )
  namespace = {"cloudlog": SimpleNamespace(error=lambda message: calls.append(message))}
  future = ast.parse("from __future__ import annotations").body
  exec(compile(ast.Module(body=[*future, ensure], type_ignores=[]), "<ensure running>", "exec"), namespace)
  assert namespace["ensure_running"]([dead], False) == [dead]
  assert calls == ["Restarting hardwared (exitcode 0)", "restart", "start"]

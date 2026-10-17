import ast
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest


ROOT = Path(__file__).resolve().parents[3]


def load_definitions(path, names, namespace):
  tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
  definitions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names]
  assert {node.name for node in definitions} == set(names)
  exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), namespace)
  return namespace


def make_watchdog():
  clock = SimpleNamespace(now=100.0)
  flags = {"loading": True, "running": False, "failed": False}
  writes, errors = [], []

  def put_bool(key, value, **kwargs):
    writes.append((key, value, kwargs))
    flags[key] = value

  sm = SimpleNamespace(update=lambda timeout: None, logMonoTime={"modelV2": 0, "modelExt": 0}, big=False)

  class Messages:
    def update(self, timeout):
      sm.update(timeout)

    @property
    def logMonoTime(self):
      return sm.logMonoTime

    def __getitem__(self, name):
      assert name == "modelExt"
      return SimpleNamespace(bigModel=sm.big)

  namespace = load_definitions(
    "dragonpilot/egpu/manager.py",
    ["check_model_progress"],
    {
      "time": SimpleNamespace(monotonic=lambda: clock.now),
      "P_LOADING": "loading",
      "P_RUNNING": "running",
      "P_FAILED": "failed",
      "FIRST_OUTPUT_TIMEOUT": 150,
      "MODEL_OUTPUT_TIMEOUT": 5,
      "cloudlog": SimpleNamespace(error=errors.append),
    },
  )
  manager = SimpleNamespace(
    params=SimpleNamespace(get_bool=lambda key: flags[key], put_bool=put_bool),
    sm=Messages(),
    _model_wait_started=None,
    _last_model_output=None,
  )
  return lambda: namespace["check_model_progress"](manager), clock, flags, sm, writes, errors, manager


def test_timeout_constants_cover_loading_and_first_output():
  tree = ast.parse((ROOT / "dragonpilot/egpu/constants.py").read_text(encoding="utf-8"))
  names = {"BIG_MODEL_TIMEOUT", "PCIE_WAIT", "FIRST_OUTPUT_TIMEOUT", "MODEL_OUTPUT_TIMEOUT"}
  nodes = [node for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)]
  namespace = {}
  exec(compile(ast.Module(body=nodes, type_ignores=[]), "<egpu deadlines>", "exec"), namespace)
  assert namespace["FIRST_OUTPUT_TIMEOUT"] == 150
  assert namespace["MODEL_OUTPUT_TIMEOUT"] == 5


def test_first_output_deadline_is_bounded_and_failure_latches():
  tick, clock, flags, _, writes, errors, _ = make_watchdog()
  tick()
  clock.now += 149.999
  tick()
  assert writes == []
  clock.now = 250
  tick()
  assert writes == [("failed", True, {"block": True})]
  assert "no first big-model output within 150s" in errors[0]
  assert flags["failed"]
  tick()
  assert len(writes) == 1


@pytest.mark.parametrize("stalled_service", ["modelV2", "modelExt", "both"])
def test_running_output_stall_falls_back_even_with_usb_present(stalled_service):
  tick, clock, flags, sm, writes, errors, _ = make_watchdog()
  flags.update(loading=False, running=True)
  sm.big = True
  sm.logMonoTime = dict.fromkeys(sm.logMonoTime, int(clock.now * 1e9))
  tick()
  clock.now += 4.999
  for name in sm.logMonoTime:
    if stalled_service not in (name, "both"):
      sm.logMonoTime[name] = int(clock.now * 1e9)
  tick()
  assert writes == []
  clock.now = 105
  tick()
  assert writes == [("failed", True, {"block": True})]
  assert "no big-model output for 5s" in errors[0]


def test_healthy_outputs_and_initial_invalid_frames_do_not_fail():
  tick, clock, flags, sm, writes, _, _ = make_watchdog()
  flags.update(loading=False, running=True)
  sm.big = True
  for _ in range(180):
    clock.now += 1
    sm.logMonoTime = dict.fromkeys(sm.logMonoTime, int(clock.now * 1e9))
    tick()
  assert writes == []


def test_stock_output_cannot_keep_failed_big_model_alive():
  tick, clock, flags, sm, writes, _, _ = make_watchdog()
  flags.update(loading=False, running=True)
  tick()
  clock.now += 5
  sm.logMonoTime = dict.fromkeys(sm.logMonoTime, int(clock.now * 1e9))
  sm.big = False
  tick()
  assert writes == [("failed", True, {"block": True})]


def test_inactive_watchdog_resets_for_next_attempt():
  tick, clock, flags, _, writes, _, manager = make_watchdog()
  tick()
  flags["loading"] = False
  clock.now += 500
  tick()
  assert manager._model_wait_started is None
  assert manager._last_model_output is None
  flags["loading"] = True
  tick()
  assert manager._model_wait_started == clock.now
  assert writes == []


def test_reset_drains_both_devices_before_releasing_camera_mappings():
  events = []

  class Devices:
    def __getitem__(self, name):
      return SimpleNamespace(synchronize=lambda: events.append(("sync", name)))

  class State:
    def zeros_like(self):
      return "zeros"

    def assign(self, value):
      assert value == "zeros"
      return SimpleNamespace(realize=lambda: events.append(("enqueue",)))

  class Array:
    def __setitem__(self, key, value):
      assert value == 0

  namespace = load_definitions("dragonpilot/egpu/runner.py", ["reset"], {"Device": Devices()})
  model = SimpleNamespace(
    packed=Array(),
    prev_desire=Array(),
    state={"hidden": State()},
    model_device="AMD",
    warp_device="QCOM",
    _blob_cache=SimpleNamespace(clear=lambda: events.append(("clear",))),
  )
  namespace["reset"](model)
  assert events == [("enqueue",), ("sync", "AMD"), ("sync", "QCOM"), ("sync", "NPY"), ("clear",)]


@pytest.mark.parametrize("egpu,run_count,expected", [(True, 1, True), (True, 2, False), (False, 1, False)])
def test_running_and_fifo_only_follow_first_published_camera_frame(egpu, run_count, expected):
  tree = ast.parse((ROOT / "selfdrive/modeld/modeld.py").read_text(encoding="utf-8"))
  block = next(node for node in ast.walk(tree) if isinstance(node, ast.If) and ast.unparse(node.test) == "EGPU and run_count == 1")
  publish = next(node for node in ast.walk(tree) if isinstance(node, ast.If) and ast.unparse(node.test) == "model_output is not None")
  assert publish.body[-1] is block
  sends = publish.body[-5:-1]
  events = []
  namespace = {
    "EGPU": egpu,
    "run_count": run_count,
    "P_RUNNING": "running",
    "P_LOADING": "loading",
    "modelv2_send": None,
    "drivingdata_send": None,
    "posenet_send": None,
    "model_ext_send": None,
    "pm": SimpleNamespace(send=lambda name, msg: events.append(("publish", name))),
    "config_realtime_process": lambda *args: events.append(("realtime", args)),
    "params": SimpleNamespace(put_bool=lambda *args, **kwargs: events.append(("param", args, kwargs))),
    "cloudlog": SimpleNamespace(warning=lambda msg: events.append(("log",))),
  }
  exec(compile(ast.Module(body=[*sends, block], type_ignores=[]), "<first output>", "exec"), namespace)
  assert events[:4] == [("publish", name) for name in ["modelV2", "drivingModelData", "cameraOdometry", "modelExt"]]
  if expected:
    assert events[4:] == [
      ("realtime", (7, 54)),
      ("param", ("running", True), {"block": True}),
      ("param", ("loading", False), {"block": True}),
      ("log",),
    ]
  else:
    assert len(events) == 4


def test_failure_preserves_traceback_and_latches_stock():
  events = []
  namespace = load_definitions(
    "dragonpilot/egpu/modeld_egpu.py",
    ["_fail"],
    {
      "Params": object,
      "P_FAILED": "failed",
      "P_RUNNING": "running",
      "P_LOADING": "loading",
      "cloudlog": SimpleNamespace(exception=lambda msg: events.append(("exception", msg))),
    },
  )
  params = SimpleNamespace(put_bool=lambda *args, **kwargs: events.append(("param", args, kwargs)))
  namespace["_fail"](params, "NPY hang detected")
  assert events[0] == ("exception", "egpu: big model failed: NPY hang detected")
  assert events[1] == ("param", ("failed", True), {"block": True})


def test_inference_error_identifies_stage_and_is_not_swallowed():
  errors = []
  namespace = load_definitions(
    "dragonpilot/egpu/runner.py",
    ["run"],
    {
      "np": SimpleNamespace(ndarray=object),
      "cloudlog": SimpleNamespace(exception=errors.append),
    },
  )

  def fail(*args):
    raise RuntimeError("NPY hang detected")

  model = SimpleNamespace(_run=fail, stage="img warp readback", _warming_up=False, _first_frame=True)
  with pytest.raises(RuntimeError, match="NPY hang detected"):
    namespace["run"](model, {}, {}, {})
  assert errors == ["egpu: inference failed during img warp readback (warmup=False)"]
  assert model._first_frame


def test_loader_never_declares_dummy_warmup_as_running():
  tree = ast.parse((ROOT / "dragonpilot/egpu/modeld_egpu.py").read_text(encoding="utf-8"))
  factory = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "make_loader")

  class WithoutImports(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
      return ast.copy_location(ast.Pass(), node)

  factory = WithoutImports().visit(factory)
  events = []
  model = SimpleNamespace(layout="stateful", warmup=lambda: events.append("warmup"))
  namespace = {
    "Params": object,
    "os": SimpleNamespace(path=SimpleNamespace(join=lambda *args: "\\".join(args))),
    "threading": threading,
    "time": SimpleNamespace(monotonic=lambda: 0.0),
    "get_active_bundle": lambda params: {"sha256": "sha", "short_name": "CTV3M"},
    "active_ready": lambda bundle: True,
    "model_root": lambda: "models",
    "model_pkl_name": lambda sha: "model.pkl",
    "warp_pkl_name": lambda *args: "warp.pkl",
    "tg_env": SimpleNamespace(patch_fetch_fw=lambda: None),
    "PCIE_WAIT": 15,
    "BIG_MODEL_TIMEOUT": 120,
    "wait_pcie_link": lambda timeout: (True, 0),
    "EgpuModelState": lambda *args: model,
    "cloudlog": SimpleNamespace(warning=lambda msg: events.append("loaded")),
    "P_LOADING": "loading",
    "P_RUNNING": "running",
  }
  exec(compile(ast.Module(body=[factory], type_ignores=[]), "<egpu loader>", "exec"), namespace)
  params = SimpleNamespace(put_bool=lambda *args, **kwargs: events.append(("param", args)))
  assert namespace["make_loader"](params)(1928, 1208) is model
  assert events == ["warmup", "loaded"]

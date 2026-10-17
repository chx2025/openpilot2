import argparse
import ast
from contextlib import nullcontext
import errno
import importlib.util
import io
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cluster_config

SPEC = importlib.util.spec_from_file_location("cluster_supervisor", Path(__file__).resolve().parents[2] / "cluster_autorun.py")
supervisor = importlib.util.module_from_spec(SPEC)
with pytest.MonkeyPatch.context() as mp:
    mp.setitem(sys.modules, "openpilot.selfdrive.dp.cluster.cluster_config", cluster_config)
    SPEC.loader.exec_module(supervisor)


class Child:
    def __init__(self, *, hung=False, exited=False):
        self.hung = hung
        self.exited = exited
        self.calls = []

    def poll(self):
        return 1 if self.exited else None

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")

    def wait(self, *, timeout):
        self.calls.append(("wait", timeout))
        if self.hung:
            self.hung = False
            raise subprocess.TimeoutExpired("cluster", timeout)


def test_startup_and_progress_deadlines(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    assert not supervisor.heartbeat_expired(heartbeat, 100, 189.9)
    assert supervisor.heartbeat_expired(heartbeat, 100, 190)
    heartbeat.write_text("180", encoding="ascii")
    assert not supervisor.heartbeat_expired(heartbeat, 100, 209.9)
    assert supervisor.heartbeat_expired(heartbeat, 100, 210)


def test_partial_heartbeat_uses_bounded_startup_grace(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text("", encoding="ascii")
    assert supervisor.heartbeat_expired(heartbeat, 100, 190)


@pytest.mark.parametrize("hung", [False, True])
def test_stop_escalates_with_bounded_waits(hung):
    child = Child(hung=hung)
    supervisor.stop_child(child)
    assert child.calls == (["terminate", ("wait", 5), "kill", ("wait", 5)] if hung else ["terminate", ("wait", 5)])


def test_exited_child_is_not_signalled():
    child = Child(exited=True)
    supervisor.stop_child(child)
    assert child.calls == []


def test_departure_feature_is_absent():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "cluster_departure.py").exists()
    for name in ("cluster_live.py", "cluster_models.py", "cluster_renderer.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "departure_reminder" not in source
        assert "READY TO GO" not in source


@pytest.mark.parametrize("mode,product_id", [(5, 0x0092), (6, 0x0123)])
def test_selected_model_command(mode, product_id):
    assert cluster_config.CLUSTER_DISPLAY_PRODUCT_IDS[mode] == product_id
    command = supervisor.cluster_cmd(product_id)
    for option, expected in (("--input", "live"), ("--output", "usb"),
                             ("--usb-product-id", hex(product_id)), ("--usb-codec", "h264")):
        assert command[command.index(option) + 1] == expected
    assert "--width" not in command
    assert "--height" not in command


def test_unknown_model_command_rejected():
    with pytest.raises(ValueError, match="Unsupported cluster USB product ID"):
        supervisor.cluster_cmd(0x0088)


@pytest.mark.parametrize("product_id", [0x0092, 0x0123])
def test_full_launch_arguments_reach_usb_driver(monkeypatch, product_id):
    root = Path(__file__).resolve().parents[1]
    namespace = vars(cluster_config).copy()
    namespace.update(argparse=argparse, errno=errno, Path=Path, sys=sys, OPENPILOT_PACKAGE_ROOT=root.parents[2])
    for filename in ("cluster_usb_display.py", "cluster_h264_pipeline.py", "main.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        nodes = [
            node for node in tree.body
            if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.isupper())
            or (isinstance(node, ast.FunctionDef) and node.name in ("parse_args", "option_present", "product_id_for_hud_mode"))
        ]
        module = ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes,
        ], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), filename, "exec"), namespace)
    monkeypatch.setattr(sys, "argv", supervisor.cluster_cmd(product_id)[1:])
    args = namespace["parse_args"]()
    assert args.usb_product_id == product_id
    assert args.output == "usb" and args.input == "live"
    assert args.width is None and args.height is None
    tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "run_demo")
    assert ast.unparse(call.args[-1]) == "args.usb_product_id"
    constructor = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Name) and node.func.id == "TuringUsbDisplay")
    selection = next(keyword.value for keyword in constructor.keywords if keyword.arg == "expected_product_id")
    namespace.update(usb_product_id=args.usb_product_id, hud_mode_watch=None)
    assert eval(compile(ast.Expression(body=selection), "<USB product selection>", "eval"), namespace) == product_id


def test_manager_only_runs_cluster_for_tr_modes():
    root = Path(__file__).resolve().parents[4]
    tree = ast.parse((root / "system/manager/process_config.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "cluster")
    namespace = {
        "CLUSTER_DISPLAY_MODE_PARAM": cluster_config.CLUSTER_DISPLAY_MODE_PARAM,
        "CLUSTER_DISPLAY_PRODUCT_IDS": cluster_config.CLUSTER_DISPLAY_PRODUCT_IDS,
    }
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function,
    ], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<manager cluster predicate>", "exec"), namespace)
    for started in (False, True):
        for mode in (None, "", *range(9)):
            params = SimpleNamespace(get=lambda key, mode=mode: mode)
            assert namespace["cluster"](started, params, None) == (mode in (5, 6))


def test_tr_modes_ignore_ignition_and_preserve_existing_modes():
    root = Path(__file__).resolve().parents[4]
    tree = ast.parse((root / "selfdrive/ui/ui_state.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Device")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_ignition_state_ovrride")
    state = SimpleNamespace(dp_ui_auto_brake_hold_active=False)
    namespace = {"ui_state": state}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<main display modes>", "exec"), namespace)
    for mode in range(7):
        state.dp_ui_display_mode = mode
        for ignition in (False, True):
            for cruise in (False, True):
                state.dp_ui_display_mode_cruise_available = cruise
                state.dp_ui_display_mode_cruise_enabled = cruise
                expected = False if mode in (5, 6) else ignition if mode == 0 else ignition and (cruise if mode in (1, 2) else not cruise)
                assert namespace["_ignition_state_ovrride"](None, ignition) == expected


def test_display_options_preserve_existing_indices_and_translate_description():
    root = Path(__file__).resolve().parents[4]
    path = root / "dragonpilot/settings/min-feat.ui.display-mode.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    namespace = {"tr": lambda text: text}
    exec(compile(ast.Module(body=tree.body[1:], type_ignores=[]), str(path), "exec"), namespace)
    item = namespace["ITEMS"][0]
    assert [option() for option in item["options"]] == ["Std.", "MAIN+", "OP+", "MAIN-", "OP-", "TR-9.2", "TR-12.3"]
    assert item["default"] == "0"
    description = item["description"]()
    assert "1920x462" in description and "1920x720" in description
    # Use the actual PO loader without importing the device UI stack.
    tree = ast.parse((root / "system/ui/lib/multilang.py").read_text(encoding="utf-8"))
    loaders = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ("load_translations", "_parse_quoted")]
    loader_namespace = {"re": re}
    exec(compile(ast.Module(body=loaders, type_ignores=[]), "<translation loader>", "exec"), loader_namespace)
    translations, _ = loader_namespace["load_translations"](root / "selfdrive/ui/translations/dragonpilot_zh-CHT.po")
    assert "只有 TR 模式" in translations[description]


@pytest.mark.parametrize("product_id,geometry", [(0x0092, (1920, 462)), (0x0123, (1920, 720))])
def test_selected_driver_uses_vendor_resolution(product_id, geometry):
    root = Path(__file__).resolve().parents[1]
    vendor = root / ".vendor/turing-smart-screen-python-main/library/lcd/lcd_comm_turing_usb.py"
    tree = ast.parse(vendor.read_text(encoding="utf-8"))
    sizes = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "PRODUCT_ID" for target in node.targets))
    product_sizes = ast.literal_eval(sizes)
    tree = ast.parse((root / "cluster_usb_display.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TuringUsbDisplay")
    connect = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_connect_device")
    namespace = {}
    exec(compile(ast.Module(body=[connect], type_ignores=[]), "<USB connect>", "exec"), namespace)
    display = SimpleNamespace(
        _find_expected_usb_device=lambda: (object(), product_id),
        _cache_out_endpoint=lambda: None,
        _product_id=product_sizes,
    )
    namespace["_connect_device"](display)
    assert display.dev_pid == product_id
    assert (display.landscape_width, display.landscape_height) == geometry


@pytest.mark.parametrize("product_id,filename", [(0x0092, "bg_9p2inches.png"), (0x0123, "bg.png"), (None, "bg.png")])
def test_renderer_uses_selected_panel_background(product_id, filename):
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "cluster_renderer.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ClusterUiRenderer")
    init = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    assignment = next(node for node in init.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Attribute) and target.attr == "_background_image_path" for target in node.targets))
    renderer = SimpleNamespace()
    namespace = {
        "self": renderer, "usb_product_id": product_id,
        "BACKGROUND_IMAGE_PATH": root / "assets/bg.png",
        "BACKGROUND_9P2_IMAGE_PATH": root / "assets/bg_9p2inches.png",
    }
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), "<panel background>", "exec"), namespace)
    assert renderer._background_image_path.name == filename
    assert renderer._background_image_path.is_file()
    load = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_load_drive_status_textures")
    assert "self._load_icon_texture(self._background_image_path, 'Cluster background')" in ast.unparse(load)
    tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    constructor = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Name) and node.func.id == "ClusterUiRenderer")
    selection = next(keyword.value for keyword in constructor.keywords if keyword.arg == "usb_product_id")
    assert eval(compile(ast.Expression(body=selection), "<renderer panel>", "eval"),
                {"usb_display": SimpleNamespace(dev_pid=product_id) if product_id else None}) == product_id


@pytest.mark.parametrize("product_id", [0x0092, 0x0123])
def test_missing_selected_usb_model_does_not_fall_back(monkeypatch, product_id):
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "cluster_usb_display.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TuringUsbDisplay")
    find = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_find_expected_usb_device")
    namespace = {"usbgpu_bus_lock": nullcontext, "TURZX_USB_VENDOR_ID": 0x1CBE}
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), find,
    ], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<USB selection>", "exec"), namespace)
    queries = []

    def find_usb(**query):
        queries.append(query)
        return None

    core = SimpleNamespace(find=find_usb)
    monkeypatch.setitem(sys.modules, "usb", SimpleNamespace(core=core))
    monkeypatch.setitem(sys.modules, "usb.core", core)
    display = SimpleNamespace(expected_product_id=product_id)
    with pytest.raises(ValueError, match=f"pid=0x{product_id:04x}"):
        namespace["_find_expected_usb_device"](display)
    assert queries == [{"idVendor": 0x1CBE, "idProduct": product_id}]


@pytest.mark.parametrize("disabled_mode", range(5))
def test_supervisor_switches_model_then_stops_usb_when_disabled(monkeypatch, disabled_mode):
    state = SimpleNamespace(mode=0, now=0.0, stop=None)
    children = []
    events = []
    params = SimpleNamespace(get=lambda key: state.mode)
    monkeypatch.setitem(sys.modules, "openpilot.common.params", SimpleNamespace(Params=lambda: params))
    monkeypatch.setattr(supervisor.signal, "signal", lambda signum, handler: setattr(state, "stop", handler))
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(supervisor, "heartbeat_expired", lambda *args: False)
    log = io.StringIO()
    # StringIO has no fileno; use inherited output for this isolated run.
    monkeypatch.setattr(supervisor, "_open_log", lambda: None)
    monkeypatch.setattr(supervisor, "_log", lambda file, text: log.write(text + "\n"))

    class RunningChild(Child):
        returncode = 0

        def terminate(self):
            self.exited = True
            events.append("stop")

    def launch(command, **kwargs):
        assert all(child.exited for child in children)
        child = RunningChild()
        child.product_id = int(command[command.index("--usb-product-id") + 1], 0)
        children.append(child)
        events.append(child.product_id)
        return child

    def advance(seconds):
        state.now += seconds
        if state.now >= 4:
            state.stop(0, None)
        elif state.now >= 3:
            state.mode = disabled_mode
        elif state.now >= 2:
            state.mode = 6
        elif state.now >= 1:
            state.mode = 5
        if state.mode in range(5):
            assert all(child.exited for child in children) or state.now == 3

    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    monkeypatch.setattr(supervisor.time, "sleep", advance)
    supervisor.main()
    assert events == [0x0092, "stop", 0x0123, "stop"]
    assert all(child.exited for child in children)

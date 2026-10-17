import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("mode", [5, 6])
@pytest.mark.parametrize("pc", [False, True])
def test_tr_screen_only_wakes_on_touch_and_times_out(mode, pc):
    root = Path(__file__).resolve().parents[4]
    tree = ast.parse((root / "selfdrive/ui/ui_state.py").read_text(encoding="utf-8"))
    device = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Device")
    methods = [node for node in device.body if isinstance(node, ast.FunctionDef)
               and node.name in ("_update_wakefulness", "_ignition_state_ovrride")]
    state = SimpleNamespace(dp_ui_display_mode=mode, ignition=True, dp_ui_auto_brake_hold_active=True)
    app = SimpleNamespace(mouse_events=[])
    clock = SimpleNamespace(now=100.0)
    namespace = {"ui_state": state, "gui_app": app, "time": SimpleNamespace(monotonic=lambda: clock.now), "PC": pc}
    cls = ast.ClassDef(name="TestDevice", bases=[], keywords=[], body=methods, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<display wake>", "exec"), namespace)
    screen = namespace["TestDevice"]()
    screen._tr_display_mode = None
    screen._tr_touch_active = False
    screen._ignition = True
    screen._interaction_time = 160.0
    screen._prev_timed_out = False
    callbacks = []
    screen._interactive_timeout_callbacks = [lambda: callbacks.append("timeout")]
    screen._set_awake = lambda on: setattr(screen, "awake", on)
    screen._reset_interactive_timeout = lambda: setattr(screen, "_interaction_time", clock.now + 5)

    screen._update_wakefulness()
    assert not screen.awake  # Initial boot grace, ignition and Auto Hold cannot wake it.
    app.mouse_events = [SimpleNamespace(left_down=True)]
    screen._update_wakefulness()
    assert screen.awake
    app.mouse_events = []
    clock.now = 104
    state.ignition = False
    screen._update_wakefulness()
    assert screen.awake
    assert screen._interaction_time == 105  # Ignition-off must not extend touch grace.
    clock.now = 106
    screen._update_wakefulness()
    assert not screen.awake
    assert callbacks == ["timeout"]
    state.ignition = True
    screen._update_wakefulness()
    assert not screen.awake
    app.mouse_events = [SimpleNamespace(left_down=True)]
    screen._update_wakefulness()
    assert screen.awake
    app.mouse_events = []
    state.dp_ui_display_mode = 6 if mode == 5 else 5
    screen._update_wakefulness()
    assert not screen.awake  # Switching TR models requires a new touch.
    state.dp_ui_display_mode = 0
    screen._update_wakefulness()
    assert screen.awake  # Stock ignition behavior restored immediately.

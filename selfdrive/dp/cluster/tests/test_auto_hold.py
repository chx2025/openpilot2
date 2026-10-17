import ast
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


CLUSTER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLUSTER_DIR))

from cluster_config import GREEN, RED
from cluster_live import LIVE_SERVICES_BASE, OpenpilotLiveSource, standby_state
from cluster_route_replay import load_openpilot_log_schema


@pytest.mark.parametrize("active", [False, True])
def test_actual_hold_schema_and_live_health_gates(active):
    schema = load_openpilot_log_schema()
    message = schema.Event.new_message(carStateExt={"autoBrakeHoldActive": active}).carStateExt
    source = OpenpilotLiveSource.__new__(OpenpilotLiveSource)
    source.services = ["carState", "carStateExt"]
    source.sm = SimpleNamespace(alive={"carState": True, "carStateExt": True},
                                valid={"carState": True, "carStateExt": True})
    source.vehicle_started = lambda: True
    source._service_data = lambda service: message
    assert "carStateExt" in LIVE_SERVICES_BASE
    assert source._auto_brake_hold_active() == active
    for service in source.services:
        for field in ("alive", "valid"):
            values = getattr(source.sm, field)
            values[service] = False
            assert not source._auto_brake_hold_active()
            values[service] = True
    for started in (False, None):
        source.vehicle_started = lambda started=started: started
        assert not source._auto_brake_hold_active()
    source.vehicle_started = lambda: True
    source.sm.alive.pop("carStateExt")
    assert not source._auto_brake_hold_active()


def test_hold_state_applies_without_debug_and_clears_immediately():
    source = OpenpilotLiveSource.__new__(OpenpilotLiveSource)
    source._hud_debug_mode = 0
    source._live_debug_enabled = False
    source._debug_plot_enabled = False
    source._auto_brake_hold_active = lambda: True
    state = source._with_debug_state(standby_state())
    assert state.auto_brake_hold_active
    source._auto_brake_hold_active = lambda: False
    assert not source._with_debug_state(state).auto_brake_hold_active


@pytest.mark.parametrize("now,color", [(0, RED), (0.499, RED), (0.5, GREEN), (0.999, GREEN), (1, RED)])
@pytest.mark.parametrize("compact", [False, True])
def test_hold_label_alternates_color_at_half_second(now, color, compact):
    tree = ast.parse((CLUSTER_DIR / "cluster_renderer.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ClusterUiRenderer")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_draw_auto_hold")
    calls = []
    matrices = []
    namespace = {
        "RED": RED, "GREEN": GREEN, "DESIGN_WIDTH": 1920, "DESIGN_HEIGHT": 720,
        "time": SimpleNamespace(monotonic=lambda: now),
        "rl": SimpleNamespace(
            rl_push_matrix=lambda: matrices.append("push"),
            rl_pop_matrix=lambda: matrices.append("pop"),
            rl_scalef=lambda *args: None,
        ),
    }
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), method,
    ], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<AUTO HOLD overlay>", "exec"), namespace)
    renderer = SimpleNamespace(width=1920, height=462, _compact_panel=compact, _panel_region=lambda region: nullcontext(),
                               _draw_text=lambda *args, **kwargs: calls.append((args, kwargs)))
    namespace["_draw_auto_hold"](renderer, SimpleNamespace(auto_brake_hold_active=False))
    assert calls == [] and matrices == []
    namespace["_draw_auto_hold"](renderer, SimpleNamespace(auto_brake_hold_active=True))
    assert calls == [(("AUTO HOLD", 960.0, 180.0 if compact else 230.0, 36.0, color), {"anchor": "center"})]
    assert matrices == ["push", "pop"]

import ast
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "cluster_renderer.py").read_text(encoding="utf-8"))
RENDERER = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "ClusterUiRenderer")


class Matrix:
    def __init__(self):
        self.stack = []
        self.transform = (1.0, 1.0, 0.0, 0.0)
        self.calls = []

    def rl_push_matrix(self):
        self.stack.append(self.transform)
        self.calls.append("push")

    def rl_pop_matrix(self):
        self.transform = self.stack.pop()
        self.calls.append("pop")

    def rl_translatef(self, x, y, z):
        sx, sy, tx, ty = self.transform
        self.transform = (sx, sy, tx + sx * x, ty + sy * y)

    def rl_scalef(self, x, y, z):
        sx, sy, tx, ty = self.transform
        self.transform = (sx * x, sy * y, tx, ty)

    def point(self, x, y):
        sx, sy, tx, ty = self.transform
        return sx * x + tx, sy * y + ty


def region_method(matrix):
    constants = [node for node in TREE.body if isinstance(node, ast.Assign)
                 and isinstance(node.targets[0], ast.Name)
                 and node.targets[0].id in ("SPEED_VALUE_CENTER_X", "SPEED_VALUE_CENTER_Y", "GEAR_STATUS_CENTER_X", "GEAR_STATUS_CENTER_Y")]
    method = next(node for node in RENDERER.body if isinstance(node, ast.FunctionDef) and node.name == "_panel_region")
    namespace = {"rl": matrix, "contextmanager": contextmanager, "DESIGN_HEIGHT": 720}
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *constants, method,
    ], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<panel layout>", "exec"), namespace)
    return namespace["_panel_region"]


@pytest.mark.parametrize("region,source,target,scale", [
    ("speed", (255, 350), (250, 240), 0.9),
    ("gear", (1615, 350), (1660, 240), 0.62),
    ("status", (960, 0), (960, 16), 0.7),
    ("top_status", (960, 0), (960, -14), 0.7),
])
def test_compact_native_centers_and_undistorted_sizes(region, source, target, scale):
    matrix = Matrix()
    matrix.rl_scalef(1, 462 / 720, 1)
    outer = matrix.transform
    with region_method(matrix)(SimpleNamespace(_compact_panel=True), region):
        assert matrix.point(*source) == pytest.approx(target)
        assert matrix.transform[:2] == pytest.approx((scale, scale))
    assert matrix.transform == outer
    assert matrix.stack == []


@pytest.mark.parametrize("region", ["speed", "gear", "status", "top_status"])
def test_large_panel_does_not_change_existing_matrix(region):
    matrix = Matrix()
    with region_method(matrix)(SimpleNamespace(_compact_panel=False), region):
        assert matrix.transform == (1, 1, 0, 0)
    assert matrix.calls == []


def test_compact_status_row_stays_between_side_panels_and_above_road():
    matrix = Matrix()
    matrix.rl_scalef(1, 462 / 720, 1)
    with region_method(matrix)(SimpleNamespace(_compact_panel=True), "top_status"):
        # Include the full turn-signal texture on each side and the detail row.
        left, _ = matrix.point(595 - 64, 94)
        right, bottom = matrix.point(1325 + 64, 190)
        assert left >= 580
        assert right <= 1340
        assert bottom <= 150
    with region_method(matrix)(SimpleNamespace(_compact_panel=True), "status"):
        # ACC-off wheel and steering text remain within the shorter panel.
        _, off_bottom = matrix.point(960, 600)
        assert off_bottom < 462


def test_compact_transform_cleanup_after_draw_error():
    matrix = Matrix()
    with pytest.raises(RuntimeError, match="draw failed"):
        with region_method(matrix)(SimpleNamespace(_compact_panel=True), "speed"):
            raise RuntimeError("draw failed")
    assert matrix.transform == (1, 1, 0, 0)
    assert matrix.stack == []


def test_compact_top_row_moves_thirty_pixels_without_moving_other_status():
    matrix = Matrix()
    matrix.rl_scalef(1, 462 / 720, 1)
    region = region_method(matrix)
    renderer = SimpleNamespace(_compact_panel=True)
    with region(renderer, "status"):
        original = matrix.point(960, 94)
    with region(renderer, "top_status"):
        moved = matrix.point(960, 94)
        _, top = matrix.point(960, 94 - 64)
    assert moved == pytest.approx((original[0], original[1] - 30))
    assert top >= 0
    calls = {}
    for method in RENDERER.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        for block in ast.walk(method):
            if not isinstance(block, ast.With):
                continue
            regions = [item.context_expr.args[0].value for item in block.items
                       if isinstance(item.context_expr, ast.Call)
                       and isinstance(item.context_expr.func, ast.Attribute)
                       and item.context_expr.func.attr == "_panel_region"]
            for call in ast.walk(block):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and regions:
                    calls.setdefault(call.func.attr, set()).update(regions)
    for name in ("_draw_turn_signal", "_draw_acc_status_icon", "_draw_follow_gap_lane_icon",
                 "_draw_lfa_status_icon", "_draw_drive_mode_icon", "_draw_chestnut_icon", "_draw_model_label"):
        assert calls[name] == {"top_status"}


@pytest.mark.parametrize("compact", [False, True])
def test_road_clipping_is_compact_only_and_cleans_up(compact):
    method = next(node for node in RENDERER.body if isinstance(node, ast.FunctionDef) and node.name == "_render_world")
    start = next(index for index, node in enumerate(method.body) if isinstance(node, ast.If)
                 and ast.unparse(node.test) == "self._compact_panel")
    nodes = method.body[start:start + 2]
    events = []
    rl = SimpleNamespace(
        begin_scissor_mode=lambda *bounds: events.append(bounds),
        end_scissor_mode=lambda: events.append("end"),
    )

    def draw(*args):
        events.append("draw")
        raise RuntimeError("draw failed")

    renderer = SimpleNamespace(_compact_panel=compact, width=1920, height=462, _draw_scene=draw)
    with pytest.raises(RuntimeError, match="draw failed"):
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<road clipping>", "exec"),
             {"self": renderer, "rl": rl, "scene": None, "state": None})
    assert events == ([(580, 150, 760, 312), "draw", "end"] if compact else ["draw"])


@pytest.mark.parametrize("compact", [False, True])
def test_side_metrics_move_inward_only_on_compact_panel(compact):
    method = next(n for n in RENDERER.body if isinstance(n, ast.FunctionDef) and n.name == "_draw_system_top_metrics")
    positions = [n for n in method.body if isinstance(n, ast.Assign)
                 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in ("left_x", "right_x")]
    namespace = {"self": SimpleNamespace(_compact_panel=compact),
                 "TOP_SYSTEM_METRIC_LEFT_X": 260, "TOP_SYSTEM_METRIC_RIGHT_X": 1660}
    exec(compile(ast.Module(body=positions, type_ignores=[]), "<metric positions>", "exec"), namespace)
    assert (namespace["left_x"], namespace["right_x"]) == ((310, 1610) if compact else (260, 1660))


@pytest.mark.parametrize("compact", [False, True])
def test_speed_and_gear_fonts_increase_exactly_twenty_percent_on_compact_panel(compact):
    speed = next(n for n in RENDERER.body if isinstance(n, ast.FunctionDef) and n.name == "_draw_speed_block")
    assignment = next(n for n in speed.body if isinstance(n, ast.Assign)
                      and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "speed_font_size")
    namespace = {"self": SimpleNamespace(_compact_panel=compact)}
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), "<speed font>", "exec"), namespace)
    assert namespace["speed_font_size"] == pytest.approx(198 if compact else 165)
    unit = next(n for n in speed.body if isinstance(n, ast.Assign)
                and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "unit_font_size")
    assert ast.literal_eval(unit.value) == 24

    gear = next(n for n in RENDERER.body if isinstance(n, ast.FunctionDef) and n.name == "_draw_drive_status")
    draw = next(n for n in ast.walk(gear) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "_draw_text")
    size = eval(compile(ast.Expression(body=draw.args[3]), "<gear font>", "eval"),
                {**namespace, "GEAR_STATUS_FONT_SIZE": 100})
    assert size == pytest.approx(120 if compact else 100)

import importlib.util
from pathlib import Path
import subprocess

import pytest


SPEC = importlib.util.spec_from_file_location("cluster_supervisor", Path(__file__).resolve().parents[2] / "cluster_autorun.py")
supervisor = importlib.util.module_from_spec(SPEC)
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

#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parent / "cluster"
# Like CarrotPilot, render on the Adreno GPU through comma-deps-raylib's DRM "comma"
# backend. In USB output mode the HUD only draws into render textures and never calls
# end_drawing()/SwapScreenBuffer, and the backend defers its modeset to the first swap,
# so it never takes the panel away from the openpilot UI. There is no CPU fallback:
# Mesa llvmpipe only reaches ~1-2 FPS on device, which is unusable.
RAYLIB_BACKEND = "comma"
DEFAULT_LOG_PATH = "/tmp/cluster.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
RESTART_DELAY_S = 5.0
DEFAULT_FPS = "20"
STARTUP_TIMEOUT_S = 90.0
HEARTBEAT_TIMEOUT_S = 30.0


def cluster_env() -> dict[str, str]:
    env = os.environ.copy()
    env["RAYLIB_BACKEND"] = RAYLIB_BACKEND
    return env


def cluster_fps() -> str:
    # Keep the autorun HUD at the same 20 FPS as the renderer/encoder defaults.
    return os.environ.get("CLUSTER_FPS", "").strip() or DEFAULT_FPS


def cluster_cmd() -> list[str]:
    return [
        sys.executable,
        str(CLUSTER_DIR / "main.py"),
        "--input",
        "live",
        "--fps",
        cluster_fps(),
        "--usb-codec",
        "h264",
        "--usb-h264-backend",
        "auto",
        "--theme",
        "auto",
    ]


def _open_log():
    log_path = os.environ.get("CLUSTER_LOG_PATH", "").strip() or DEFAULT_LOG_PATH
    path = Path(log_path)
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        return path.open("a", buffering=1)
    except OSError as exc:
        print(f"Unable to open cluster log {path}: {exc}", file=sys.stderr, flush=True)
        return None


def _log(log_file, message: str) -> None:
    line = f"[cluster_autorun {time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, file=log_file or sys.stderr, flush=True)


def stop_child(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def heartbeat_expired(path: Path, started: float, now: float) -> bool:
    try:
        last_progress = float(path.read_text(encoding="ascii"))
    except FileNotFoundError:
        return now - started >= STARTUP_TIMEOUT_S
    except ValueError:
        return now - started >= STARTUP_TIMEOUT_S
    return now - max(started, last_progress) >= HEARTBEAT_TIMEOUT_S


def main() -> None:
    # sunnypilot's manager never restarts a PythonProcess that exited, so supervise
    # the cluster here: restart on crash (e.g. screen not enumerated yet at boot).
    log_file = _open_log()
    env = cluster_env()
    child: subprocess.Popen | None = None
    stopping = False

    def _stop(signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    _log(log_file, f"starting cluster HUD with live openpilot data (raylib backend={env.get('RAYLIB_BACKEND')})")
    try:
        with tempfile.TemporaryDirectory(prefix="cluster-supervisor-") as temp_dir:
            heartbeat = Path(temp_dir) / "heartbeat"
            env["CLUSTER_HEARTBEAT_PATH"] = str(heartbeat)
            while not stopping:
                heartbeat.unlink(missing_ok=True)
                child = subprocess.Popen(cluster_cmd(), env=env, stdout=log_file, stderr=subprocess.STDOUT)
                started = time.monotonic()
                while not stopping and child.poll() is None:
                    if heartbeat_expired(heartbeat, started, time.monotonic()):
                        _log(log_file, "HUD progress timed out; terminating and restarting")
                        break
                    if log_file is not None and os.fstat(log_file.fileno()).st_size > LOG_MAX_BYTES:
                        log_file.seek(0)
                        log_file.truncate()
                        _log(log_file, "log size limit reached; older output discarded")
                    time.sleep(0.5)
                stop_child(child)
                if stopping:
                    break
                _log(log_file, f"cluster exited with code {child.returncode}, restarting in {RESTART_DELAY_S:.0f}s")
                deadline = time.monotonic() + RESTART_DELAY_S
                while not stopping and time.monotonic() < deadline:
                    time.sleep(0.2)
    finally:
        if child is not None:
            stop_child(child)
        _log(log_file, "stopped")
        if log_file is not None:
            log_file.close()


if __name__ == "__main__":
    main()

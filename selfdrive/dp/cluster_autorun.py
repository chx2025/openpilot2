#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parent / "cluster"
# Like CarrotPilot, render on the Adreno GPU through comma-deps-raylib's DRM "comma"
# backend. In USB output mode the HUD only draws into render textures and never calls
# end_drawing()/SwapScreenBuffer, and the backend defers its modeset to the first swap,
# so it never takes the panel away from the openpilot UI. There is no CPU fallback:
# Mesa llvmpipe only reaches ~1-2 FPS on device, which is unusable.
RAYLIB_BACKEND = "comma"
# Logging is off by default; set CLUSTER_LOG_PATH=/tmp/cluster.log to debug.
LOG_MAX_BYTES = 5 * 1024 * 1024
RESTART_DELAY_S = 5.0
DEFAULT_FPS = "20"


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
    log_path = os.environ.get("CLUSTER_LOG_PATH", "").strip()
    if not log_path:
        return None
    path = Path(log_path)
    try:
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        return path.open("a", buffering=1)
    except OSError:
        return None


def _log(log_file, message: str) -> None:
    if log_file is None:
        return
    line = f"[cluster_autorun {time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    log_file.write(line + "\n")


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
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    _log(log_file, f"starting cluster HUD with live openpilot data (raylib backend={env.get('RAYLIB_BACKEND')})")
    while not stopping:
        output = log_file if log_file is not None else subprocess.DEVNULL
        child = subprocess.Popen(cluster_cmd(), env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            returncode = child.wait()
        except KeyboardInterrupt:
            stopping = True
            child.terminate()
            returncode = child.wait()
        if stopping:
            break
        _log(log_file, f"cluster exited with code {returncode}, restarting in {RESTART_DELAY_S:.0f}s")
        deadline = time.monotonic() + RESTART_DELAY_S
        while not stopping and time.monotonic() < deadline:
            time.sleep(0.2)

    if child is not None and child.poll() is None:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
    _log(log_file, "stopped")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
dp - eGPU model manager daemon (egpu_manager).

Runs always (1 Hz):
  * dp_egpu_present: the eGPU on USB (sunnypilot reads deviceState.chestnutPresent, dp's cereal has none)
  * offroad only:
    - catalog: shared with jetlink (JetlinkCatalog via jetlink_adapter.refresh_catalog: sunnypilot's chestnut
      catalogs + jetlink's extra list, ONNX pointers resolved), refreshed hourly and on request
    - dp_egpu_download_ref: ref -> ONNX pointer -> ONNX -> compile -> dp_egpu_bundle
        pointer & download: jetlink's registry (jetlink_repo/jetlink/registry/lfs.py), ONNX kept in jetlink's
        folder so both share one copy; a download resumes (.part) and is sha256-checked by jetlink
        compile: dragonpilot/egpu/compile.py in its own process with the vendored tinygrad; warps once per
        camera on QCOM, the model on the eGPU (needs it plugged in, else waits)
    - dp_egpu_delete_ref: delete one model (its compiled pkl and its ONNX)
    - dp_egpu_clear_cache: delete every model but the active one
  * going onroad aborts a download / compile; what is verified on disk stays and resumes next time

Status for the UI in dp_egpu_status (sunnypilot uses the modelManagerSP service; dp's cereal has none).
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper
from openpilot.common.swaglog import cloudlog
from dragonpilot.egpu.bundles import (cached_bundles, find_bundle, get_active_bundle, pointer_for, onnx_present, model_compiled,
                                      warps_present)
from dragonpilot.egpu.constants import (MANIFEST_CACHE_TIMEOUT_S, CAMERA_CONFIGS, CACHE_TAG, TG_FLAGS_QCOM, TG_FLAGS_AMD, EGPU_DIR,
                                        P_PRESENT, P_ACTIVE_BUNDLE, P_MANIFEST_SYNC, P_POINTERS, P_DOWNLOAD_REF, P_FAILED, P_LOADING, P_RUNNING,
                                        P_STATUS, P_DELETE_REF, P_CLEAR_CACHE, P_ENABLED, P_SEEN, P_AUTO_DEFAULT, DEFAULT_REF, AUTO_RETRY_S,
                                        ST_IDLE, ST_RESOLVING, ST_DOWNLOADING,
                                        ST_COMPILING_WARP, ST_COMPILING, ST_DONE, ST_FAILED, ST_CANCELLED, ST_WAITING_EGPU,
                                        FIRST_OUTPUT_TIMEOUT, MODEL_OUTPUT_TIMEOUT, model_root, onnx_path, model_pkl_name, warp_pkl_name)
from dragonpilot.egpu.usb import egpu_present, egpu_rom_present, wait_pcie_link

BASEDIR = EGPU_DIR.parent.parent
COMPILE_TIMEOUT_S = 3 * 3600


class Interrupted(Exception):
  pass


class EgpuModelManager:
  def __init__(self, params: Params | None = None):
    self.params = params or Params()
    self.bundles: list[dict] = []
    self._status: dict = {"state": ST_IDLE}
    self._last_status_put = 0.0
    self._next_fetch = 0.0
    self._last_sync = None
    self._ref: str | None = None
    self._rom_logged = False
    self._auto_next = 0.0
    self.compile_python = sys.executable
    self.sm = messaging.SubMaster(["modelV2", "modelExt"])
    self._model_wait_started: float | None = None
    self._last_model_output: float | None = None

  def check_model_progress(self) -> None:
    self.sm.update(0)
    loading = self.params.get_bool(P_LOADING)
    running = self.params.get_bool(P_RUNNING)
    if self.params.get_bool(P_FAILED) or not (loading or running):
      self._model_wait_started = None
      self._last_model_output = None
      return

    now = time.monotonic()
    if self._model_wait_started is None:
      self._model_wait_started = now
    if running:
      if self._last_model_output is None:
        self._last_model_output = now
      if self.sm["modelExt"].bigModel:
        output_ts = min(self.sm.logMonoTime["modelV2"], self.sm.logMonoTime["modelExt"]) / 1e9
        self._last_model_output = max(self._last_model_output, output_ts)
      expired = now - self._last_model_output >= MODEL_OUTPUT_TIMEOUT
      reason = f"no big-model output for {MODEL_OUTPUT_TIMEOUT}s"
    else:
      expired = now - self._model_wait_started >= FIRST_OUTPUT_TIMEOUT
      reason = f"no first big-model output within {FIRST_OUTPUT_TIMEOUT}s"
    if expired:
      cloudlog.error(f"egpu: {reason}; failing this drive for stock fallback")
      self.params.put_bool(P_FAILED, True, block=True)

  # ------------------------------------------------------------------ status
  def _set_status(self, force: bool = False, **kw) -> None:
    self._status = {**self._status, **kw}
    now = time.monotonic()
    if force or now - self._last_status_put > 0.5:
      self._last_status_put = now
      self.params.put(P_STATUS, self._status)

  # ------------------------------------------------------------------ catalog
  def _cache_expired(self) -> bool:
    last = self.params.get(P_MANIFEST_SYNC) or 0
    return last == 0 or (time.monotonic_ns() - last) >= MANIFEST_CACHE_TIMEOUT_S * 1e9

  @staticmethod
  def fetch_catalog(params: Params) -> int:
    """the catalog shared with jetlink: jetlink_adapter.refresh_catalog fills JetlinkCatalog and resolves every
    model's ONNX pointer into JetlinkModelPointers (the Jetlink panel's refresh does the same)"""
    from dragonpilot import jetlink_adapter
    return jetlink_adapter.refresh_catalog(params, resolve=True)

  def refresh_catalog(self) -> None:
    sync = self.params.get(P_MANIFEST_SYNC) or 0
    if sync == 0 and self._last_sync:
      self._next_fetch = 0.0  # the UI asked for a refresh (zeroed the stamp)
    self._last_sync = sync
    if self._cache_expired() and time.monotonic() >= self._next_fetch:
      try:
        n = self.fetch_catalog(self.params)
        self.params.put(P_MANIFEST_SYNC, int(time.monotonic_ns()), block=True)
        self._last_sync = None
        cloudlog.warning(f"egpu: catalog refreshed, {n} models")
      except Exception as e:
        cloudlog.warning(f"egpu: catalog refresh failed: {e}")
        self._next_fetch = time.monotonic() + 30.0
    self.bundles = cached_bundles(self.params)  # also picks up a refresh made from the Jetlink panel

  # ------------------------------------------------------------------ pipeline
  def _interrupted(self) -> bool:
    # removing the ref cancels (UI); going onroad aborts so the drive gets the eGPU, CPU and IO
    return self.params.get(P_DOWNLOAD_REF) != self._ref or self.params.get_bool("IsOnroad")

  def _check(self) -> None:
    if self._interrupted():
      raise Interrupted()

  def _remember_pointer(self, ref: str, pointer: dict) -> None:
    cache = self.params.get(P_POINTERS)
    cache = cache if isinstance(cache, dict) else {}
    if cache.get(ref) != pointer:
      cache[ref] = pointer
      self.params.put(P_POINTERS, cache)

  def resolve(self, bundle: dict) -> dict:
    if (p := pointer_for(self.params, bundle["ref"])) is not None:
      self._remember_pointer(bundle["ref"], p)
      return p
    self._set_status(force=True, state=ST_RESOLVING)
    from jetlink.registry import lfs
    ptr = lfs.fetch_pointer(bundle["ref"])
    p = {"oid": ptr.oid, "size": int(ptr.size)}
    self._remember_pointer(bundle["ref"], p)
    return p

  def download_onnx(self, pointer: dict) -> Path:
    dest = onnx_path(pointer["oid"])
    if onnx_present(pointer):
      return dest
    from jetlink.registry import lfs
    ptr = lfs.Pointer(pointer["oid"], int(pointer["size"]))
    self._set_status(force=True, state=ST_DOWNLOADING, progress=0.0, size_mb=round(ptr.size / 1e6))
    errors = []
    for endpoint in lfs.LFS_ENDPOINTS:
      self._check()
      href = lfs.lfs_resolve(endpoint, ptr)
      if not href:
        errors.append(endpoint.split('/')[2])
        continue
      try:
        lfs.lfs_download(href, ptr, dest, progress=lambda f: self._set_status(state=ST_DOWNLOADING, progress=round(100 * f, 1)),
                         should_stop=self._interrupted)
        return dest
      except lfs.RegistryError:
        self._check()  # a stop surfaces as Interrupted, not as a failure
        raise
    raise RuntimeError(f"no LFS server has {pointer['oid'][:16]} ({', '.join(errors)})")

  def _run_compile(self, args: list[str], flags: dict, state: str, what: str) -> None:
    root = model_root()
    os.makedirs(root, exist_ok=True)
    env = {**os.environ, **flags, "PYTHONPATH": str(BASEDIR)}
    logf = open(os.path.join(root, "compile.log"), "ab")
    logf.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} {what}: {' '.join(args)}\n".encode())
    logf.flush()
    started = time.monotonic()
    self._set_status(force=True, state=state, detail=what, progress=0.0)
    proc = subprocess.Popen([self.compile_python, str(EGPU_DIR / "compile.py"), *args], cwd=root, env=env,
                            stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    try:
      while proc.poll() is None:
        if self._interrupted() or time.monotonic() - started > COMPILE_TIMEOUT_S:
          os.killpg(proc.pid, signal.SIGKILL)
          proc.wait()
          self._check()
          raise RuntimeError(f"{what}: timeout")
        self._set_status(state=state, detail=what, elapsed=int(time.monotonic() - started))
        time.sleep(0.5)
    finally:
      logf.close()
    if proc.returncode != 0:
      raise RuntimeError(f"{what} failed (exit {proc.returncode}), see {os.path.join(root, 'compile.log')}")

  def compile_warps(self) -> None:
    for cam_w, cam_h in CAMERA_CONFIGS:
      out = os.path.join(model_root(), warp_pkl_name(cam_w, cam_h))
      if not os.path.isfile(out):
        self._run_compile(["warp", "--cam", f"{cam_w}x{cam_h}", "--out", out], TG_FLAGS_QCOM, ST_COMPILING_WARP, f"warp {cam_w}x{cam_h}")

  def compile_model(self, pointer: dict, onnx: Path) -> None:
    out = os.path.join(model_root(), model_pkl_name(pointer["oid"]))
    if not os.path.isfile(out):
      self._run_compile(["model", "--onnx", str(onnx), "--out", out], TG_FLAGS_AMD, ST_COMPILING, "model")

  def prepare(self, bundle: dict) -> None:
    """ref -> compiled and active. Every step is skipped when its result is already on disk."""
    self._status = {"state": ST_RESOLVING, "ref": bundle["ref"], "name": bundle["short_name"]}
    self._set_status(force=True)
    try:
      pointer = self.resolve(bundle)
      self._check()
      onnx = self.download_onnx(pointer)
      self._check()
      if not (model_compiled(pointer["oid"]) and warps_present()):
        if not egpu_present():
          # keep the ref: the compile starts as soon as the eGPU is plugged in (offroad)
          self._set_status(force=True, state=ST_WAITING_EGPU)
          return
        self.compile_warps()
        self._check()
        if not model_compiled(pointer["oid"]):
          up, ltssm = wait_pcie_link(15.0, should_stop=self._interrupted)
          self._check()
          if not up:
            # the bridge is on USB but the GPU behind it is not (12 V / its own supply): wait, as for no eGPU
            self._set_status(force=True, state=ST_WAITING_EGPU, error=f"PCIe LTSSM=0x{ltssm:02X}")
            return
        self.compile_model(pointer, onnx)
        self._check()
      self.params.put(P_ACTIVE_BUNDLE, {**bundle, "sha256": pointer["oid"]}, block=True)
      self._set_status(force=True, state=ST_DONE, progress=100.0)
      cloudlog.warning(f"egpu: {bundle['short_name']} ready ({pointer['oid'][:16]})")
      self._finish()
    except Interrupted:
      self._set_status(force=True, state=ST_CANCELLED)
    except Exception as e:
      cloudlog.exception(f"egpu: preparing {bundle['short_name']} failed")
      self._set_status(force=True, state=ST_FAILED, error=str(e)[:300])
      self._finish()

  def _finish(self) -> None:
    if self.params.get(P_DOWNLOAD_REF) == self._ref:
      self.params.remove(P_DOWNLOAD_REF)

  def process_download_request(self) -> None:
    ref = self.params.get(P_DOWNLOAD_REF)
    if ref is None:
      return
    bundle = find_bundle(self.bundles, ref)
    if bundle is None:
      self.params.remove(P_DOWNLOAD_REF)
      self._set_status(force=True, state=ST_FAILED, ref=ref, name=ref[:8], error="not in the catalog")
      return
    self._ref = ref
    try:
      self.prepare(bundle)
    finally:
      self._ref = None
      self._auto_result(ref)

  # ------------------------------------------------------------------ default model (plan B)
  def auto_default(self) -> None:
    """First boot with an eGPU on USB: queue the default big model (CTV3) once. Never for a comma without an eGPU
    (dp_egpu_enabled defaults on, and 766 MB nobody asked for is not on); never again once it is ready, or once
    the user picked, declined (不使用大模型) or deleted a model in the eGPU panel (which clears P_AUTO_DEFAULT)."""
    if not self.params.get_bool(P_AUTO_DEFAULT) or not self.params.get_bool(P_ENABLED):
      return
    if get_active_bundle(self.params) is not None:
      self.params.put_bool(P_AUTO_DEFAULT, False)
      return
    if self.params.get(P_DOWNLOAD_REF) is not None or time.monotonic() < self._auto_next or not egpu_present():
      return
    cloudlog.warning(f"egpu: eGPU on USB and no big model yet, getting the default {DEFAULT_REF[:10]}")
    self.params.put(P_DOWNLOAD_REF, DEFAULT_REF)

  def _auto_result(self, ref: str) -> None:
    if ref != DEFAULT_REF or not self.params.get_bool(P_AUTO_DEFAULT):
      return
    state = self._status.get("state")
    if state == ST_DONE:
      self.params.put_bool(P_AUTO_DEFAULT, False)
    elif state == ST_FAILED:
      self._auto_next = time.monotonic() + AUTO_RETRY_S

  # ------------------------------------------------------------------ delete / cache
  def _model_files(self, ref: str) -> tuple[Path | None, Path | None]:
    p = pointer_for(self.params, ref)
    if p is None:
      return None, None
    return Path(model_root()) / model_pkl_name(p["oid"]), onnx_path(p["oid"])

  def delete_model(self, ref: str) -> None:
    active = get_active_bundle(self.params)
    if active and active["ref"] == ref:
      self.params.remove(P_ACTIVE_BUNDLE)
    for f in self._model_files(ref):
      if f is not None:
        for g in (f, f.with_name(f.name + ".part"), f.with_name(f.name + ".tmp")):
          g.unlink(missing_ok=True)
    cloudlog.warning(f"egpu: deleted model {ref[:10]}")

  def clear_cache(self) -> None:
    """every model but the active one: compiled pkls in egpu/, and the ONNX of models egpu resolved. The
    warps stay (model-independent, re-made only by a tinygrad update)."""
    active = get_active_bundle(self.params)
    keep_sha = active["sha256"] if active else None
    root = Path(model_root())
    if root.is_dir():
      for f in root.iterdir():
        if f.name.startswith("warp_") and f.name.endswith(f"_{CACHE_TAG}.pkl"):
          continue  # the current tinygrad's warps
        if keep_sha and f.name == model_pkl_name(keep_sha):
          continue
        if f.is_file() and f.name != "compile.log":
          f.unlink(missing_ok=True)
    pointers = self.params.get(P_POINTERS)
    for p in (pointers.values() if isinstance(pointers, dict) else []):
      if isinstance(p, dict) and p.get("oid") and p["oid"] != keep_sha:
        onnx_path(p["oid"]).unlink(missing_ok=True)
    cloudlog.warning("egpu: model cache cleared")

  def validate_active(self) -> None:
    """Drop the active model if its files are gone (deleted by hand, or a tinygrad update changed the cache key)."""
    active = get_active_bundle(self.params)
    if self.params.get(P_ACTIVE_BUNDLE) is not None and active is None:
      self.params.remove(P_ACTIVE_BUNDLE)
    elif active is not None and not model_compiled(active["sha256"]):
      cloudlog.warning(f"egpu: active model {active['short_name']} not compiled for this tinygrad, recompiling")
      self.params.remove(P_ACTIVE_BUNDLE)
      if self.params.get(P_DOWNLOAD_REF) is None:
        self.params.put(P_DOWNLOAD_REF, active["ref"])

  # ------------------------------------------------------------------ loop
  def tick(self) -> None:
    present = egpu_present()
    if present != self.params.get_bool(P_PRESENT):
      self.params.put_bool(P_PRESENT, present)
      cloudlog.warning(f"egpu: {'eGPU on USB' if present else 'no eGPU on USB'}")
    rom = not present and egpu_rom_present()
    if rom and not self._rom_logged:
      cloudlog.error("egpu: an ASM2464 with factory/ROM firmware (174C:2464/2463) is on USB; it needs comma or UT3G firmware first")
    self._rom_logged = rom
    if (present or rom) and not self.params.get_bool(P_SEEN):
      # from now on jetlinkd is not started beside the eGPU switch (jetlink_adapter.should_run)
      self.params.put_bool(P_SEEN, True)

    if self.params.get_bool("IsOnroad"):
      if not present and (self.params.get_bool(P_LOADING) or self.params.get_bool(P_RUNNING)) and not self.params.get_bool(P_FAILED):
        cloudlog.error("egpu: USB disappeared while the big model was loading/running; failing this drive for stock fallback")
        self.params.put_bool(P_FAILED, True, block=True)
      self.check_model_progress()
      return

    self._model_wait_started = None
    self._last_model_output = None
    self.refresh_catalog()
    self.validate_active()
    if (ref := self.params.get(P_DELETE_REF)) is not None:
      self.params.remove(P_DELETE_REF)
      if self.params.get(P_DOWNLOAD_REF) == ref:
        self.params.remove(P_DOWNLOAD_REF)
      self.delete_model(ref)
    if self.params.get_bool(P_CLEAR_CACHE):
      self.clear_cache()
      self.params.remove(P_CLEAR_CACHE)
    self.auto_default()
    self.process_download_request()


def main():
  params = Params()
  params.put(P_STATUS, {"state": ST_IDLE})
  mgr = EgpuModelManager(params)
  rk = Ratekeeper(1, print_delay_threshold=None)
  while True:
    try:
      mgr.tick()
    except Exception:
      cloudlog.exception("egpu_manager tick failed")
    rk.keep_time()


if __name__ == "__main__":
  main()

#!/usr/bin/env python3
"""
dp - modeld_egpu: dp's modeld main loop driving a big model compiled on this device for the eGPU.

Started by manager as a NativeProcess (cwd dragonpilot/egpu, `<manager python> ./modeld_egpu.py`), i.e. a
fresh interpreter: manager preimports every PythonProcess module, so a forked one would inherit dp's
tinygrad from tinygrad_repo. Here nothing is imported yet, so tg_env can put the vendored sunnypilot
tinygrad (tinygrad_spm/, the one compile.py used) first on sys.path.

Failure handling (sunnypilot: "big model unavailable until the next drive"): a load that fails or
exceeds BIG_MODEL_TIMEOUT, or any exception while driving, sets dp_egpu_failed and exits. manager's
route (dragonpilot/egpu/bundles.py EgpuRoute) then starts dp's stock modeld for the rest of the drive.
"""
import tg_env  # first: vendored tinygrad on sys.path (this script's dir is sys.path[0])

import os
import threading
import time

tg_env.check()
os.environ.setdefault('HCQDEV_WAIT_TIMEOUT_MS', '3000')  # sunnypilot modeld with a chestnut present (no-op on this tinygrad)
# UT3G author's sp branch (sunnypilot/modeld_v2/egpu_loader.py configure_default_device): keep tinygrad's
# default-device scan off the USB AMD GPU; every tensor here names its device anyway
os.environ.setdefault('DEV', 'QCOM')
# openpilot #38992: a 100 W power limit for the chestnut, set by tinygrad when it boots the device, which then
# leaves the clocks to the SMU (~1.2 GHz / ~31 W / ~5 A for the big model). dp: AmyJeanes/openpilot-upstream@e9fa764
# (pin the top clocks under that limit: ~37 ms instead of ~42 ms, but ~12 A bursts at the chestnut's input) is
# deliberately NOT applied: a car without a high-voltage system cannot supply it. Without this variable at all
# tinygrad pins the top clocks with no limit, the highest draw of the three.
os.environ.setdefault('AM_POWER_LIMIT', '100')

from setproctitle import setproctitle
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from dragonpilot.egpu.bundles import get_active_bundle, active_ready
from dragonpilot.egpu.constants import P_LOADING, P_RUNNING, P_FAILED, BIG_MODEL_TIMEOUT, PCIE_WAIT, model_root, model_pkl_name, warp_pkl_name

PROCESS_NAME = "dragonpilot.egpu.modeld_egpu"
# s. sunnypilot: 60. The UT3G author measured 71.5-75.6 s on a C3XL with a UT3G (sp egpu_loader.py
# C3XL_MODEL_LOAD_TIMEOUT = 120, "bounded 44.42 s margin for cold starts and USB scheduling variance")


def _fail(params: Params, why: str) -> None:
  cloudlog.exception(f"egpu: big model failed: {why}")
  params.put_bool(P_FAILED, True, block=True)
  params.put_bool(P_RUNNING, False)
  params.put_bool(P_LOADING, False)


def make_loader(params: Params):
  def loader(cam_w: int, cam_h: int):
    from dragonpilot.egpu.runner import EgpuModelState
    bundle = get_active_bundle(params)
    if not active_ready(bundle):
      raise RuntimeError("no compiled eGPU model")
    model_pkl = os.path.join(model_root(), model_pkl_name(bundle["sha256"]))
    warp_pkl = os.path.join(model_root(), warp_pkl_name(cam_w, cam_h))
    tg_env.patch_fetch_fw()

    from dragonpilot.egpu.usb import wait_pcie_link
    up, ltssm = wait_pcie_link(PCIE_WAIT)
    if not up:
      raise RuntimeError(f"eGPU PCIe link not up after {PCIE_WAIT}s (LTSSM=0x{ltssm:02X})")
    result: dict = {}

    def load():
      try:
        st = time.monotonic()
        m = EgpuModelState(cam_w, cam_h, bundle, model_pkl, warp_pkl)
        m.warmup()
        result['model'] = m
        cloudlog.warning(f"egpu: {bundle['short_name']} ({m.layout}) loaded in {time.monotonic() - st:.1f}s")
      except Exception as e:
        cloudlog.exception("egpu: big model load failed")
        result['error'] = e

    t = threading.Thread(target=load, daemon=True)
    t.start()
    t.join(BIG_MODEL_TIMEOUT)
    if 'model' not in result:
      raise RuntimeError(f"load failed: {result.get('error', f'timeout {BIG_MODEL_TIMEOUT}s')}")

    return result['model']
  return loader


def main():
  setproctitle(PROCESS_NAME)
  cloudlog.bind(daemon="modeld_egpu")
  params = Params()
  params.put_bool(P_RUNNING, False)
  params.put_bool(P_LOADING, True, block=True)
  try:
    from openpilot.selfdrive.modeld import modeld as dp_modeld
    dp_modeld.main(egpu_loader=make_loader(params))
  except KeyboardInterrupt:
    cloudlog.warning("modeld_egpu got SIGINT")
    params.put_bool(P_RUNNING, False)
    params.put_bool(P_LOADING, False)
  except BaseException as e:
    _fail(params, repr(e))
    raise


if __name__ == "__main__":
  main()

"""
dp - EgpuModelState: runs a model compiled on this device by dragonpilot/egpu/compile.py. Import ONLY
from modeld_egpu.py (tinygrad here must be the vendored sunnypilot tinygrad, see tg_env.py).

Per frame:
  1. warp, on QCOM: each camera buffer handed in place (Tensor.from_blob, cached per ring-buffer slot),
     transform on the host -> one (6, H, W) uint8 crop per camera
  2. the crops and the scalars (desire pulse, traffic convention, action_t, and for a queued graph the
     fed-back hidden state) go into one host buffer, uploaded to the eGPU in one copy: only ~0.4 MB
     crosses USB, not the 2 x 3.5 MB NV12 frames
  3. the model JIT on the eGPU; its history (stateful: state_* <- next_state_*, aliased in place as in
     sunnypilot-master selfdrive/modeld/modeld.py; queued: the wrapped JIT's own queues) stays there

Ported from: sunnypilot-master selfdrive/modeld/modeld.py ModelState (input_view, pack_inputs, state
aliasing, run, warmup), AmyJeanes/sunnypilot@173d90b (from_blob frames on the warp device, prev_feat
feedback, legacy-format handling replaced by on-device compile), sunnypilot modeld_v2 get_action_from_model.
"""
import base64
import math
import pickle

import numpy as np
from tinygrad.tensor import Tensor
from tinygrad.device import Buffer, Device
from tinygrad.dtype import _from_np_dtype
from tinygrad.helpers import round_up
from tinygrad.uop.ops import UOp
from examples.openpilot.helpers import load_pickle

from cereal import log
from openpilot.common.swaglog import cloudlog
from openpilot.system.camerad.cameras.nv12_info import get_nv12_info
from openpilot.selfdrive.controls.lib.drive_helpers import get_accel_from_plan, get_curvature_from_plan, smooth_value
from openpilot.selfdrive.modeld.constants import ModelConstants, Plan
from openpilot.selfdrive.modeld.parse_model_outputs import Parser

STATEFUL_FRAME = 'new_img'
DRIVING_OUTPUT = 'outputs'
VISION_INPUTS = ('img', 'big_img')
MIN_LAT_CONTROL_SPEED = 0.3


def input_view(buffer: Buffer, shape: tuple[int, ...], dtype, offset: int) -> Tensor:
  view = buffer.view(math.prod(shape), dtype, offset).ensure_allocated()
  return Tensor(UOp.from_buffer(view)).reshape(shape)


def output_slices(metadata: dict) -> dict[str, slice]:
  props = metadata.get('metadata', {})
  return pickle.loads(base64.b64decode(props['output_slices']))


class EgpuModelState:
  # read by dp's modeld main loop: chestnut -> modelExt.bigModel; jetlink hooks it sets each frame
  chestnut = True
  handovers = 0

  def __init__(self, cam_w: int, cam_h: int, bundle: dict, model_pkl: str, warp_pkl: str):
    self.bundle = bundle
    self.generation = int(bundle.get("generation", 0))
    overrides = bundle.get("overrides", {})
    # per-bundle smoothing, as sunnypilot's ModelState reads its overrides
    self.LAT_SMOOTH_SECONDS = float(overrides.get('lat', ".0"))
    self.LONG_SMOOTH_SECONDS = float(overrides.get('long', ".0"))

    with open(warp_pkl, 'rb') as f:
      warp = pickle.load(f)
    self.run_warp = warp['run']
    self.warp_device = warp['input_specs']['input_frame'][2]
    self.frame_size = get_nv12_info(cam_w, cam_h)[3]
    if warp['input_specs']['input_frame'][0] != (self.frame_size,):
      raise RuntimeError(f"warp {warp_pkl} is for another camera than {cam_w}x{cam_h}")

    cloudlog.warning(f"egpu: loading {model_pkl}")
    with open(model_pkl, 'rb') as f:
      art = load_pickle(f, out_of_band=True)
    self.layout = art['layout']
    self.run_model = art['run']
    self.specs = {name: (tuple(shape), np.dtype(dtype), device) for name, (shape, dtype, device) in art['input_specs'].items()}
    self.model_device = self.specs[STATEFUL_FRAME][2]
    self.state_names = [n for n in art['state_inputs'] if n in self.specs]
    self.host_names = [n for n in self.specs if n not in self.state_names]
    self.output_slices = output_slices(art['metadata'])
    self.hidden = self.output_slices.get('hidden_state')
    if self.layout == 'queued' and self.hidden is None:
      raise RuntimeError("queued graph without a hidden_state output to feed back")
    warped_shape = self.specs[STATEFUL_FRAME][0]
    mw, mh = warp.get('metadata', {}).get('model_size', (0, 0))
    if (6, mh // 2, mw // 2) != tuple(warped_shape[1:]):
      raise RuntimeError(f"warp makes (6, {mh // 2}, {mw // 2}), the model takes {warped_shape}")
    cloudlog.warning(f"egpu: layout={self.layout} model on {self.model_device}, warp on {self.warp_device}, " +
                     f"new_img {warped_shape}, host inputs {self.host_names}, state {self.state_names}")

    self.vision_input_names = list(VISION_INPUTS)
    self._blob_cache: dict = {}
    self.tfm = {k: np.zeros((3, 3), dtype=np.float32) for k in VISION_INPUTS}
    self.tfm_t = {k: Tensor(self.tfm[k], device='NPY') for k in VISION_INPUTS}
    self._pack()

    self.outputs = {name: Tensor(np.zeros(shape, dtype=dtype), device=device).realize()
                    for name, (shape, dtype, device) in art['output_specs'].items()}
    for name in self.state_names:
      if (nxt := 'next_' + name) in self.outputs:
        state = self.state[name]
        self.outputs[nxt] = input_view(state._buffer(), state.shape, state.dtype, 0)
    self.parser = Parser()
    self.prev_desire = np.zeros(ModelConstants.DESIRE_LEN, dtype=np.float32)
    self.stage = "initialized"
    self._warming_up = True
    self._first_frame = True

  def _pack(self) -> None:
    """one host buffer -> one upload per frame (sunnypilot-master pack_inputs), views as the JIT's inputs"""
    self.state = {n: Tensor(np.zeros(self.specs[n][0], dtype=self.specs[n][1]), device=self.model_device).realize() for n in self.state_names}
    offsets, size = {}, 0
    for n in self.host_names:
      shape, dtype, _ = self.specs[n]
      offsets[n] = size
      size += round_up(math.prod(shape) * dtype.itemsize, 128)
    self.packed = np.zeros(size, dtype=np.uint8)
    self.input_host = Tensor(self.packed, device='NPY')._buffer()
    self.input_device = Tensor(self.packed, device=self.model_device)._buffer()
    self.npy, self.inputs = {}, {}
    for n in self.host_names:
      shape, dtype, _ = self.specs[n]
      self.npy[n] = np.ndarray(shape, dtype=dtype, buffer=self.packed, offset=offsets[n])
      self.inputs[n] = input_view(self.input_device, shape, _from_np_dtype(dtype), offsets[n])
    self.inputs.update(self.state)

  def run(self, bufs, transforms: dict[str, np.ndarray], inputs: dict[str, np.ndarray], prepare_only: bool = False) -> dict[str, np.ndarray]:
    try:
      result = self._run(bufs, transforms, inputs)
    except Exception:
      cloudlog.exception(f"egpu: inference failed during {self.stage} (warmup={self._warming_up})")
      raise
    if not self._warming_up:
      self._first_frame = False
    return result

  def _set_stage(self, stage: str) -> None:
    self.stage = stage
    if not self._warming_up and self._first_frame:
      cloudlog.warning(f"egpu: first camera frame: {stage}")

  def _run(self, bufs, transforms: dict[str, np.ndarray], inputs: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    # prepare_only ignored: history lives in the JIT, every frame must go through it (as upstream's newer modeld)
    for i, key in enumerate(VISION_INPUTS):
      self._set_stage(f"{key} camera mapping")
      data = bufs[key].data if hasattr(bufs[key], 'data') else bufs[key]
      ptr = np.frombuffer(data, dtype=np.uint8).ctypes.data
      if (key, ptr) not in self._blob_cache:  # ring buffer of camera buffers: one tensor per slot
        self._blob_cache[(key, ptr)] = Tensor.from_blob(ptr, (self.frame_size,), dtype='uint8', device=self.warp_device)
      self.tfm[key][:, :] = transforms[key].reshape(3, 3)
      self._set_stage(f"{key} QCOM warp")
      warped = self.run_warp(input_frame=self._blob_cache[(key, ptr)], M_inv=self.tfm_t[key])
      self._set_stage(f"{key} warp readback")
      self.npy[STATEFUL_FRAME][i] = warped.numpy().reshape(self.npy[STATEFUL_FRAME].shape[1:])

    # desire is a pulse on the rising edge, the model decides when the action is complete
    inputs['desire_pulse'][0] = 0
    pulse = np.where(inputs['desire_pulse'] - self.prev_desire > .99, inputs['desire_pulse'], 0)
    self.prev_desire[:] = inputs['desire_pulse']
    self.npy['desire'][...] = pulse.reshape(self.npy['desire'].shape)
    self.npy['traffic_convention'][...] = np.asarray(inputs['traffic_convention']).reshape(self.npy['traffic_convention'].shape)
    self.npy['action_t'][...] = np.asarray(inputs['action_t']).reshape(self.npy['action_t'].shape)

    self._set_stage("AMD input upload")
    self.input_device.copy_from(self.input_host)
    self._set_stage("AMD inference")
    self.run_model(output_buffers=self.outputs, **self.inputs)

    self._set_stage("AMD output readback")
    model_output = self.outputs[DRIVING_OUTPUT].numpy().reshape(-1)
    if not np.all(np.isfinite(model_output)):
      raise RuntimeError("egpu: model output not finite")
    if self.layout == 'queued':
      self.npy['prev_feat'][...] = model_output[self.hidden].reshape(self.npy['prev_feat'].shape)
    sliced = {k: model_output[np.newaxis, v] for k, v in self.output_slices.items()}
    self._set_stage("output parsing")
    outputs = self.parser.parse_vision_outputs(sliced)
    outputs = self.parser.parse_policy_outputs(outputs)
    if 'planplus' in outputs and 'plan' in outputs:
      outputs['plan'] = outputs['plan'] + outputs['planplus']
    return outputs

  def warmup(self) -> None:
    dummy = {k: np.zeros(self.frame_size, dtype=np.uint8) for k in VISION_INPUTS}
    eye = np.eye(3, dtype=np.float32)
    dims = {'desire_pulse': ModelConstants.DESIRE_LEN, 'traffic_convention': 2, 'action_t': 2}
    self.run(dummy, dict.fromkeys(VISION_INPUTS, eye), {k: np.zeros(v, dtype=np.float32) for k, v in dims.items()})
    self.reset()
    self._warming_up = False

  def reset(self) -> None:
    self.packed[:] = 0
    for t in self.state.values():
      t.assign(t.zeros_like()).realize()
    # realize() enqueues the reset; finish it before handing the model to the driving thread.
    for device in dict.fromkeys((self.model_device, self.warp_device, "NPY")):
      Device[device].synchronize()
    self._blob_cache.clear()
    self.prev_desire[:] = 0

  @property
  def mlsim(self) -> bool:
    return self.generation >= 11

  def get_action(self, model_output: dict[str, np.ndarray], prev_action: log.ModelDataV2.Action,
                 lat_action_t: float, long_action_t: float, v_ego: float, dp_lat_offset_cm: int) -> log.ModelDataV2.Action:
    # sunnypilot modeld_v2 ModelState.get_action_from_model + dp lateral offset
    if 'action' not in model_output:
      plan = model_output['plan'][0]
      desired_accel, _ = get_accel_from_plan(plan[:, Plan.VELOCITY][:, 0], plan[:, Plan.ACCELERATION][:, 0],
                                             ModelConstants.T_IDXS, action_t=long_action_t)
      if not self.mlsim and 'desired_curvature' in model_output:
        desired_curvature = float(model_output['desired_curvature'][0, 0])
      else:
        desired_curvature = get_curvature_from_plan(plan[:, Plan.T_FROM_CURRENT_EULER][:, 2], plan[:, Plan.ORIENTATION_RATE][:, 2],
                                                    ModelConstants.T_IDXS, v_ego, lat_action_t)
    else:
      desired_accel = model_output['action'][0, 1]
      desired_curvature = model_output['action'][0, 0] / (max(1.0, v_ego))**2

    # dp - lateral offset (dp0111pre selfdrive/modeld/modeld.py formula), added to the target BEFORE smoothing:
    # prev_action already carries it, adding it after a non-zero smoothing would amplify it to offset / alpha
    if dp_lat_offset_cm != 0:
      desired_curvature += 2.0 * (dp_lat_offset_cm / 100.0) / 900.0

    stop = v_ego < 0.3 and desired_accel < 0.1
    desired_accel = smooth_value(desired_accel, prev_action.desiredAcceleration, self.LONG_SMOOTH_SECONDS)
    if self.generation >= 10:  # smooth curvature for post FOF models
      if v_ego > MIN_LAT_CONTROL_SPEED:
        desired_curvature = smooth_value(desired_curvature, prev_action.desiredCurvature, self.LAT_SMOOTH_SECONDS)
      else:
        desired_curvature = prev_action.desiredCurvature
    return log.ModelDataV2.Action(desiredCurvature=float(desired_curvature), desiredAcceleration=float(desired_accel), shouldStop=bool(stop))

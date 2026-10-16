import json
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parent / 'models'
TG_INPUT_DEVICES_PATH = MODELS_DIR / 'tg_input_devices.json'
USBGPU_VID = 0xADD1
USBGPU_PID = 0x0001
# dp - egpu: sunnypilot common/hardware/usb.py CHESTNUT_USB_IDS (both ids of a running chestnut / eGPU)
USBGPU_IDS = ((USBGPU_VID, USBGPU_PID), (0x3801, 0x0001), (0xADD1, 0x0002))  # dp - egpu: + UT3G V1.6 Dual


def use_builtin_usbgpu(present: bool, compiled: bool, egpu_failed: bool) -> bool:
  return present and compiled and not egpu_failed


def get_tg_input_devices(process_name: str, usbgpu: bool):
  with open(TG_INPUT_DEVICES_PATH) as f:
    return json.load(f)[process_name]['default' if not usbgpu else 'usbgpu']

def modeld_pkl_path(usbgpu: bool):
  prefix = 'big_' if usbgpu else ''
  return MODELS_DIR / f'{prefix}driving_tinygrad.pkl'

def usbgpu_present() -> bool:
  for d in Path("/sys/bus/usb/devices").glob("*"):
    try:
      if (int((d / "idVendor").read_text(), 16), int((d / "idProduct").read_text(), 16)) in USBGPU_IDS:
        return True
    except Exception:
      pass
  return False

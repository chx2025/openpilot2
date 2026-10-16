import ast
from pathlib import Path
from types import SimpleNamespace
import pytest


ROOT = Path(__file__).resolve().parents[3]
USB_IDS = ((0xADD1, 0x0001), (0x3801, 0x0001), (0xADD1, 0x0002))


class TestUsbGpuDeviceIds:
  def setup_method(self):
    # Execute the real enumeration guard without importing GPU drivers or opening hardware.
    tree = ast.parse((ROOT / "tinygrad_repo/tinygrad/runtime/ops_amd.py").read_text())
    iface = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "USBIface")
    init = next(node for node in iface.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    guard = next(node for node in init.body if isinstance(node, ast.If))
    self.guard = compile(ast.Module(body=[guard], type_ignores=[]), "<USBIface enumeration>", "exec")

  def enumerate(self, devices, dev_id=0, filter_devices=None):
    namespace = {
      "USB3": SimpleNamespace(list_devices=lambda vendor, product: devices.get((vendor, product), [])),
      "hcq_filter_visible_devices": filter_devices or (lambda devices, device: devices),
      "pluralize": lambda word, count: f"{count} {word}s",
      "dev_id": dev_id,
    }
    exec(self.guard, namespace)
    return namespace["visible"]

  @pytest.mark.parametrize("usb_id", USB_IDS)
  def test_each_supported_id(self, usb_id):
    assert self.enumerate({usb_id: ["gpu"]}) == ["gpu"]

  def test_multiple_devices_keep_original_id_first(self):
    devices = {usb_id: [f"gpu-{i}"] for i, usb_id in enumerate(USB_IDS)}
    assert self.enumerate(devices, dev_id=2) == ["gpu-0", "gpu-1", "gpu-2"]

  def test_visibility_filter_applies_to_combined_devices(self):
    calls = []

    def select(devices, device):
      calls.append((devices, device))
      return devices[1:]

    assert self.enumerate({USB_IDS[0]: ["first"], USB_IDS[1]: ["second"]}, filter_devices=select) == ["second"]
    assert calls == [(["first", "second"], "AMD")]

  def test_no_supported_device_still_raises(self):
    with pytest.raises(RuntimeError, match=r"AMD:0 does not exist"):
      self.enumerate({(0x174C, 0x2464): ["rom"]})

  def test_out_of_range_device_still_raises(self):
    with pytest.raises(RuntimeError, match=r"AMD:1 does not exist"):
      self.enumerate({USB_IDS[0]: ["gpu"]}, dev_id=1)

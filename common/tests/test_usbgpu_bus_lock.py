import multiprocessing
import threading
import time
import unittest

from openpilot.common.usbgpu_bus_lock import fcntl, usbgpu_bus_lock


def _acquire_lock_in_child(entered):
  with usbgpu_bus_lock():
    entered.set()


class TestUsbGpuBusLock(unittest.TestCase):
  def test_is_reentrant(self):
    with usbgpu_bus_lock():
      with usbgpu_bus_lock():
        pass

  def test_serializes_threads(self):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first():
      with usbgpu_bus_lock():
        first_entered.set()
        release_first.wait(2)

    def second():
      first_entered.wait(2)
      with usbgpu_bus_lock():
        second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    self.assertTrue(first_entered.wait(2))
    time.sleep(0.05)
    self.assertFalse(second_entered.is_set())
    release_first.set()
    first_thread.join(2)
    second_thread.join(2)
    self.assertTrue(second_entered.is_set())

  @unittest.skipIf(fcntl is None, "flock is only available on POSIX")
  def test_serializes_processes(self):
    context = multiprocessing.get_context("fork")
    entered = context.Event()

    with usbgpu_bus_lock():
      child = context.Process(target=_acquire_lock_in_child, args=(entered,))
      child.start()
      time.sleep(0.05)
      self.assertFalse(entered.is_set())

    self.assertTrue(entered.wait(2))
    child.join(2)
    self.assertEqual(child.exitcode, 0)

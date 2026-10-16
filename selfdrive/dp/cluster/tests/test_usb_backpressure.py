from __future__ import annotations

import ast
from collections.abc import Iterable
import ctypes
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock


CLUSTER_DIR = Path(__file__).resolve().parents[1]


def load_class(file_name: str, name: str, methods: set[str] | None = None) -> type:
    tree = ast.parse((CLUSTER_DIR / file_name).read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    if methods is not None:
        cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls],
        type_ignores=[],
    )
    namespace = {
        "threading": threading,
        "time": time,
        "queue": queue,
        "ctypes": ctypes,
        "Any": Any,
        "Iterable": Iterable,
        "NATIVE_PACKET_QUEUE_PUT_TIMEOUT_S": 0.05,
    }
    exec(compile(ast.fix_missing_locations(module), str(CLUSTER_DIR / file_name), "exec"), namespace)
    return namespace[name]


class JpegBackpressureTests(unittest.TestCase):
    def test_busy_frame_dropped_until_usb_send_completes(self):
        cls = load_class("cluster_usb_pipeline.py", "AsyncJpegUsbPipeline")
        entered, release = threading.Event(), threading.Event()
        display = Mock()
        display.encode_jpeg.side_effect = lambda rgba, w, h: rgba
        display.profile_samples.return_value = ()

        def send(frame):
            entered.set()
            if not release.wait(2.0):
                raise RuntimeError("test did not release sender")

        display.send_jpeg.side_effect = send
        pipeline = cls(display)
        pipeline.start()
        try:
            pipeline.submit_rgba(b"first", 1, 1)
            self.assertTrue(entered.wait(1.0))
            # The worker has removed the pending slot, but is still sending.
            self.assertIsNone(pipeline._pending_rgba)
            self.assertFalse(pipeline.wait_for_capacity(timeout=0.0))
            pipeline.submit_rgba(b"stale", 1, 1)
            self.assertIsNone(pipeline._pending_rgba)
            release.set()
            self.assertTrue(pipeline.wait_for_capacity(timeout=1.0))
            display.send_jpeg.assert_called_once_with(b"first")
            pipeline.submit_rgba(b"latest", 1, 1)
            self.assertTrue(pipeline.wait_for_capacity(timeout=1.0))
            self.assertEqual(display.send_jpeg.call_args.args[0], b"latest")
        finally:
            release.set()
            pipeline.close()
        self.assertFalse(pipeline.wait_for_capacity(timeout=0.0))

    def test_sender_error_is_not_treated_as_busy_drop(self):
        cls = load_class("cluster_usb_pipeline.py", "AsyncJpegUsbPipeline")
        pipeline = cls(Mock())
        pipeline._error = OSError("disconnected")
        with self.assertRaisesRegex(RuntimeError, "pipeline failed"):
            pipeline.wait_for_capacity(timeout=0.0)
        with self.assertRaisesRegex(RuntimeError, "pipeline failed"):
            pipeline.submit_rgba(b"new", 1, 1)


class H264BackpressureTests(unittest.TestCase):
    def setUp(self):
        cls = load_class(
            "cluster_h264_pipeline.py",
            "H264UsbPipeline",
            {
                "ready_for_frame",
                "_send_queued_packets",
                "_native_packet_callback",
                "_packetize_h264_for_usb",
                "_set_error",
                "submit_rgba",
                "_write_rgba_frames",
                "submit_nv12",
                "submit_native_nv12_dmabuf_input",
                "close",
            },
        )
        self.pipeline = cls()
        p = self.pipeline
        p.check_error = Mock()
        p._closing = False
        p._native_handle = 1
        p._native_lib = SimpleNamespace(cluster_h264_encoder_bridge_drain=Mock(return_value=0))
        p._native_callback = Mock()
        p._native_waiting_for_packet = False
        p._native_packet_deadline = time.perf_counter() + 3.0
        p._packet_queue = queue.Queue(maxsize=8)
        p._condition = threading.Condition()
        p._error = None
        p.chunk_size = 4
        p._send_h264_chunk = Mock()

    def test_packet_being_sent_still_blocks_next_frame(self):
        p = self.pipeline
        p._packet_queue.put(([b"first", b"second"], False))

        def send(chunk, size, **kwargs):
            self.assertFalse(p.ready_for_frame())

        p._send_h264_chunk.side_effect = send
        # Stop after this packet; the sentinel is also acknowledged.
        p._packet_queue.put(None)
        p._send_queued_packets()
        self.assertEqual(p._packet_queue.unfinished_tasks, 0)
        self.assertTrue(p.ready_for_frame())
        self.assertEqual([call.args[0] for call in p._send_h264_chunk.call_args_list], [b"first", b"second"])

    def test_large_encoded_frame_queues_whole_not_partial_chunks(self):
        p = self.pipeline
        p.usb_display = SimpleNamespace(profile_enabled=False)
        p._debug_encoder_packets = p._debug_encoder_bytes = p._debug_max_packet_bytes = 0
        for name in ("_debug_log_encoder_packet", "_write_dump", "_debug_log_packetize", "_record_h264_unit", "_record_h264_queue_depth"):
            setattr(p, name, Mock())
        p._prepare_hardware_packet = lambda packet, **kwargs: packet
        data = b"x" * 80  # Twenty USB chunks, larger than the old eight-chunk queue.
        buffer = ctypes.create_string_buffer(data)
        p._native_waiting_for_packet = True
        p._native_packet_callback(ctypes.addressof(buffer), len(data), 0, 0, 0, 1, 0)
        self.assertIsNone(p._error)
        self.assertFalse(p._native_waiting_for_packet)
        self.assertEqual(p._packet_queue.qsize(), 1)
        self.assertFalse(p.ready_for_frame())
        p._packet_queue.put(None)
        p._send_queued_packets()
        self.assertEqual(b"".join(call.args[0] for call in p._send_h264_chunk.call_args_list), data)
        self.assertTrue(p.ready_for_frame())

    def test_delayed_encoder_packet_is_polled_without_new_input(self):
        p = self.pipeline
        p._native_waiting_for_packet = True
        self.assertFalse(p.ready_for_frame())
        p._native_lib.cluster_h264_encoder_bridge_drain.assert_called_once_with(1, 0, p._native_callback, None)
        p._native_waiting_for_packet = False
        self.assertTrue(p.ready_for_frame())

    def test_busy_native_drops_before_encoding(self):
        p = self.pipeline
        p.encoder_width, p.encoder_height = 1, 1
        p._submit_nv12_native = Mock()
        p._packet_queue.put(([b"pending"], False))
        p.submit_nv12(b"new", 1, 1)
        p._submit_nv12_native.assert_not_called()

    def test_missing_native_output_reports_timeout(self):
        p = self.pipeline
        p._native_waiting_for_packet = True
        p._native_packet_deadline = time.perf_counter() - 1.0
        with self.assertRaisesRegex(RuntimeError, "frame output timed out"):
            p.ready_for_frame()

    def test_busy_dmabuf_keeps_lease_active_for_context_cancellation(self):
        p = self.pipeline
        p.native_dmabuf_input_available = Mock(return_value=True)
        p._native_lib.cluster_h264_encoder_bridge_submit_nv12_input_dmabuf = Mock()
        p._packet_queue.put(([b"pending"], False))
        lease = SimpleNamespace(owner_id=id(p), active=True, dmabuf_fd=5, index=0)
        p.submit_native_nv12_dmabuf_input(lease)
        self.assertTrue(lease.active)
        p._native_lib.cluster_h264_encoder_bridge_submit_nv12_input_dmabuf.assert_not_called()

    def test_ffmpeg_worker_error_propagates(self):
        p = self.pipeline
        p._native_handle = None
        p._input_busy = True
        p._pending_rgba = b"1234"
        p.encoder_width = p.encoder_height = 1
        p._proc = SimpleNamespace(stdin=SimpleNamespace(fileno=lambda: 1))
        p._write_all = Mock(side_effect=BrokenPipeError("encoder exited"))
        p._write_rgba_frames()
        self.assertIsInstance(p._error, BrokenPipeError)

    def test_ffmpeg_busy_writer_drops_new_raw_frames_and_copies_input(self):
        p = self.pipeline
        p._native_handle = None
        p._input_busy = False
        p._pending_rgba = None
        p.width = p.encoder_width = 1
        p.height = p.encoder_height = 1
        p._proc = SimpleNamespace(stdin=SimpleNamespace(fileno=lambda: 1))
        p._add_sample = Mock()
        entered, release = threading.Event(), threading.Event()

        def write(fd, data, count):
            entered.set()
            if not release.wait(2.0):
                raise RuntimeError("test did not release writer")

        p._write_all = Mock(side_effect=write)
        worker = threading.Thread(target=p._write_rgba_frames)
        worker.start()
        try:
            rgba = bytearray(b"1234")
            p.submit_rgba(rgba, 1, 1)
            rgba[:] = b"5678"
            self.assertTrue(entered.wait(1.0))
            self.assertFalse(p.ready_for_frame())
            p.submit_rgba(b"skip", 1, 1)
            self.assertIsNone(p._pending_rgba)
            p._write_all.assert_called_once_with(1, b"1234", 4)
            release.set()
            with p._condition:
                self.assertTrue(p._condition.wait_for(lambda: not p._input_busy, timeout=1.0))
            self.assertTrue(p.ready_for_frame())
        finally:
            release.set()
            with p._condition:
                p._closing = True
                p._condition.notify_all()
            worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())
        self.assertFalse(p.ready_for_frame())

    def test_sender_error_surfaces_and_acknowledges_queue_task(self):
        p = self.pipeline
        p._packet_queue.put(([b"packet"], False))
        p._send_h264_chunk.side_effect = OSError("USB disconnected")
        p._send_queued_packets()
        self.assertIsInstance(p._error, OSError)
        self.assertEqual(p._packet_queue.unfinished_tasks, 0)

    def test_close_unblocks_writer_before_closing_pipe(self):
        p = self.pipeline
        p._native_handle = None
        p._input_busy = True
        p._pending_rgba = b"1234"
        p.encoder_width = p.encoder_height = 1
        p._packet_queue = None
        p._stdout_thread = p._stderr_thread = p._sender_thread = None
        p._stream_started = False
        p._add_sample = Mock()
        p._maybe_log_h264_diag = p._debug_log_close_summary = p._close_dump_file = Mock()
        entered, release = threading.Event(), threading.Event()
        p._proc = Mock()
        p._proc.stdin.fileno.return_value = 1
        p._proc.terminate.side_effect = release.set

        def write(*args):
            entered.set()
            if not release.wait(2.0):
                raise RuntimeError("test did not unblock pipe")

        p._write_all = Mock(side_effect=write)
        p._input_thread = threading.Thread(target=p._write_rgba_frames)
        p._proc.stdin.close.side_effect = lambda: self.assertFalse(p._input_thread.is_alive())
        p._input_thread.start()
        try:
            self.assertTrue(entered.wait(1.0))
            p.close()
            p._proc.terminate.assert_called_once()
            p._proc.stdin.close.assert_called_once()
            self.assertFalse(p._input_thread.is_alive())
        finally:
            release.set()
            with p._condition:
                p._closing = True
                p._condition.notify_all()
            p._input_thread.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()

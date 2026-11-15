"""Headless unit tests for the TV transport kernel (no sockets, no ffmpeg).

Run: python3 -m unittest tvcast.tests.test_kernel -v
"""
import time
import unittest

from .. import rtp
from ..receiver import Receiver
from ..injector import Injector


def fake_rtp(seq, payload=b"\x00" * 1316, ssrc=0xCAFE0001):
    return rtp.build_rtp(seq, seq * 3000, ssrc, payload)


class SequenceHandling(unittest.TestCase):
    def setUp(self):
        self.rx = Receiver({"port": 0}, reorder_timeout=0.05,
                           max_reorder=8, capture_dir=None)
        # bypass socket/decoder wiring
        self.rx.decoder.write = lambda data: True
        self.rx._started = False

    def feed(self, seq):
        self.rx._handle(fake_rtp(seq), time.monotonic())

    def drain(self, seconds=0.3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.rx._watchdog_once()
            time.sleep(0.02)

    def test_wrap_is_not_mass_loss(self):
        # stream crosses the 16-bit boundary
        for seq in range(65530, 65536):
            self.feed(seq)
        for seq in range(0, 6):
            self.feed(seq)
        self.assertEqual(self.rx.stats["wraps"], 1)
        self.assertEqual(self.rx.stats["missing_packets"], 0)
        self.assertEqual(self.rx.stats["gap_intervals"], 0)
        self.assertEqual(self.rx.stats["in_order"], 12)

    def test_reorder_and_duplicate(self):
        for seq in range(100, 105):
            self.feed(seq)
        # delayed arrival of 106 after 107 already buffered
        self.feed(107)
        self.feed(106)
        self.feed(107)  # exact duplicate
        self.assertEqual(self.rx.stats["out_of_order"], 2)
        self.assertEqual(self.rx.stats["duplicates"], 1)
        self.assertEqual(self.rx.stats["missing_packets"], 0)

    def test_unrecoverable_gap_is_marked(self):
        self.feed(200)
        self.feed(201)
        # 202..206 lost forever; next packet arrives at 207
        self.feed(207)
        self.drain()
        self.assertGreaterEqual(self.rx.stats["gap_intervals"], 1)
        self.assertEqual(self.rx.stats["missing_packets"], 5)
        gap = self.rx.gap_events[-1]
        self.assertEqual(gap["from_seq"], 202)
        self.assertEqual(gap["to_seq"], 206)
        # stream continues normally afterwards
        self.feed(208)
        self.assertEqual(self.rx.stats["in_order"], 3)

    def test_late_packet_after_gap_not_double_counted(self):
        self.feed(300)
        self.feed(303)  # 301,302 missing
        self.drain()
        self.assertEqual(self.rx.stats["missing_packets"], 2)
        self.feed(301)  # too late: classified, never reopens the gap
        self.assertEqual(self.rx.stats["late_after_gap"] +
                         self.rx.stats["duplicates"], 1)
        self.assertEqual(self.rx.stats["missing_packets"], 2)

    def test_multiple_wraps_stay_continuous(self):
        seq = 65534
        for _ in range(20):
            self.feed(seq)
            seq = (seq + 1) & 0xFFFF
        self.assertEqual(self.rx.stats["wraps"], 1)
        self.assertEqual(self.rx.stats["missing_packets"], 0)


class InjectorTest(unittest.TestCase):
    def test_deterministic_burst_and_loss(self):
        inj = Injector(seed=1)
        lost = 0
        inj.trigger_burst(5)
        for i in range(20):
            out = inj.process(f"p{i}".encode())
            lost += 0 if out else 1
        self.assertEqual(inj.snapshot()["burst_remaining"], 0)
        self.assertGreaterEqual(lost, 5)

    def test_reorder_emits_newest_first(self):
        inj = Injector()
        inj.reconfigure(reorder_window=3)
        emitted = []
        for i in range(3):
            emitted.extend(inj.process(bytes([i])))
        # newest packet jumps the queue
        self.assertEqual(emitted[0], b"\x02")
        flushed = inj.flush()
        self.assertIn(b"\x00", flushed)


if __name__ == "__main__":
    unittest.main()

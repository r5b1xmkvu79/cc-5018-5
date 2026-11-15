"""Controlled network impairment injector: loss, bursts, duplication, reordering.

Block (all send traffic suspended for a window) is driven by the sender,
because it represents the socket path being unavailable rather than a
per-packet decision.
"""
import random
import time
from collections import deque


class Injector:
    def __init__(self, seed=20261115):
        self.rng = random.Random(seed)
        self.loss_rate = 0.0          # uniform random drops
        self.duplicate_rate = 0.0     # fraction duplicated (duplicate sent later)
        self.reorder_window = 0       # packets held back, 0 disables
        self.burst_length = 0         # size of next contiguous burst drop
        self.counters = {
            "in": 0, "sent": 0, "lost": 0, "dup": 0,
            "reordered": 0, "burst_remaining": 0,
        }
        self._delay_q = deque()       # (release_time, packet)
        self._hold = deque()          # packets inside the reorder window
        self._burst_remaining = 0

    def reconfigure(self, loss_rate=None, duplicate_rate=None,
                    reorder_window=None):
        if loss_rate is not None:
            self.loss_rate = max(0.0, min(1.0, loss_rate))
        if duplicate_rate is not None:
            self.duplicate_rate = max(0.0, min(1.0, duplicate_rate))
        if reorder_window is not None:
            self.reorder_window = max(0, int(reorder_window))

    def trigger_burst(self, length):
        self._burst_remaining = max(self._burst_remaining, int(length))
        self.counters["burst_remaining"] = self._burst_remaining

    def process(self, packet):
        """Return the list of packets to send now (may contain swaps/dups)."""
        self.counters["in"] += 1
        now = time.monotonic()
        out = []

        # release timed-out reorder holdings first
        while self._delay_q and self._delay_q[0][0] <= now:
            out.append(self._delay_q.popleft()[1])

        if self._burst_remaining > 0:
            self._burst_remaining -= 1
            self.counters["burst_remaining"] = self._burst_remaining
            self.counters["lost"] += 1
            return out

        if self.rng.random() < self.loss_rate:
            self.counters["lost"] += 1
            return out

        if self.reorder_window > 0:
            self._hold.append(packet)
            if len(self._hold) >= self.reorder_window:
                # swap: release newest first, keep the rest pending
                newest = self._hold.pop()
                oldest = self._hold.popleft()
                out.append(newest)
                self.counters["reordered"] += 1
                self._delay_q.append((now + 0.05, oldest))
            return out

        out.append(packet)
        if self.rng.random() < self.duplicate_rate:
            # duplicate arrives late, mimicking a retransmitted/duplicated packet
            self._delay_q.append((now + 0.04, packet))
            self.counters["dup"] += 1
        self.counters["sent"] += 1
        return out

    def flush(self):
        out = [p for _, p in self._delay_q]
        self._delay_q.clear()
        return out

    def snapshot(self):
        c = dict(self.counters)
        c["burst_remaining"] = self._burst_remaining
        c["pending"] = len(self._delay_q) + len(self._hold)
        return c

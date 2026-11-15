"""Broadcast kernel: owns sender + receiver lifecycle and test scenarios."""
import threading
import time

from .configs import CHANNELS, DEFAULT_MCAST, DEFAULT_UNICAST
from .receiver import Receiver
from .sender import Sender


class BroadcastKernel:
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.mode = "multicast"
        self.channel = "campus-news"
        self.sender = None
        self.receiver = None
        self._scenario_thread = None
        self.scenario = {"name": None, "phase": "idle", "detail": ""}
        self._lock = threading.Lock()
        self._listeners = []
        self._t0 = None

    # --------------------------------------------------------- transport
    def _dest(self):
        if self.mode == "multicast":
            return dict(DEFAULT_MCAST)
        return dict(DEFAULT_UNICAST)

    def start_pipeline(self):
        with self._lock:
            if self.sender:
                return
            dest = self._dest()
            capture_dir = self.base_dir / "captures"
            self.sender = Sender(dest)
            self.receiver = Receiver(dest, capture_dir=capture_dir)
            self._t0 = time.monotonic()
            self.receiver.start()
            time.sleep(0.4)  # join group before first datagram
            self.sender.start(self.channel)
        self._notify("pipeline_started")

    def stop_pipeline(self):
        with self._lock:
            if not self.sender:
                return
            s, r = self.sender, self.receiver
            self.sender = self.receiver = None
        s.stop()
        r.stop()
        self.scenario = {"name": None, "phase": "idle", "detail": ""}
        self._notify("pipeline_stopped")

    def restart_with_mode(self, mode):
        running = bool(self.sender)
        if running:
            self.stop_pipeline()
        self.mode = mode
        if running:
            self.start_pipeline()

    # --------------------------------------------------------- controls
    def switch_channel(self, channel):
        if channel not in CHANNELS:
            raise ValueError(channel)
        if not self.sender:
            self.channel = channel
            return
        self.sender.switch_channel(channel)
        self.receiver.note_source_switch(channel)
        self.channel = channel
        self._notify("channel_switch")

    def set_injection(self, loss_rate=None, duplicate_rate=None,
                      reorder_window=None):
        if not self.sender:
            raise RuntimeError("pipeline not running")
        self.sender.injector.reconfigure(loss_rate, duplicate_rate,
                                         reorder_window)
        self._notify("injection_updated")

    def trigger_burst(self, length):
        if not self.sender:
            raise RuntimeError("pipeline not running")
        self.sender.injector.trigger_burst(length)

    def block(self, seconds):
        if not self.sender:
            raise RuntimeError("pipeline not running")
        self.sender.block(seconds)

    # --------------------------------------------------------- scenarios
    def run_scenario(self, name):
        if self._scenario_thread and self._scenario_thread.is_alive():
            raise RuntimeError("another scenario is running")
        if not self.sender:
            self.start_pipeline()
            time.sleep(1.0)
        self._scenario_thread = threading.Thread(
            target=self._scenario_runner, args=(name,), daemon=True)
        self._scenario_thread.start()

    def _set_phase(self, name, phase, detail=""):
        self.scenario = {"name": name, "phase": phase, "detail": detail}
        self._notify("scenario")

    def _scenario_runner(self, name):
        try:
            if name == "continuous":
                self._sc_continuous()
            elif name == "pmt_change":
                self._sc_pmt_change()
            elif name == "late_join":
                self._sc_late_join()
            elif name == "block":
                self._sc_block()
            elif name == "wrap":
                self._sc_wrap()
            else:
                raise ValueError(name)
            self._set_phase(name, "done", "completed")
        except Exception as exc:  # surface failure back to the control page
            self._set_phase(name, "failed", str(exc))

    def _sc_continuous(self):
        name = "continuous"
        self._set_phase(name, "running",
                        "20s clean broadcast, measuring first decodable time")
        time.sleep(20)
        self._set_phase(name, "running", "injecting 5% controlled loss, 10s")
        self.set_injection(loss_rate=0.05)
        time.sleep(10)
        self.set_injection(loss_rate=0.0)
        self._set_phase(name, "running", "recovery observation, 5s")
        time.sleep(5)

    def _sc_pmt_change(self):
        name = "pmt_change"
        self._set_phase(name, "running", "baseline on campus-news, 8s")
        self.switch_channel("campus-news")
        time.sleep(8)
        self._set_phase(name, "running", "switching to campus-life (new PMT)")
        self.switch_channel("campus-life")
        time.sleep(8)
        self._set_phase(name, "running", "switching to campus-tts")
        self.switch_channel("campus-tts")
        time.sleep(8)
        self.switch_channel("campus-news")

    def _sc_late_join(self):
        name = "late_join"
        self._set_phase(name, "running", "stopping receiver, sender keeps airing")
        rx = self.receiver
        rx.stop()
        time.sleep(6)
        self._set_phase(name, "running", "receiver joins the live multicast")
        new_rx = Receiver(self._dest(),
                          capture_dir=self.base_dir / "captures")
        self.receiver = new_rx
        new_rx.start()
        time.sleep(10)
        self._set_phase(name, "done",
                        "check first_packet_at / first_output_at in snapshot")

    def _sc_block(self):
        name = "block"
        self._set_phase(name, "running", "clean 6s baseline")
        time.sleep(6)
        self._set_phase(name, "running", "4s network block")
        self.block(4.0)
        time.sleep(5)
        self._set_phase(name, "running", "post-block recovery, 8s")
        time.sleep(8)

    def _sc_wrap(self):
        name = "wrap"
        self._set_phase(name, "running",
                        "restarting sender near 65535 to force a wrap")
        s = self.sender
        s.stop_source()
        s.seq = 65530
        s._start_source(self.channel)
        self.receiver.note_source_switch(self.channel)
        self._set_phase(name, "running",
                        "streaming across the wrap boundary, 12s")
        time.sleep(12)

    # --------------------------------------------------------- snapshots
    def snapshot(self):
        up = bool(self.sender)
        out = {
            "mode": self.mode,
            "channel": self.channel,
            "channels": {k: v["label"] for k, v in CHANNELS.items()},
            "pipeline_up": up,
            "scenario": dict(self.scenario),
            "uptime": round(time.monotonic() - self._t0, 2) if self._t0 and up else None,
        }
        if up:
            out["sender"] = self.sender.snapshot()
            out["receiver"] = self.receiver.snapshot()
        return out

    # --------------------------------------------------------- events
    def add_listener(self, cb):
        self._listeners.append(cb)

    def _notify(self, event):
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:
                pass

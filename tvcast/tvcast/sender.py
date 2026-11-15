"""Sender: ffmpeg MPEG-TS source -> RTP packetization -> impairment -> UDP."""
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from . import rtp
from .configs import CHANNELS
from .injector import Injector


def _find_ffmpeg():
    here = Path(__file__).resolve().parent.parent
    p = here / "tools" / "ffmpeg-static" / "ffmpeg"
    return str(p) if p.exists() else "ffmpeg"


class Sender:
    def __init__(self, dest, start_seq=65530, ssrc=0xCAFE0001):
        self.dest = dict(dest)
        self.seq = start_seq & 0xFFFF
        self.ssrc = ssrc
        self.injector = Injector()
        self._proc = None
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._block_until = 0.0
        self._lock = threading.Lock()
        self.stats = {
            "rtp_sent": 0, "rtp_ts_payload_bytes": 0,
            "ffmpeg_restarts": 0, "blocks": 0,
            "start_time": None, "state": "idle", "channel": None,
        }
        self.events = []
        self._ffmpeg = _find_ffmpeg()

    # ------------------------------------------------------------- control
    def start(self, channel):
        if self.stats["state"] == "running":
            self.stop_source()
        with self._lock:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                       socket.IPPROTO_UDP)
            if "group" in self.dest:
                self._sock.setsockopt(socket.IPPROTO_IP,
                                      socket.IP_MULTICAST_TTL, 2)
                self._sock.setsockopt(socket.IPPROTO_IP,
                                      socket.IP_MULTICAST_LOOP, 1)
                self._sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(self.dest.get("iface", "127.0.0.1")))
                self.dest_tuple = (self.dest["group"], self.dest["port"])
            else:
                self.dest_tuple = (self.dest["host"], self.dest["port"])
        self.stats["channel"] = channel
        self.stats["state"] = "starting"
        self._stop.clear()
        self._start_source(channel)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _start_source(self, channel):
        cmd = [self._ffmpeg, "-hide_banner", "-loglevel", "warning",
               *CHANNELS[channel]["cmd"]]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE)
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        if self.stats["start_time"] is None:
            self.stats["start_time"] = time.monotonic()
        else:
            self.stats["ffmpeg_restarts"] += 1
        self._log("source_start", channel=channel)

    def stop_source(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def stop(self):
        self._stop.set()
        self.stop_source()
        if self._thread:
            self._thread.join(timeout=3)
        with self._lock:
            if self._sock:
                self._sock.close()
                self._sock = None
        self.stats["state"] = "idle"

    def switch_channel(self, channel):
        """Restart only the ffmpeg source; RTP sequence space is continuous."""
        if channel not in CHANNELS:
            raise ValueError(channel)
        self.stop_source()
        self._log("channel_switch", channel=channel)
        self._start_source(channel)
        self.stats["channel"] = channel

    def block(self, seconds):
        """Suspend all sends for a window; queue is not buffered (path down)."""
        self._block_until = time.monotonic() + seconds
        self.stats["blocks"] += 1
        self._log("block_begin", seconds=seconds)

    # ------------------------------------------------------------- internals
    def _run(self):
        self.stats["state"] = "running"
        start = time.monotonic()
        leftover = b""
        seen_proc = None
        try:
            while not self._stop.is_set():
                proc = self._proc
                if proc is None or proc is not seen_proc:
                    # source not started yet, or a switch installed a new one
                    leftover = b""
                    seen_proc = proc
                    time.sleep(0.05)
                    continue
                if proc.poll() is not None:
                    # source died unexpectedly; stay resident so a later
                    # switch can attach a fresh source to the same thread
                    self._log("source_exit", rc=proc.returncode)
                    seen_proc = None
                    time.sleep(0.1)
                    continue
                chunk = proc.stdout.read(4096)
                if not chunk:
                    time.sleep(0.02)
                    continue
                leftover += chunk
                while len(leftover) >= rtp.RTP_MTU:
                    payload, leftover = (leftover[:rtp.RTP_MTU],
                                         leftover[rtp.RTP_MTU:])
                    self._emit(payload, start)
        finally:
            for late in self.injector.flush():
                self._send(late)
            if self._stop.is_set():
                self.stats["state"] = "idle"

    def _emit(self, payload, start):
        seq = self.seq
        rtp_ts = int((time.monotonic() - start) * rtp.TS_CLOCK_HZ)
        packet = rtp.build_rtp(seq, rtp_ts, self.ssrc, payload)
        self.seq = (self.seq + 1) & 0xFFFF
        for out in self.injector.process(packet):
            if time.monotonic() < self._block_until:
                # network path blocked: packets during the window are lost,
                # not buffered, matching a short network outage
                self.injector.counters["lost"] += 1
                continue
            self._send(out)

    def _send(self, packet):
        with self._lock:
            if not self._sock:
                return
            try:
                self._sock.sendto(packet, self.dest_tuple)
            except OSError as exc:
                self._log("send_error", error=str(exc))
                return
        self.stats["rtp_sent"] += 1
        self.stats["rtp_ts_payload_bytes"] += len(packet) - 12

    def _drain_stderr(self):
        proc = self._proc
        if not proc:
            return
        for line in proc.stderr:
            self._log("ffmpeg", line=line.decode(errors="replace").strip())

    def _log(self, kind, **kw):
        self.events.append({"at": round(time.monotonic(), 3),
                            "kind": kind, **kw})
        self.events[:] = self.events[-100:]

    def snapshot(self):
        return {
            "stats": dict(self.stats),
            "dest": self.dest,
            "injector": self.injector.snapshot(),
            "next_seq": self.seq,
            "events": list(self.events[-15:]),
        }

"""Receiver: RTP reorder (wrap-aware), gap marking, dejitter, decode, analysis."""
import collections
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from . import rtp
from .ts import TSAnalyzer

NULL_TS_PACKET = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\x00" * 184


def _find_ffmpeg():
    here = Path(__file__).resolve().parent.parent
    p = here / "tools" / "ffmpeg-static" / "ffmpeg"
    return str(p) if p.exists() else "ffmpeg"


class Decoder:
    """Real ffmpeg pipelines.

    validator: persistent decode/remux probe (null output). It keeps running
    even with no browser attached and yields the true first-decodable time
    and decoder-side errors, which are kept separate from network loss.
    stream(): one short-lived ffmpeg per browser connection, emitting
    fragmented MP4; a disconnect only kills that connection's process.
    """

    def __init__(self):
        self._proc = None
        self.first_output_time = None
        self.error_lines = collections.deque(maxlen=30)
        self.error_count = 0
        self.bytes_in = 0
        self._ffmpeg = _find_ffmpeg()

    def start(self):
        # full decode of video + audio proves the received TS is decodable;
        # null muxer means no stdout backpressure can kill the process
        cmd = [
            self._ffmpeg, "-hide_banner", "-loglevel", "warning",
            "-fflags", "+discardcorrupt+genpts",
            "-f", "mpegts", "-i", "pipe:0",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-f", "null", "-",
        ]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._watch_stderr, daemon=True).start()

    def open_stream(self):
        """Return (process, ) for a browser fMP4 connection."""
        cmd = [
            self._ffmpeg, "-hide_banner", "-loglevel", "warning",
            "-fflags", "+discardcorrupt+genpts",
            "-f", "mpegts", "-i", "pipe:0",
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
        proc.tv_stderr = []

        def pump_err():
            for raw in proc.stderr:
                line = raw.decode(errors="replace").strip()
                if line:
                    proc.tv_stderr.append(line)
        threading.Thread(target=pump_err, daemon=True).start()
        return proc

    def _watch_stderr(self):
        started = False
        for raw in self._proc.stderr:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            low = line.lower()
            if "error" in low or "corrupt" in low or "missing" in low:
                self.error_count += 1
                self.error_lines.append({"at": round(time.monotonic(), 3),
                                         "line": line})
            elif not started and ("frame=" in low or "stream mapping" in low):
                started = True
        if not started:
            # ffmpeg only prints frame stats at exit; first output is marked
            # by the caller once the first valid input has been accepted
            pass

    def mark_first_output(self):
        if self.first_output_time is None:
            self.first_output_time = time.monotonic()

    def write(self, data):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.write(data)
                self.bytes_in += len(data)
                self.mark_first_output()
                return True
            except (BrokenPipeError, OSError):
                return False
        return False

    def stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def snapshot(self, t0):
        running = self._proc is not None and self._proc.poll() is None
        return {
            "running": running,
            "first_output_at": (round(self.first_output_time - t0, 3)
                                if self.first_output_time and t0 else None),
            "decode_error_count": self.error_count,
            "recent_errors": list(self.error_lines)[-8:],
            "ts_bytes_fed": self.bytes_in,
        }


class Receiver:
    def __init__(self, listen, reorder_timeout=0.25, max_reorder=256,
                 max_playout=600, capture_dir=None):
        self.listen = dict(listen)
        self.reorder_timeout = reorder_timeout
        self.max_reorder = max_reorder
        self.max_playout = max_playout
        self.capture_dir = Path(capture_dir) if capture_dir else None
        self.decoder = Decoder()
        self.analyzer = TSAnalyzer()

        self._sock = None
        self._stop = threading.Event()
        self._rx_thread = None
        self._play_thread = None
        self._watchdog_thread = None
        self._lock = threading.Lock()

        # extended (64-bit) sequence space makes wrap handling uniform
        self._started = False
        self._expected_ext = 0
        self._base_seq = 0
        self._last_seq16 = None
        self._base_rtp_ts = None
        self._buf = {}                 # ext_seq -> (pkt_info, arrival)
        self._delivered_lo = 0          # ext seqs below this are evicted
        self._delivered = set()        # recent ext_seqs, for dup classification
        self._delivered_q = collections.deque(maxlen=2048)
        self._first_gap_seen_at = None
        self._play_q = collections.deque()   # ("pkt", ext, payload) / ("gap", n)
        self._cond = threading.Condition(self._lock)

        self.stats = {
            "state": "idle", "start_time": None,
            "datagrams": 0, "parse_errors": 0, "ssrc_changes": 0,
            "in_order": 0, "out_of_order": 0, "duplicates": 0,
            "late_after_gap": 0, "wraps": 0,
            "missing_packets": 0, "gap_intervals": 0,
            "null_ts_inserted": 0, "playout_overflows": 0,
            "playout_high_water": 0, "jitter_rfc3550": 0.0,
            "first_packet_at": None, "first_run_ext": None,
        }
        self.gap_events = collections.deque(maxlen=50)
        self.events = collections.deque(maxlen=100)
        self._last_ssrc = None
        self._j = 0.0
        self._last_arrival = None
        self._last_rtp_ts = None
        self._capture_fh = None
        self._sample_fh = None
        self._streams = []
        self._streams_lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                                   socket.IPPROTO_UDP)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", self.listen["port"]))
        if "group" in self.listen:
            mreq = struct.pack(
                "4s4s", socket.inet_aton(self.listen["group"]),
                socket.inet_aton(self.listen.get("iface", "127.0.0.1")))
            self._sock.setsockopt(socket.IPPROTO_IP,
                                  socket.IP_ADD_MEMBERSHIP, mreq)
        self._sock.settimeout(0.5)
        self.stats["state"] = "running"
        self.stats["start_time"] = time.monotonic()
        self.decoder.start()
        if self.capture_dir:
            self.capture_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self._capture_fh = open(
                self.capture_dir / f"reordered-{stamp}.ts", "wb")
            self._sample_fh = open(
                self.capture_dir / f"packets-{stamp}.csv", "w")
            self._sample_fh.write("wall,seq16,ext_seq,classification,arrival_gap_ms\n")
        self._stop.clear()
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._play_thread = threading.Thread(target=self._play_loop, daemon=True)
        self._watchdog_thread = threading.Thread(target=self._watchdog, daemon=True)
        self._rx_thread.start()
        self._play_thread.start()
        self._watchdog_thread.start()

    def stop(self):
        self._stop.set()
        for t in (self._rx_thread, self._play_thread, self._watchdog_thread):
            if t:
                t.join(timeout=2)
        with self._streams_lock:
            streams = list(self._streams)
            self._streams.clear()
        for proc in streams:
            if proc.poll() is None:
                proc.kill()
        self.decoder.stop()
        if self._sock:
            self._sock.close()
            self._sock = None
        if self._capture_fh:
            self._capture_fh.close()
        if self._sample_fh:
            self._sample_fh.close()
        self.stats["state"] = "idle"

    # ------------------------------------------------------------- RX path
    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            arrival = time.monotonic()
            self._handle(data, arrival)

    def _handle(self, data, arrival):
        try:
            info = rtp.parse_rtp(data)
        except rtp.RtpParseError:
            self.stats["parse_errors"] += 1
            return
        with self._lock:
            self.stats["datagrams"] += 1
            if self.stats["first_packet_at"] is None:
                self.stats["first_packet_at"] = arrival
            if self._last_ssrc is not None and info["ssrc"] != self._last_ssrc:
                self.stats["ssrc_changes"] += 1
                self._ev("ssrc_change", old=self._last_ssrc, new=info["ssrc"])
            self._last_ssrc = info["ssrc"]
            self._update_jitter(info["ts"], arrival)

            if not self._started:
                self._started = True
                self._base_seq = info["seq"]
                self._base_rtp_ts = info["ts"]
                self._expected_ext = info["seq"]
                self._ev("first_packet", seq=info["seq"])

            ext = self._to_ext(info["seq"], arrival)
            kind = self._classify(ext, info["seq"], arrival)
            if self._sample_fh:
                self._sample_fh.write(
                    f"{arrival:.6f},{info['seq']},{ext},{kind},\n")
            self._buf_add(ext, info, arrival, kind)
            if kind in ("in_order", "reorder"):
                self._drain_forward(arrival)

    def _to_ext(self, seq16, arrival):
        """Map a 16-bit seq onto the running 64-bit extended space."""
        delta = rtp.seq_delta(seq16, self._expected_ext & 0xFFFF)
        ext = self._expected_ext + delta
        # wrap = the arriving 16-bit seq itself falls back across the edge;
        # a forward high->low transition can only be a wrap, whereas a
        # backward low->high jump is ordinary reordering/duplication
        if self._last_seq16 is not None and seq16 < self._last_seq16 \
                and self._last_seq16 >= 0xFF00 and seq16 <= 0x00FF:
            self.stats["wraps"] += 1
            self._ev("wrap", at_seq=seq16, prev=self._last_seq16)
        self._last_seq16 = seq16
        return ext

    def _classify(self, ext, seq16, arrival):
        if ext in self._buf or ext in self._delivered or ext < self._delivered_lo:
            return "duplicate" if ext >= self._delivered_lo else "late"
        if ext < self._expected_ext:
            return "late"
        if ext > self._expected_ext:
            return "reorder"
        return "in_order"

    def _buf_add(self, ext, info, arrival, kind):
        if kind == "duplicate":
            self.stats["duplicates"] += 1
            return
        if kind == "late":
            self.stats["late_after_gap"] += 1
            return
        if kind == "reorder":
            self.stats["out_of_order"] += 1
        else:
            self.stats["in_order"] += 1
        self._buf[ext] = (info, arrival)

    def _drain_forward(self, arrival):
        """Release every contiguous packet starting at expected."""
        released = 0
        while self._expected_ext in self._buf:
            info, _ = self._buf.pop(self._expected_ext)
            self._play_q.append(("pkt", self._expected_ext, info["payload"]))
            self._delivered.add(self._expected_ext)
            self._delivered_q.append(self._expected_ext)
            if len(self._delivered_q) == self._delivered_q.maxlen:
                evicted = self._delivered_q[0]
                self._delivered.discard(evicted)
                self._delivered_lo = evicted + 1
            self._expected_ext += 1
            released += 1
        if released:
            self._first_gap_seen_at = None
            hw = len(self._play_q)
            self.stats["playout_high_water"] = max(
                self.stats["playout_high_water"], hw)
            self._cond.notify_all()
        elif self._first_gap_seen_at is None:
            self._first_gap_seen_at = arrival

    def _watchdog(self):
        """Declare unrecoverable gaps after reorder_timeout; never wait forever."""
        while not self._stop.is_set():
            time.sleep(0.05)
            with self._lock:
                self._watchdog_once_locked()

    def _watchdog_once(self):
        with self._lock:
            self._watchdog_once_locked()

    def _watchdog_once_locked(self):
        if not self._started or not self._first_gap_seen_at:
            return
        aged = (time.monotonic() - self._first_gap_seen_at
                >= self.reorder_timeout)
        oversized = len(self._buf) > self.max_reorder
        if not (aged or oversized):
            return
        if not self._buf:
            # nothing on the wire at all; only declare after timeout
            if not aged:
                return
            reason = "timeout-no-traffic"
            # cannot know extent until next packet arrives; mark one
            # interval now and re-arm; arrival will reconcile
            missing = 1
            next_ext = self._expected_ext + 1
        else:
            next_ext = min(self._buf)
            missing = next_ext - self._expected_ext
            reason = "oversize-buffer" if oversized else "reorder-timeout"
        self._declare_gap(missing, next_ext, reason)

    def _declare_gap(self, missing, next_ext, reason):
        first_seq = self._expected_ext & 0xFFFF
        last_seq = (self._expected_ext + missing - 1) & 0xFFFF
        self.stats["missing_packets"] += missing
        self.stats["gap_intervals"] += 1
        t0 = self.stats["start_time"] or time.monotonic()
        self.gap_events.append({
            "at": round(time.monotonic() - t0, 3),
            "from_seq": first_seq, "to_seq": last_seq,
            "count": missing, "reason": reason,
        })
        self._ev("gap", **self.gap_events[-1])
        self._play_q.append(("gap", missing))
        self._expected_ext = next_ext
        self._first_gap_seen_at = None
        self._drain_forward(time.monotonic())

    # ------------------------------------------------------------- playout
    def _play_loop(self):
        """Ordered payload -> analyzer/capture/decoder, with bounded buffer."""
        while not self._stop.is_set():
            with self._lock:
                while not self._play_q and not self._stop.is_set():
                    self._cond.wait(timeout=0.5)
                if self._stop.is_set():
                    return
                item = self._play_q.popleft()
            if item[0] == "gap":
                n = item[1]
                # keep decoder byte-clock fed with null TS (PID 0x1FFF);
                # the interval stays explicitly marked, nothing waits forever
                nulls = NULL_TS_PACKET * (n * 7)
                self.stats["null_ts_inserted"] += n * 7
                self._sink(nulls, is_null=True)
            else:
                _, ext, payload = item
                for ts_pkt in rtp.split_ts_packets(payload):
                    self.analyzer.feed(ts_pkt, time.monotonic())
                self._sink(payload)
            if len(self._play_q) > self.max_playout:
                # bounded live buffer: oldest content is sacrificed, not RAM
                dropped = self._play_q.popleft()
                self.stats["playout_overflows"] += 1
                self._ev("buffer_overflow", item=str(dropped[0]))

    def _sink(self, data, is_null=False):
        if self._capture_fh and not is_null:
            self._capture_fh.write(data)
        self.decoder.write(data)
        with self._streams_lock:
            dead = []
            for proc in self._streams:
                if proc.poll() is not None:
                    dead.append(proc)
                    continue
                try:
                    proc.stdin.write(data)
                except (BrokenPipeError, OSError):
                    dead.append(proc)
            for proc in dead:
                self._streams.remove(proc)

    def add_stream(self):
        proc = self.decoder.open_stream()
        with self._streams_lock:
            self._streams.append(proc)
        return proc

    def note_source_switch(self, channel):
        """Called by the kernel when the sender restarts its ffmpeg source.
        RTP sequence space stays continuous, but the MPEG-TS program table
        enters a new generation (encoder may reuse PMT version 0)."""
        self.analyzer.reset_tables(time.monotonic(),
                                   f"channel_switch:{channel}")
        self._ev("source_switch", channel=channel)

    def remove_stream(self, proc):
        with self._streams_lock:
            if proc in self._streams:
                self._streams.remove(proc)
        if proc.poll() is None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    # ------------------------------------------------------------- metrics
    def _update_jitter(self, rtp_ts, arrival):
        # RFC 3550 appendix A.8, units of RTP clock
        if self._last_arrival is not None:
            d = ((arrival - self._last_arrival) * rtp.TS_CLOCK_HZ
                 - (rtp_ts - self._last_rtp_ts))
            self._j += abs(d) - self._j / 16
            self.stats["jitter_rfc3550"] = round(
                self._j / rtp.TS_CLOCK_HZ * 1000, 3)  # ms
        self._last_arrival = arrival
        self._last_rtp_ts = rtp_ts

    def _ev(self, kind, **kw):
        self.events.append({"at": round(time.monotonic(), 3),
                            "kind": kind, **kw})

    def snapshot(self):
        t0 = self.stats["start_time"]
        with self._lock:
            base = dict(self.stats)
            qsize = len(self._play_q)
            bufsize = len(self._buf)
            expected = self._expected_ext & 0xFFFF
        if base["first_packet_at"] and t0:
            base["first_packet_at"] = round(base["first_packet_at"] - t0, 3)
        base["playout_queue_now"] = qsize
        base["reorder_buffer_now"] = bufsize
        base["expected_seq"] = expected
        base["capture"] = (str(self._capture_fh.name)
                           if self._capture_fh else None)
        return {
            "stats": base,
            "gaps": list(self.gap_events),
            "events": list(self._clean_events())[-20:],
            "ts": self.analyzer.snapshot(),
            "decoder": self.decoder.snapshot(t0),
        }

    def _clean_events(self):
        return self.events

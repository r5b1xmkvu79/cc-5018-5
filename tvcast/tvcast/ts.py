"""MPEG-TS (ISO/IEC 13818-1) inspection: PAT/PMT versions, PCR, continuity counters."""
from collections import defaultdict

TS_SYNC = 0x47
PID_PAT = 0x0000
PID_NULL = 0x1FFF
PCR_HZ = 27_000_000


class _SectionBuffer:
    """Reassemble one table section per PID across TS packets."""

    def __init__(self):
        self.buf = b""
        self.active = False

    def feed(self, payload, pusi):
        out = None
        if pusi:
            pointer = payload[0]
            self.buf = payload[1 + pointer:]
            self.active = True
        elif self.active:
            self.buf += payload
        if self.active and len(self.buf) >= 3:
            sec_len = ((self.buf[1] & 0x0F) << 8) | self.buf[2]
            total = 3 + sec_len
            if len(self.buf) >= total:
                out = self.buf[:total]
                self.buf = b""
                self.active = False
        return out


def _parse_pat(section):
    # table_id(1) + flags/len(2) + tsid(2) + version byte(1) + sec/last(2)
    version = (section[5] >> 1) & 0x1F
    programs = {}
    pos = 8
    end = len(section) - 4  # CRC32
    while pos + 4 <= end:
        program_num = (section[pos] << 8) | section[pos + 1]
        pid = ((section[pos + 2] & 0x1F) << 8) | section[pos + 3]
        if program_num != 0:
            programs[program_num] = pid
        pos += 4
    return version, programs


def _parse_pmt(section):
    version = (section[5] >> 1) & 0x1F
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    info_len = ((section[10] & 0x0F) << 8) | section[11]
    pos = 12 + info_len
    end = len(section) - 4
    streams = []
    while pos + 5 <= end:
        stream_type = section[pos]
        es_pid = ((section[pos + 1] & 0x1F) << 8) | section[pos + 2]
        es_info_len = ((section[pos + 3] & 0x0F) << 8) | section[pos + 4]
        streams.append({"stream_type": stream_type, "pid": es_pid})
        pos += 5 + es_info_len
    return version, pcr_pid, streams


class TSAnalyzer:
    def __init__(self):
        self.ts_packet_count = 0
        self.tei_count = 0
        self.pid_packets = defaultdict(int)
        self.cc_errors = 0
        self.discontinuity_markers = 0
        self.pat_versions = []          # [{version, programs, wall}]
        self.pmt_versions = {}          # pid -> [{version, pcr_pid, streams, wall}]
        self.pcr = []                   # last N observations
        self._last_cc = {}              # pid -> last cc
        self._sections = defaultdict(_SectionBuffer)
        self._last_pcr_base = None
        self.pcr_jitter_us = []         # deviation from wall-clock expectation
        self.events = []
        self.epoch = 0
        self._pmt_pids = set()

    def reset_tables(self, wall, reason):
        """Source restarted (channel switch): next PAT/PMT is a new generation
        even if the encoder reuses version number 0."""
        self.epoch += 1
        self._sections.clear()
        self._pmt_pids.clear()
        self._last_cc.clear()
        self.events.append({"at": wall, "kind": "table_epoch",
                            "epoch": self.epoch, "reason": reason})

    def feed(self, pkt, wall):
        if len(pkt) < 4 or pkt[0] != TS_SYNC:
            return
        self.ts_packet_count += 1
        pusi = bool(pkt[1] & 0x40)
        tei = bool(pkt[1] & 0x80)
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        afc = (pkt[3] >> 4) & 0x03
        cc = pkt[3] & 0x0F
        if tei:
            self.tei_count += 1
        self.pid_packets[pid] += 1

        # continuity counter check (per PID, packets that carry payload)
        has_payload = afc in (1, 3)
        if pid != PID_NULL and has_payload:
            if pid in self._last_cc:
                expected = (self._last_cc[pid] + 1) & 0x0F
                if cc != expected:
                    self.cc_errors += 1
                    self.events.append({
                        "at": wall, "kind": "cc_error", "pid": pid,
                        "expected": expected, "got": cc,
                    })
            self._last_cc[pid] = cc

        offset = 4
        discont = False
        if afc in (2, 3):  # adaptation field present
            af_len = pkt[4]
            if af_len > 0 and 5 + af_len <= len(pkt):
                flags = pkt[5]
                discont = bool(flags & 0x80)
                if discont:
                    self.discontinuity_markers += 1
                    self._last_cc.pop(pid, None)
                p = 6
                if flags & 0x10 and p + 6 <= 5 + af_len:  # PCR flag
                    base = ((pkt[p] << 25) | (pkt[p + 1] << 17) |
                            (pkt[p + 2] << 9) | (pkt[p + 3] << 1) |
                            (pkt[p + 4] >> 7))
                    ext = ((pkt[p + 4] & 0x01) << 8) | pkt[p + 5]
                    pcr = base * 300 + ext
                    self._record_pcr(pid, pcr, wall)
                p += af_len
            offset = 5 + af_len

        table_pid = pid == PID_PAT or self._is_known_pmt(pid)
        if has_payload and table_pid:
            section = self._sections[pid].feed(pkt[offset:], pusi)
            if section:
                self._handle_section(pid, section, wall)

    def _is_known_pmt(self, pid):
        return pid in self._pmt_pids

    def _handle_section(self, pid, section, wall):
        table_id = section[0]
        try:
            if table_id == 0x00 and pid == PID_PAT:
                version, programs = _parse_pat(section)
                self._pmt_pids = set(programs.values())
                if not self.pat_versions \
                        or self.pat_versions[-1]["epoch"] != self.epoch \
                        or self.pat_versions[-1]["version"] != version \
                        or self.pat_versions[-1]["programs"] != programs:
                    entry = {"version": version, "programs": programs,
                             "wall": wall, "epoch": self.epoch}
                    self.pat_versions.append(entry)
                    self.events.append({"at": wall, "kind": "pat_change", **entry})
            elif table_id == 0x02:
                version, pcr_pid, streams = _parse_pmt(section)
                fingerprint = (version, pcr_pid,
                               tuple((x["stream_type"], x["pid"]) for x in streams))
                history = self.pmt_versions.setdefault(pid, [])
                if (not history or history[-1]["epoch"] != self.epoch
                        or history[-1]["fingerprint"] != fingerprint):
                    entry = {"version": version, "pcr_pid": pcr_pid,
                             "streams": streams, "wall": wall,
                             "fingerprint": fingerprint, "epoch": self.epoch,
                             "generation": len(history)}
                    history.append(entry)
                    self.events.append({"at": wall, "kind": "pmt_change",
                                        "pmt_pid": pid,
                                        "generation": entry["generation"],
                                        "epoch": self.epoch,
                                        "version": version,
                                        "pcr_pid": pcr_pid,
                                        "streams": streams})
        except (IndexError, KeyError):
            pass  # malformed section; counter keeps running

    def _record_pcr(self, pid, pcr, wall):
        # PCR wraps at 2^33*300 (~26.5 h); handle one wrap
        if self._last_pcr_base is not None:
            prev_pcr, prev_wall = self._last_pcr_base
            delta_pcr = (pcr - prev_pcr) & ((1 << 33) * 300 - 1)
            delta_wall = int((wall - prev_wall) * PCR_HZ)
            jitter_us = (delta_pcr - delta_wall) / 27.0
            self.pcr_jitter_us.append(round(jitter_us, 1))
            self.pcr_jitter_us[:] = self.pcr_jitter_us[-200:]
        self._last_pcr_base = (pcr, wall)
        obs = {"pid": pid, "pcr": pcr, "wall": round(wall, 6)}
        self.pcr.append(obs)
        self.pcr[:] = self.pcr[-50:]

    def snapshot(self):
        jit = self.pcr_jitter_us[-100:]
        return {
            "ts_packets": self.ts_packet_count,
            "tei": self.tei_count,
            "cc_errors": self.cc_errors,
            "discontinuity_markers": self.discontinuity_markers,
            "pid_packets": dict(sorted(self.pid_packets.items())),
            "pat": self.pat_versions[-1] if self.pat_versions else None,
            "pat_history": list(self.pat_versions),
            "pmt": {str(pid): hist[-1] for pid, hist in self.pmt_versions.items()},
            "pmt_history": {str(k): list(v) for k, v in self.pmt_versions.items()},
            "pcr_last": self.pcr[-1] if self.pcr else None,
            "pcr_jitter_us": {
                "min": min(jit) if jit else None,
                "max": max(jit) if jit else None,
                "last": jit[-1] if jit else None,
            },
            "events": list(self.events[-20:]),
        }

"""Channel definitions and default transport configuration."""

DEFAULT_MCAST = {"group": "239.10.20.30", "port": 5004, "iface": "127.0.0.1"}
DEFAULT_UNICAST = {"host": "127.0.0.1", "port": 5004}

# Three in-house programs. Each ffmpeg command emits a constant-rate
# MPEG-TS on stdout; the sender RTP-packetizes it itself so that
# impairment injection sits on the actual RTP sequence number space.
CHANNELS = {
    "campus-news": {
        "label": "园区新闻 (Campus News)",
        "cmd": [
            "-re", "-f", "lavfi", "-i",
            "testsrc2=size=1280x720:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-g", "25", "-b:v", "1200k",
            "-c:a", "aac", "-b:a", "96k",
            "-mpegts_flags", "+pat_pmt_at_frames",
            "-muxrate", "2000k", "-f", "mpegts", "pipe:1",
        ],
    },
    "campus-life": {
        "label": "园区生活 (Campus Life)",
        "cmd": [
            "-re", "-f", "lavfi", "-i",
            "smptebars=size=1280x720:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-g", "50", "-b:v", "900k",
            "-c:a", "aac", "-b:a", "64k",
            "-mpegts_flags", "+pat_pmt_at_frames",
            "-muxrate", "1500k", "-f", "mpegts", "pipe:1",
        ],
    }, "campus-tts": {
        "label": "图文频道 (Info Ticker)",
        "cmd": [
            "-re", "-f", "lavfi", "-i",
            "rgbtestsrc=size=854x480:rate=15",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-g", "30", "-b:v", "500k",
            "-c:a", "aac", "-b:a", "48k",
            "-mpegts_flags", "+pat_pmt_at_frames",
            "-muxrate", "800k", "-f", "mpegts", "pipe:1",
        ],
    },
}

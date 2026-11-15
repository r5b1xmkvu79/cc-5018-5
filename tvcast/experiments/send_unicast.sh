#!/usr/bin/env bash
# Unicast alternative: point-to-point RTP/MPEG-TS when multicast is unavailable
# (e.g. switches without IGMP, cloud VMs, restricted lab VLANs).
# Start recv_unicast.sh FIRST, then this sender.
set -euo pipefail
cd "$(dirname "$0")"
FF=../tools/ffmpeg-static/ffmpeg
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5004}"

exec "$FF" -hide_banner -loglevel info \
  -re -f lavfi -i "testsrc2=size=1280x720:rate=25" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -c:v libx264 -preset veryfast -tune zerolatency -g 25 -b:v 1200k \
  -c:a aac -b:a 96k \
  -f rtp_mpegts "rtp://$HOST:$PORT?pkt_size=1316"

#!/usr/bin/env bash
# Standalone multicast experiment: ffmpeg -> RTP/MPEG-TS -> multicast group.
# This bypasses the Python sender; use it to validate the network path alone.
# Join/inspect with the companion receive script or any RTP client.
set -euo pipefail
cd "$(dirname "$0")"
FF=../tools/ffmpeg-static/ffmpeg
GROUP="${GROUP:-239.10.20.30}"
PORT="${PORT:-5004}"
TTL="${TTL:-2}"

# rtp_mpegts muxer does RTP packetization (PT 33, 7 TS packets/datagram)
# natively; the rtp:// URL to a 239.x address selects multicast and the
# localaddr query pins the outgoing interface so loopback delivery works.
exec "$FF" -hide_banner -loglevel info \
  -re -f lavfi -i "testsrc2=size=1280x720:rate=25" \
  -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -c:v libx264 -preset veryfast -tune zerolatency -g 25 -b:v 1200k \
  -c:a aac -b:a 96k \
  -f rtp_mpegts \
  "rtp://$GROUP:$PORT?pkt_size=1316&ttl=$TTL&localaddr=127.0.0.1"

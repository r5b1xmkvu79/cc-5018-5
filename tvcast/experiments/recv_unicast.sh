#!/usr/bin/env bash
# Unicast receiver: listens on UDP PORT for RTP/MPEG-TS and decodes headless.
# Start this before send_unicast.sh.
set -euo pipefail
cd "$(dirname "$0")"
FF=../tools/ffmpeg-static/ffmpeg
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5004}"

# listen=1 binds the socket instead of joining a multicast group
exec "$FF" -hide_banner -loglevel info \
  -protocol_whitelist "file,udp,rtp,crypto,srtp" \
  -i "rtp://$HOST:$PORT?listen=1" \
  -c:v libx264 -preset ultrafast -c:a aac -f null -

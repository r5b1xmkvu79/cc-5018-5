#!/usr/bin/env bash
# Receive the multicast RTP/MPEG-TS stream and decode to a window/null probe.
# Set PROBE=1 for a headless decode test (no display), otherwise ffplay opens.
set -euo pipefail
cd "$(dirname "$0")"
FF=../tools/ffmpeg-static/ffmpeg
GROUP="${GROUP:-239.10.20.30}"
PORT="${PORT:-5004}"
URL="rtp://$GROUP:$PORT"

if [[ "${PROBE:-0}" == "1" ]]; then
  # headless: full decode, report errors, no output device needed
  exec "$FF" -hide_banner -loglevel info \
    -protocol_whitelist "file,udp,rtp,crypto,srtp" \
    -i "$URL" -c:v libx264 -preset ultrafast -c:a aac -f null -
else
  exec "$FF" -hide_banner -loglevel info \
    -protocol_whitelist "file,udp,rtp,crypto,srtp" \
    -fflags +discardcorrupt -i "$URL" \
    -c copy -f mp4 -movflags frag_keyframe+empty_moov pipe:1 \
    | "$FF" -hide_banner -loglevel error -i pipe:0 -f null -
fi

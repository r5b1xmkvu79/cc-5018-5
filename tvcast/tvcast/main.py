"""Command-line entry: python -m tvcast.main [--host H] [--port P]"""
import argparse
from pathlib import Path

from .server import ControlServer


def main():
    ap = argparse.ArgumentParser(description="Campus TV RTP/MPEG-TS prototype")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    base = Path(__file__).resolve().parent.parent
    server = ControlServer(base, host=args.host, port=args.port)
    print(f"TV transport control page: http://{args.host}:{args.port}/")
    print("Press Ctrl+C to stop.")
    try:
        server.serve()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()

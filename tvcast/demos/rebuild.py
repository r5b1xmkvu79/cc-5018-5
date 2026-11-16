#!/usr/bin/env python3
"""Rebuild all TV transport demo assets from source (content -> wire -> manifest).

    python3 demos/rebuild.py            # build anything missing
    python3 demos/rebuild.py --force    # rebuild everything from scratch
    python3 demos/rebuild.py --verify   # rebuild and replay every asset

The demo assets are checked into the repository so the transport page can load
them directly. This script is the reproducible source that produced them: it
regenerates the pictures, the marker audio, the MPEG-TS clips, the packetized
RTP wires, the fault schedules and every manifest. Running it twice yields
byte-identical outputs.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tvcast.demo import build, wire  # noqa: E402
from tvcast.demo.library import DemoLibrary  # noqa: E402


def verify_all(speed=2.0):
    import time
    library = DemoLibrary(ROOT)
    failures = 0
    for asset in library.catalog()["assets"]:
        library.start_replay(asset["id"], speed=speed)
        result = {}
        for _ in range(120):
            result = library.state().get("last_result") or {}
            if result.get("asset_id") == asset["id"]:
                break
            time.sleep(0.5)
        library.stop_replay()
        if result.get("error"):
            failures += 1
            print(f"VERIFY {asset['id']}: ERROR {result['error']}")
            continue
        failed = [c for c in result.get("checks", []) if not c["passed"]]
        failures += len(failed)
        print(f"VERIFY {asset['id']}: {result.get('passed', 0)} passed, "
              f"{len(failed)} failed")
        for check in failed:
            print(f"   FAIL {check['key']}: {check['actual']} != "
                  f"{check['expected']}")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="rebuild even if outputs exist")
    parser.add_argument("--verify", action="store_true",
                        help="replay and verify every built asset")
    parser.add_argument("--speed", type=float, default=2.0)
    args = parser.parse_args()

    results = build.build_all_assets(force=args.force)
    manifest = {
        "schema": "tvcast.demo.index/1",
        "assets": [
            {"id": asset["id"],
             "label": asset["label"],
             "program": asset["program"]["id"],
             "wire": asset["wire"]["file"],
             "wire_sha256": asset["wire"]["sha256"],
             "manifest": f"{asset['id']}.json"}
            for _, _, asset in results
        ],
    }
    wire.write_json(ROOT / "demos" / "index.json", manifest)
    print(f"indexed {len(results)} assets in demos/index.json")

    if args.verify:
        failures = verify_all(args.speed)
        if failures:
            print(f"verification failed: {failures} check(s)")
            return 1
        print("all demo assets verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
